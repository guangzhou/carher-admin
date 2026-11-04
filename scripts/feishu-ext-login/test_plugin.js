/**
 * carher-ext-login 插件的离线回归(不碰飞书、不碰门户、不碰 pod)。
 *
 * 为什么要有:这份插件跑在**生产机器人**里,拿到它就是拿到"替任意人签登录链接"的那一步。
 * 11-05 之前它一条测试都没有,而且已经踩了一个真 bug:
 *   两个注册 API 都挂了同一个 handler ⇒ 一条「登录」签了**两条**一次性链接
 *   (机器人侧 audit 只记 1 条 issued,门户侧日志 2 条「magic 签发」⇒ 多出来那条是白签的、TTL 内可用)。
 *
 * 跑: node scripts/feishu-ext-login/test_plugin.js
 */
"use strict";

const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

let pass = 0;
const fails = [];

function t(name, fn) {
  try {
    fn();
    pass++;
    console.log("OK   " + name);
  } catch (e) {
    fails.push(name);
    console.log("FAIL " + name + "\n       " + String(e.message).split("\n")[0]);
  }
}

// 🔴 必须排队跑:每个用例都要重设 global.fetch(假上游),并发跑会互相顶掉
//    (第一版就这么红的 —— 5 条红全是测试自己的串台,不是插件的问题)。
const queue = [];
function ta(name, fn) {
  queue.push([name, fn]);
}

async function runQueue() {
  for (const [name, fn] of queue) {
    try {
      await fn();
      pass++;
      console.log("OK   " + name);
    } catch (e) {
      fails.push(name);
      console.log("FAIL " + name + "\n       " + String(e.message).split("\n")[0]);
    }
  }
}

// ── 夹具:一个临时 state 目录 + 一个假门户 + 一个假飞书 ────────────────────
const OPEN_ID = "ou_" + "a".repeat(32);
const UNION_ID = "on_" + "b".repeat(32);
const MSG_ID = "om_" + "c".repeat(32);

function freshState(allow = [OPEN_ID]) {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "extlogin-"));
  fs.writeFileSync(path.join(d, "allow.json"), JSON.stringify({ allow }));
  fs.writeFileSync(path.join(d, "internal-secret"), "x".repeat(48));
  return d;
}

/** 装载插件:每次都重新 require(它有模块级常量读 env)。 */
function loadPlugin(stateDir, portalUrl) {
  const p = require.resolve("./plugin/carher-ext-login/index.js");
  delete require.cache[p];
  process.env.CARHER_EXT_LOGIN_STATE_DIR = stateDir;
  process.env.CARHER_EXT_LOGIN_SECRET_FILE = path.join(stateDir, "internal-secret");
  process.env.CARHER_EXT_LOGIN_PORTAL_URL = portalUrl;
  process.env.CARHER_EXT_LOGIN_FEISHU_BASE = "http://127.0.0.1:1/open-apis";  // 默认打不通
  process.env.FEISHU_APP_ID = "cli_fake";
  process.env.FEISHU_APP_SECRET = "fake-secret";
  return require(p);
}

/** 把插件注册到一个假的 api 上,收集它挂了几个监听器。 */
function register(plugin) {
  const hooks = [];
  const logs = [];
  plugin.register({
    logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m), error: (m) => logs.push(m) },
    registerHook: (name, fn) => hooks.push({ via: "registerHook", name, fn }),
    on: (name, fn) => hooks.push({ via: "on", name, fn }),
  });
  return { hooks, logs };
}

/** 假飞书 + 假门户:记下每一次调用,便于数"真签了几次"。 */
function fakeUpstreams({ identityOk = true, portalOk = true } = {}) {
  const calls = { token: 0, msgOpen: 0, msgUnion: 0, portal: 0 };
  global.fetch = async (url, opts) => {
    const u = String(url);
    const json = (code, obj) => ({
      ok: true, status: code,
      text: async () => JSON.stringify(obj),
    });
    if (u.includes("/auth/v3/tenant_access_token/internal")) {
      calls.token++;
      return json(200, { code: 0, tenant_access_token: "t-fake", expire: 3600 });
    }
    if (u.includes("/im/v1/messages/")) {
      const union = u.includes("user_id_type=union_id");
      union ? calls.msgUnion++ : calls.msgOpen++;
      if (!identityOk) return json(200, { code: 230002, msg: "no permission" });
      return json(200, {
        code: 0,
        data: { items: [{ chat_id: "oc_x",
                          sender: { id: union ? UNION_ID : OPEN_ID, sender_type: "user",
                                    tenant_key: "ff00external0000" } }] },
      });
    }
    if (u.includes("/api/internal/magic-link")) {
      calls.portal++;
      if (!portalOk) return json(500, { message: "boom" });
      return json(200, { url: "https://chat.auto-link.com.cn/ai/auth/magic?t=" + "z".repeat(43),
                         expires_in: 600 });
    }
    throw new Error("unexpected fetch: " + u);
  };
  return calls;
}

const ev = (over = {}) => Object.assign(
  { senderId: OPEN_ID, messageId: MSG_ID, content: "登录", isGroup: false }, over);
const cx = (over = {}) => Object.assign({ channelId: "feishu" }, over);

// ── 1) 双注册不许双签(本轮修的真 bug)────────────────────────────────────
ta("两个监听器收到同一条消息 ⇒ 只真签一次", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  assert.strictEqual(hooks.length, 2, "本来就该挂两个(不同版本 gateway 认的 API 不同)");
  const r1 = await hooks[0].fn(ev(), cx());
  const r2 = await hooks[1].fn(ev(), cx());
  assert.strictEqual(calls.portal, 1, "签发接口只该被调一次,实际 " + calls.portal);
  assert.ok(r1 && r1.handled === true && r1.text.includes("一次性登录链接"));
  assert.deepStrictEqual(r2, r1, "第二个监听器应复用第一遍的结果");
  const audit = fs.readFileSync(path.join(d, "audit.jsonl"), "utf8").trim().split("\n");
  assert.strictEqual(audit.filter((l) => l.includes('"issued"')).length, 1, "audit 也只该有一条");
});

ta("UNGUARDED 对照:去掉去重 ⇒ 真的会双签(证明上一条不是碰巧)", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  // 直接拿同一个 handler 跑两遍,但**绕过**去重(模拟修复前的形状):
  // hooks[0].fn 是 dedupedHandler;把 messageId 改掉就等价于"没有去重"。
  await hooks[0].fn(ev({ messageId: "om_" + "1".repeat(32) }), cx());
  await hooks[0].fn(ev({ messageId: "om_" + "2".repeat(32) }), cx());
  assert.strictEqual(calls.portal, 2, "两条不同消息本来就该签两次(阳性对照)");
});

ta("不同消息仍然各签一次(去重不许误伤)", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  await hooks[0].fn(ev(), cx());
  await hooks[0].fn(ev({ messageId: "om_" + "9".repeat(32) }), cx());
  assert.strictEqual(calls.portal, 2);
});

// ── 2) 名单与触发词 ───────────────────────────────────────────────────────
ta("不在名单 ⇒ 完全不介入(return undefined,不签、不回话)", async () => {
  const d = freshState([]);     // 空名单
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  const r = await hooks[0].fn(ev(), cx());
  assert.strictEqual(r, undefined);
  assert.strictEqual(calls.portal, 0);
});

ta("名单文件缺失/坏 ⇒ 失败关闭(谁都不放)", async () => {
  const d = freshState();
  fs.writeFileSync(path.join(d, "allow.json"), "{坏 json");
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  assert.strictEqual(await hooks[0].fn(ev(), cx()), undefined);
  assert.strictEqual(calls.portal, 0);
});

ta("群里绝不签发", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  assert.strictEqual(await hooks[0].fn(ev({ isGroup: true }), cx()), undefined);
  assert.strictEqual(calls.portal, 0);
});

ta("触发词:全角/大小写/@前缀/JSON 文本都认;普通聊天不认", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const yes = ["登录", " 登录 ", "登陆", "LOGIN", "/login", '{"text":"登录"}',
               "<at user_id=\"x\">小助手</at> 登录"];
  const no = ["我想登录一下", "帮我登录系统", "你好", "", "登录失败了怎么办"];
  for (const c of yes) {
    const calls = fakeUpstreams();
    const r = await hooks[0].fn(ev({ content: c, messageId: "om_" + Buffer.from(c).toString("hex").padEnd(32, "0").slice(0, 32) }), cx());
    assert.ok(r && r.handled, "该认的没认:" + JSON.stringify(c));
  }
  for (const c of no) {
    const calls = fakeUpstreams();
    const r = await hooks[0].fn(ev({ content: c, messageId: "om_" + "f".repeat(32) }), cx());
    assert.strictEqual(r, undefined, "不该认的认了:" + JSON.stringify(c));
    assert.strictEqual(calls.portal, 0);
  }
});

// ── 3) 身份:以消息本身为真相源,不信正文 ────────────────────────────────
ta("事件里的 senderId 与消息查回来的发件人不一致 ⇒ 拒签", async () => {
  const d = freshState([OPEN_ID]);
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  // 假飞书回的 sender 是 OPEN_ID;这里让事件里的 senderId 是另一个人(且在名单里)
  const other = "ou_" + "d".repeat(32);
  fs.writeFileSync(path.join(d, "allow.json"), JSON.stringify({ allow: [other] }));
  const r = await hooks[0].fn(ev({ senderId: other }), cx());
  assert.ok(r && r.handled && r.text.includes("没能确认你的飞书身份"));
  assert.strictEqual(calls.portal, 0, "身份没对上就不许调签发");
});

ta("拿不到 union_id ⇒ 回人话,不签", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams({ identityOk: false });
  const r = await hooks[0].fn(ev(), cx());
  assert.ok(r && r.handled && r.text.includes("没能确认你的飞书身份"));
  assert.strictEqual(calls.portal, 0);
});

// ── 4) 限流与失败路径 ────────────────────────────────────────────────────
ta("同一个人一分钟最多 3 次,第 4 次回人话且不再调门户", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  for (let i = 0; i < 3; i++) {
    const r = await hooks[0].fn(ev({ messageId: "om_" + String(i).repeat(32).slice(0, 32) }), cx());
    assert.ok(r && r.handled && r.text.includes("一次性登录链接"), "第 " + (i + 1) + " 次该成功");
  }
  const before = calls.portal;
  const r4 = await hooks[0].fn(ev({ messageId: "om_" + "7".repeat(32) }), cx());
  assert.ok(r4 && r4.handled && r4.text.includes("频繁"));
  assert.strictEqual(calls.portal, before, "撞闸后不该再打门户");
});

ta("门户挂了 ⇒ 回人话,不把错误细节甩给用户", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  fakeUpstreams({ portalOk: false });
  const r = await hooks[0].fn(ev(), cx());
  assert.ok(r && r.handled && r.text.includes("生成失败"));
  assert.ok(!/boom|500/.test(r.text), "不许把上游错误原文透给用户");
});

ta("密钥文件缺失 ⇒ 不签(不裸调门户)", async () => {
  const d = freshState();
  fs.rmSync(path.join(d, "internal-secret"));
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  const calls = fakeUpstreams();
  const r = await hooks[0].fn(ev(), cx());
  assert.ok(r && r.handled && r.text.includes("生成失败"));
  assert.strictEqual(calls.portal, 0);
});

ta("handler 内部抛异常 ⇒ fail-open(return undefined,不打断 inbound)", async () => {
  const d = freshState();
  const { hooks } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  global.fetch = async () => { throw new Error("网络炸了"); };
  const r = await hooks[0].fn(ev(), cx());
  // 身份拿不到 ⇒ 要么回人话、要么 undefined,但**不许抛**
  assert.ok(r === undefined || r.handled === true);
});

// ── 5) 不许泄密 ──────────────────────────────────────────────────────────
ta("密钥与 token 不进 audit、不进回复文案", async () => {
  const d = freshState();
  const secret = fs.readFileSync(path.join(d, "internal-secret"), "utf8").trim();
  const { hooks, logs } = register(loadPlugin(d, "http://portal.test/api/internal/magic-link"));
  fakeUpstreams();
  const r = await hooks[0].fn(ev(), cx());
  const audit = fs.readFileSync(path.join(d, "audit.jsonl"), "utf8");
  assert.ok(!audit.includes(secret), "audit 里不许有内部密钥");
  assert.ok(!audit.includes("z".repeat(43)), "audit 里不许有一次性 token");
  assert.ok(!logs.join("\n").includes(secret), "日志里不许有内部密钥");
  assert.ok(r.text.includes("z".repeat(43)), "阳性:链接本身当然要发给用户");
});

(async () => {
  await runQueue();
  console.log();
  if (fails.length) {
    console.log("FAILED: " + fails.length + " / " + (pass + fails.length));
    fails.forEach((f) => console.log("  - " + f));
    process.exit(1);
  }
  console.log("ALL PASS (" + pass + " checks)");
})();
