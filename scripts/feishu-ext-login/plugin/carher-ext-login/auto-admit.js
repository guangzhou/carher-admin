/**
 * 按公司自动放行:外部人私聊机器人被 DM 名单挡住 ⇒ 查他属于哪家公司 ⇒ 是已开通的合作公司就自动加名单,
 * 并私聊告诉他「已开通,再发一句「登录」」。
 *
 * 为什么在这里做、而不是钩子里:lark 插件的 DM 名单判定在 inbound 流水线第 5 步,不过就 return,
 * **任何插件钩子都看不到这条消息**(before_dispatch 在第 9 步,message_received 也在 gate 之后)。
 * 唯一能看到"有人被挡"的是 gateway 日志那一行 `sender ou_… not in DM allowlist`
 * (11-05 一上午 6 个上海车联外包被拒 38 次,全在日志里)。所以这里定时读日志增量。
 *
 * 失败关闭:
 *   - 只放 ALLOW_TENANTS_FILE 里列出的 tenant_key(= 门户 tenant_map 里已开通申请的公司);
 *     公司查不出 / 不在名单 / 文件缺失 ⇒ 什么都不做,人仍在门外(和以前一样)。
 *   - 公司以**飞书消息接口回的 sender.tenant_key** 为准(用他被挡的那条消息 id 去查),不信任何正文。
 * 只追加名单,从不删;每次加都写 audit。
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const LOG_DIR = process.env.CARHER_EXT_LOGIN_LOG_DIR || "/data/.openclaw/logs";
const TICK_MS = 10_000;
const RECV_RE = /\[msg:(om_[A-Za-z0-9]+)\]: received from (ou_[a-z0-9]+) in (oc_[a-z0-9]+) \(p2p\)/;
const REJ_RE = /sender (ou_[a-z0-9]+) not in DM allowlist/;

function start({ stateDir, dmStoreFile, allowFile, feishuBase, tenantToken, httpJson, audit, log }) {
  const tenantsFile = path.join(stateDir, "allow-tenants.json");
  const offsets = new Map();          // 日志文件 → 已读到的字节位置
  const recent = new Map();           // open_id → {mid, chat, at}(最近一条私聊消息)
  const done = new Map();             // open_id → at(处理过,10 分钟内不重复查)
  let busy = false;

  const readJson = (f) => { try { return JSON.parse(fs.readFileSync(f, "utf8")); } catch { return null; } };

  function allowedTenants() {
    const d = readJson(tenantsFile);
    return new Set(Array.isArray(d?.tenants) ? d.tenants.map(String) : []);
  }

  function appendList(file, key, id) {
    const doc = readJson(file) || {};
    const cur = Array.isArray(doc[key]) ? doc[key].map(String) : [];
    if (cur.map((x) => x.toLowerCase()).includes(id)) return false;
    doc[key] = cur.concat([id]);
    const tmp = file + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(doc));
    fs.chmodSync(tmp, 0o600);
    fs.renameSync(tmp, file);
    return true;
  }

  async function handleRejected(openId) {
    const now = Date.now();
    if (now - (done.get(openId) || 0) < 600_000) return;
    done.set(openId, now);
    const r = recent.get(openId);
    if (!r) return;
    const tenants = allowedTenants();
    if (tenants.size === 0) return;           // 没配公司名单 ⇒ 全关
    const tok = await tenantToken();
    if (!tok) return;
    const h = { Authorization: `Bearer ${tok}` };
    const m = await httpJson(`${feishuBase}/im/v1/messages/${encodeURIComponent(r.mid)}?user_id_type=open_id`,
                             { headers: h }, 10_000);
    const it = (m.body?.data?.items ?? [])[0];
    const sender = String(it?.sender?.id || "");
    const tk = String(it?.sender?.tenant_key || "");
    if (m.body?.code !== 0 || sender !== openId) {
      audit({ ev: "auto_admit_lookup_failed", open_id: openId, code: m.body?.code });
      return;
    }
    if (!tenants.has(tk)) {
      audit({ ev: "auto_admit_skipped", open_id: openId, tenant_key: tk });
      log("info", `auto-admit skipped ${openId}: tenant ${tk} not approved`);
      return;
    }
    const a = appendList(dmStoreFile, "allowFrom", openId);
    const b = appendList(allowFile, "allow", openId);
    audit({ ev: "auto_admitted", open_id: openId, tenant_key: tk, dm_added: a, issue_added: b });
    log("info", `auto-admitted ${openId} (tenant ${tk})`);
    await httpJson(`${feishuBase}/im/v1/messages?receive_id_type=chat_id`, {
      method: "POST",
      headers: { ...h, "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        receive_id: r.chat, msg_type: "text",
        content: JSON.stringify({ text: "已为你开通。请再发一句「登录」,我会回你一个登录链接(在电脑上打开)。" }),
      }),
    }, 10_000).catch(() => {});
  }

  function todayLog() {
    let best = null;
    try {
      for (const f of fs.readdirSync(LOG_DIR)) {
        if (/^openclaw-\d{4}-\d{2}-\d{2}\.log$/.test(f) && (!best || f > best)) best = f;
      }
    } catch {}
    return best ? path.join(LOG_DIR, best) : null;
  }

  async function tick() {
    if (busy) return;
    busy = true;
    try {
      const f = todayLog();
      if (!f) return;
      const size = fs.statSync(f).size;
      let off = offsets.get(f);
      if (off === undefined) off = Math.max(0, size - 2_000_000);   // 首次:回看最近 ~2MB(覆盖重启前刚被挡的人)
      if (size < off) off = 0;
      if (size === off) return;
      const fd = fs.openSync(f, "r");
      const buf = Buffer.alloc(Math.min(size - off, 8_000_000));
      fs.readSync(fd, buf, 0, buf.length, off);
      fs.closeSync(fd);
      const text = buf.toString("utf8");
      const lastNl = text.lastIndexOf("\n");
      if (lastNl < 0) return;
      offsets.set(f, off + Buffer.byteLength(text.slice(0, lastNl + 1)));
      const rejected = [];
      for (const line of text.slice(0, lastNl).split("\n")) {
        let m = RECV_RE.exec(line);
        if (m) { recent.set(m[2], { mid: m[1], chat: m[3], at: Date.now() }); continue; }
        m = REJ_RE.exec(line);
        if (m) rejected.push(m[1]);
      }
      for (const id of new Set(rejected)) {
        try { await handleRejected(id); } catch (e) { log("warn", `auto-admit ${id} failed: ${String(e).slice(0, 120)}`); }
      }
    } catch (e) {
      log("warn", `auto-admit tick failed: ${String(e).slice(0, 120)}`);
    } finally {
      busy = false;
    }
  }

  const timer = setInterval(tick, TICK_MS);
  if (typeof timer.unref === "function") timer.unref();
  setTimeout(tick, 2_000).unref?.();
  return { tick, stop: () => clearInterval(timer) };
}

module.exports = { start };
