/** auto-admit 离线测试:假日志 + 假飞书。跑: node scripts/feishu-ext-login/test_auto_admit.js */
"use strict";
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const OK_TK = "153f0ee88f0a575e", BAD_TK = "ffffbadtenant000";
const A = "ou_" + "a".repeat(32), B = "ou_" + "b".repeat(32);

function setup(tenants = [OK_TK]) {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "autoadmit-"));
  const logs = path.join(d, "logs"); fs.mkdirSync(logs);
  fs.writeFileSync(path.join(d, "allow.json"), JSON.stringify({ allow: [] }));
  fs.writeFileSync(path.join(d, "dm.json"), JSON.stringify({ allowFrom: ["ou_existing"] }));
  if (tenants) fs.writeFileSync(path.join(d, "allow-tenants.json"), JSON.stringify({ tenants }));
  const lines = [
    `{"message":"feishu[default][msg:om_${"1".repeat(20)}]: received from ${A} in oc_${"c".repeat(20)} (p2p)"}`,
    `{"message":"feishu[default]: sender ${A} not in DM allowlist"}`,
    `{"message":"feishu[default][msg:om_${"2".repeat(20)}]: received from ${B} in oc_${"d".repeat(20)} (p2p)"}`,
    `{"message":"feishu[default]: sender ${B} not in DM allowlist"}`,
  ];
  fs.writeFileSync(path.join(logs, "openclaw-2026-11-05.log"), lines.join("\n") + "\n");
  return { d, logs };
}

async function run(tenants) {
  const { d, logs } = setup(tenants);
  process.env.CARHER_EXT_LOGIN_LOG_DIR = logs;
  delete require.cache[require.resolve("./plugin/carher-ext-login/auto-admit.js")];
  const AA = require("./plugin/carher-ext-login/auto-admit.js");
  const sent = [], audits = [];
  const httpJson = async (url, opts) => {
    if (url.includes("/im/v1/messages/om_1")) return { status: 200, body: { code: 0, data: { items: [{ sender: { id: A, tenant_key: OK_TK } }] } } };
    if (url.includes("/im/v1/messages/om_2")) return { status: 200, body: { code: 0, data: { items: [{ sender: { id: B, tenant_key: BAD_TK } }] } } };
    if (url.includes("/im/v1/messages?receive_id_type")) { sent.push(JSON.parse(opts.body)); return { status: 200, body: { code: 0 } }; }
    throw new Error("unexpected " + url);
  };
  const h = AA.start({ stateDir: d, dmStoreFile: path.join(d, "dm.json"), allowFile: path.join(d, "allow.json"),
    feishuBase: "http://f", tenantToken: async () => "t", httpJson, audit: (e) => audits.push(e), log: () => {} });
  h.stop();
  await h.tick();
  const dm = JSON.parse(fs.readFileSync(path.join(d, "dm.json"))).allowFrom;
  const al = JSON.parse(fs.readFileSync(path.join(d, "allow.json"))).allow;
  return { dm, al, sent, audits };
}

(async () => {
  let r = await run([OK_TK]);
  assert.ok(r.dm.includes(A) && r.al.includes(A), "合作公司的人应被加进两份名单");
  assert.ok(!r.dm.includes(B) && !r.al.includes(B), "非合作公司的人不许放");
  assert.ok(r.dm.includes("ou_existing"), "原有名单不许丢");
  assert.strictEqual(r.sent.length, 1, "只给被放行的人发通知");
  assert.ok(r.audits.some((e) => e.ev === "auto_admitted" && e.open_id === A));
  assert.ok(r.audits.some((e) => e.ev === "auto_admit_skipped" && e.open_id === B));
  console.log("OK   合作公司自动放行,其他公司不放,原名单保留");

  r = await run(null);
  assert.ok(!r.dm.includes(A) && r.sent.length === 0, "没有公司名单文件 ⇒ 谁都不放(失败关闭)");
  console.log("OK   阴性对照:没配公司名单 ⇒ 不放任何人");
  console.log("\nALL PASS (2 checks)");
})().catch((e) => { console.log("FAIL", e.message); process.exit(1); });
