/**
 * carher-ext-login — 外部用户发「登录」⇒ 确定性回一条一次性登录链接。
 *
 * 为什么是 before_dispatch:
 *   her-236 的 lark 插件在 dispatchReplyFromConfig 里跑 before_dispatch(claiming hook),
 *   返回 {handled:true,text} 就直接把 text 当最终回复发出去,**完全不进模型**
 *   (dispatch-DnzGTpPs.js:1736-1788)。线上 carher-engine-swap 的 /dify 一次性登录链接
 *   走的就是这同一个钩子,属于已验证的生产路径。
 *
 * 身份:只认飞书事件里的 sender。
 *   - open_id  = event.senderId / ctx.senderId(由 lark 插件从 event.sender.sender_id.open_id 填,
 *                parse.js:110;消息正文完全不参与)
 *   - union_id = 用 app B 的 tenant_access_token 查 p2p 会话成员(member_id_type=union_id)。
 *                外部人查通讯录必 41050,所以不走 contact 接口。
 *   - 拿不到 union_id ⇒ 不签发,回一句人话。
 *
 * 名单:ALLOW_FILE(PVC 上的 JSON)。不在名单里 ⇒ 这个钩子完全不介入(return undefined),
 *   消息按原有逻辑继续走(DM allowlist 会拒,行为与现状一致)。
 *
 * 失败一律 fail-open(return undefined / 或回一句错误文案),绝不打断 inbound dispatch。
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const PLUGIN_ID = "carher-ext-login";
const STATE_DIR = process.env.CARHER_EXT_LOGIN_STATE_DIR || "/data/.openclaw/carher-ext-login";
const ALLOW_FILE = path.join(STATE_DIR, "allow.json");
const AUDIT_FILE = path.join(STATE_DIR, "audit.jsonl");
const PORTAL_URL =
  process.env.CARHER_EXT_LOGIN_PORTAL_URL ||
  "https://chat.auto-link.com.cn/ai/api/internal/magic-link";
const SECRET_FILE =
  process.env.CARHER_EXT_LOGIN_SECRET_FILE || path.join(STATE_DIR, "internal-secret");
const FEISHU_BASE = process.env.CARHER_EXT_LOGIN_FEISHU_BASE || "https://open.feishu.cn/open-apis";
const OUR_TENANT_KEY = process.env.CARHER_EXT_LOGIN_OUR_TENANT_KEY || "1088406f1c155740";

// 触发词:精确匹配,避免把普通聊天吞掉。全角/半角、大小写、首尾空白都归一。
const TRIGGERS = new Set(["登录", "登陆", "登录链接", "login", "/登录", "/login"]);

const OPEN_ID_RE = /^ou_[A-Za-z0-9]+$/;
const UNION_ID_RE = /^on_[A-Za-z0-9]{8,64}$/;

// 每人每窗口最多签发次数(进程内,重启即清。真正的限流在门户侧 rl_issue=5/min)。
const RATE_WINDOW_MS = 60_000;
const RATE_MAX = 3;
const rate = new Map();

let logger = console;
function log(level, msg) {
  const line = `[${PLUGIN_ID}] ${msg}`;
  try {
    const fn = logger?.[level] ?? logger?.info ?? console.log;
    fn.call(logger, line);
  } catch {
    console.log(line);
  }
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

/** 名单:{"allow":["ou_xxx", ...]}。读不到就当空名单(fail closed:谁都不放行)。 */
function isAllowed(openId) {
  const doc = readJson(ALLOW_FILE);
  const list = Array.isArray(doc?.allow) ? doc.allow : [];
  return list.includes(openId);
}

function audit(entry) {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.appendFileSync(AUDIT_FILE, JSON.stringify({ t: new Date().toISOString(), ...entry }) + "\n");
  } catch (err) {
    log("warn", `audit write failed: ${String(err).slice(0, 120)}`);
  }
}

function hitRate(openId) {
  const now = Date.now();
  const arr = (rate.get(openId) ?? []).filter((t) => now - t < RATE_WINDOW_MS);
  if (arr.length >= RATE_MAX) {
    rate.set(openId, arr);
    return false;
  }
  arr.push(now);
  rate.set(openId, arr);
  return true;
}

function normalize(text) {
  if (typeof text !== "string") return "";
  return text
    .trim()
    // 去掉前导 @某人(飞书渲染成 <at …>名字</at>,或纯文本 @名字)
    .replace(/^(?:<at[^>]*>[^<]*<\/at>\s*|@\S+\s+)+/u, "")
    .replace(/[\s　]+/gu, "")
    .toLowerCase();
}

/** 候选文本:只看"当前这条消息"的字段,不碰 body/history(避免历史里出现过"登录"就误触发)。 */
function findTrigger(event, ctx) {
  const cands = [event?.content, ctx?.content, event?.text];
  for (const c of cands) {
    if (typeof c !== "string" || !c) continue;
    let t = c;
    // lark 文本消息有时是 {"text":"..."} 这种 JSON
    if (t.startsWith("{")) {
      try {
        const p = JSON.parse(t);
        if (typeof p?.text === "string") t = p.text;
      } catch {}
    }
    if (TRIGGERS.has(normalize(t))) return true;
  }
  return false;
}

async function httpJson(url, opts, timeoutMs) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...opts, signal: ac.signal });
    const text = await res.text();
    let body = null;
    try {
      body = JSON.parse(text);
    } catch {}
    return { status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}

/** app B 的 tenant_access_token。只在内存里拿,永不写盘、永不进日志。 */
async function tenantToken() {
  const appId = process.env.FEISHU_APP_ID;
  const appSecret = process.env.FEISHU_APP_SECRET;
  if (!appId || !appSecret) return null;
  const r = await httpJson(
    `${FEISHU_BASE}/auth/v3/tenant_access_token/internal`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ app_id: appId, app_secret: appSecret }),
    },
    10_000,
  );
  return r.body?.code === 0 ? r.body.tenant_access_token || null : null;
}

/**
 * open_id → union_id,**以这条消息本身为真相源**。
 *
 * 走 `/im/v1/messages/{message_id}`,它按 user_id_type 直接给出发件人的 open_id 或 union_id,
 * 还带真实 tenant_key。两次调用交叉校验:open_id 口径拿到的发件人必须与事件里的 senderId
 * 逐字相同,否则拒签(防止 message_id 与 sender 对不上)。
 * 不走 contact 接口——外部租户必 41050;也不靠"p2p 只有一个成员"这种假设。
 *
 * 返回 {unionId, name, external, tenantKey, chatId} 或 null。
 */
async function resolveIdentity(openId, messageId) {
  if (!OPEN_ID_RE.test(openId)) return null;
  if (typeof messageId !== "string" || !/^om_[A-Za-z0-9]+$/.test(messageId)) return null;
  const tok = await tenantToken();
  if (!tok) {
    log("warn", "tenant_access_token unavailable (FEISHU_APP_ID/SECRET missing or rejected)");
    return null;
  }
  const h = { Authorization: `Bearer ${tok}` };
  const url = (idType) =>
    `${FEISHU_BASE}/im/v1/messages/${encodeURIComponent(messageId)}?user_id_type=${idType}`;

  const byOpen = await httpJson(url("open_id"), { headers: h }, 10_000);
  if (byOpen.body?.code !== 0) {
    log("warn", `messages/get(open_id) failed code=${byOpen.body?.code} http=${byOpen.status}`);
    return null;
  }
  const itOpen = (byOpen.body?.data?.items ?? [])[0];
  const senderOpen = String(itOpen?.sender?.id || "");
  if (senderOpen !== openId) {
    log("warn", "message sender does not match event senderId, refusing to issue");
    return null;
  }
  if (String(itOpen?.sender?.sender_type || "") !== "user") return null;

  const byUnion = await httpJson(url("union_id"), { headers: h }, 10_000);
  if (byUnion.body?.code !== 0) {
    log("warn", `messages/get(union_id) failed code=${byUnion.body?.code} http=${byUnion.status}`);
    return null;
  }
  const itUnion = (byUnion.body?.data?.items ?? [])[0];
  const unionId = String(itUnion?.sender?.id || "");
  if (!UNION_ID_RE.test(unionId)) return null;

  const tenantKey = String(itUnion?.sender?.tenant_key || itOpen?.sender?.tenant_key || "");
  return {
    unionId,
    name: "",
    tenantKey,
    external: Boolean(tenantKey) && tenantKey !== OUR_TENANT_KEY,
    chatId: String(itOpen?.chat_id || ""),
  };
}

function internalSecret() {
  try {
    const s = fs.readFileSync(SECRET_FILE, "utf8").trim();
    return s.length >= 32 ? s : null;
  } catch {
    return null;
  }
}

/** 调门户签发一次性登录链接。只回 url,错误里不带密钥。 */
async function issueMagicLink(identity, openId) {
  const secret = internalSecret();
  if (!secret) return { ok: false, reason: "no_secret" };
  const r = await httpJson(
    PORTAL_URL,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        Authorization: `Bearer ${secret}`,
      },
      body: JSON.stringify({
        union_id: identity.unionId,
        open_id: openId,
        tenant_key: identity.tenantKey,
        name: identity.name,
      }),
    },
    20_000,
  );
  const url = r.body?.url;
  if (r.status === 200 && typeof url === "string" && url.startsWith("https://")) {
    return { ok: true, url, expiresIn: r.body?.expires_in };
  }
  return { ok: false, reason: `http_${r.status}`, message: r.body?.message };
}

const plugin = {
  id: PLUGIN_ID,
  name: "CarHer External Login Link",
  version: "0.1.0",
  description: "外部用户发「登录」⇒ 确定性回一次性登录链接(不经过模型)。",

  register(api) {
    logger = api?.logger ?? console;

    // 🔴 11-05 上海车联外包实测「没能确认你的飞书身份」:gateway 调 before_dispatch 时
    //   **不传 messageId**(dispatch-DnzGTpPs.js:1737 的事件对象里只有 content/senderId/isGroup 等),
    //   audit 里两次失败都是 message_id:"" ⇒ resolveIdentity 直接拒。
    //   message_received 钩子带 messageId + senderId,且在同一次 dispatch 里**更早**触发(:1444),
    //   handler 同步部分先跑 ⇒ 在这里按 senderId 记下最近一条消息 id,before_dispatch 取用。
    const lastMsg = new Map(); // senderId → {id, at}
    const MSG_TTL_MS = 60_000;
    const onReceived = (event, ctx) => {
      try {
        const sid = String(event?.senderId ?? ctx?.senderId ?? "");
        const mid = String(event?.messageId ?? event?.metadata?.messageId ?? "");
        if (OPEN_ID_RE.test(sid) && /^om_[A-Za-z0-9]+$/.test(mid)) {
          lastMsg.set(sid, { id: mid, at: Date.now() });
        }
      } catch {}
    };
    const messageIdFor = (event, ctx, openId) => {
      const direct = String(event?.messageId ?? ctx?.messageId ?? "");
      if (direct) return direct;
      const r = lastMsg.get(openId);
      return r && Date.now() - r.at < MSG_TTL_MS ? r.id : "";
    };
    // message_received 是 fire-and-forget,理论上可能比 before_dispatch 晚一点点 ⇒ 最多等 1.5 秒
    const waitMessageId = async (event, ctx, openId) => {
      for (let i = 0; i < 15; i++) {
        const id = messageIdFor(event, ctx, openId);
        if (id) return id;
        await new Promise((r) => setTimeout(r, 100));
      }
      return "";
    };
    try {
      if (typeof api.on === "function") api.on("message_received", onReceived, { name: "ext-login-msgid" });
      else if (typeof api.registerHook === "function") api.registerHook("message_received", onReceived, { name: "ext-login-msgid" });
    } catch (e) {
      log("warn", `message_received register failed: ${String(e).slice(0, 120)}`);
    }

    const handler = async (event, ctx) => {
      try {
        if (!findTrigger(event, ctx)) return; // 不是我们的词 ⇒ 完全不介入
        const openId = String(event?.senderId ?? ctx?.senderId ?? "");
        const channelId = String(ctx?.channelId ?? "");
        if (channelId && channelId !== "feishu") return;
        if (!OPEN_ID_RE.test(openId)) return;
        const isGroup = event?.isGroup === true || ctx?.isGroup === true;
        if (isGroup) return; // 只在私聊里生效,绝不在群里签发

        if (!isAllowed(openId)) {
          // 不在名单里:什么都不做,让原有准入逻辑说话(现状是 not in DM allowlist)
          log("info", `trigger from ${openId} ignored: not in ext-login allowlist`);
          return;
        }
        if (!hitRate(openId)) {
          audit({ ev: "rate_limited", open_id: openId });
          return { handled: true, text: "请求有点频繁,过一分钟再发一次「登录」。" };
        }

        const messageId = await waitMessageId(event, ctx, openId);
        const identity = await resolveIdentity(openId, messageId);
        if (!identity) {
          audit({ ev: "identity_failed", open_id: openId, message_id: messageId });
          return {
            handled: true,
            text: "没能确认你的飞书身份,暂时发不了登录链接。请稍后再试一次;如果还不行请联系管理员。",
          };
        }

        const issued = await issueMagicLink(identity, openId);
        if (!issued.ok) {
          audit({ ev: "issue_failed", open_id: openId, union_id: identity.unionId, reason: issued.reason });
          log("warn", `issue failed: ${issued.reason}`);
          return {
            handled: true,
            text: "登录链接生成失败,请稍后再发一次「登录」。如果一直失败请联系管理员。",
          };
        }
        audit({ ev: "issued", open_id: openId, union_id: identity.unionId, external: identity.external });
        const mins = Math.max(1, Math.round(Number(issued.expiresIn || 600) / 60));
        return {
          handled: true,
          text:
            `你的一次性登录链接(${mins} 分钟内有效,只能用一次):\n${issued.url}\n\n` +
            `打开后就能看到自己的 key。链接过期或用过了,再发一句「登录」即可。`,
        };
      } catch (err) {
        log("error", `before_dispatch handler crashed: ${String(err).slice(0, 200)}`);
        return; // fail-open:绝不打断 inbound dispatch
      }
    };

    // 🔴 两个注册 API 都挂上(不同版本的 gateway 认的不一样),但**两条都生效时会双签**:
    //   11-04 14:08 真踩到 —— 机器人侧 audit 只有 1 条 issued,门户侧日志却是 **2 条**「magic 签发」。
    //   原因:registerHook 和 api.on 注册的是同一个 handler,一条消息跑两遍;
    //   第二遍在 issueMagicLink 之后才被"第一遍已经 handled"盖掉,所以**链接已经白签了一条**
    //   (一次性 token,多签一条就是多留一个能登录的凭据,TTL 内都有效)。
    //   修法:按 message_id 去重 —— 同一条消息只真干一次,第二遍直接复用第一遍的结果。
    const DEDUP_TTL_MS = 120_000;
    const seen = new Map();   // messageId → {at, result}
    const dedupedHandler = async (event, ctx) => {
      // 去重键:有 messageId 用它;没有(gateway 不传)就用 发件人+消息时间戳 —— 两个监听器拿到的是同一个事件对象,
      // 时间戳一致;两条不同消息的时间戳不同。缺了键就退化成不去重(宁可多签,不吞消息)。
      const sid = String(event?.senderId ?? ctx?.senderId ?? "");
      const direct = String(event?.messageId ?? ctx?.messageId ?? "");
      const mid = direct || (sid && event?.timestamp != null ? "ts:" + sid + ":" + event.timestamp : "");
      const now = Date.now();
      for (const [k, v] of seen) if (now - v.at > DEDUP_TTL_MS) seen.delete(k);
      if (mid && seen.has(mid)) return await seen.get(mid).result;   // 第二个监听器:复用,不重签(含进行中)
      const p = handler(event, ctx);
      if (mid) seen.set(mid, { at: now, result: p });
      return await p;
    };

    let registered = 0;
    try {
      if (typeof api.registerHook === "function") {
        api.registerHook("before_dispatch", dedupedHandler, { name: "ext-login-command" });
        registered++;
      }
    } catch (e) {
      log("warn", `registerHook failed: ${String(e).slice(0, 120)}`);
    }
    try {
      if (typeof api.on === "function") {
        api.on("before_dispatch", dedupedHandler, { name: "ext-login-command-on" });
        registered++;
      }
    } catch (e) {
      log("warn", `api.on failed: ${String(e).slice(0, 120)}`);
    }
    log("info", `registered ${registered} before_dispatch listener(s, deduped by message_id); allow=${ALLOW_FILE}`);
  },
};

module.exports = plugin;
module.exports.default = plugin;
