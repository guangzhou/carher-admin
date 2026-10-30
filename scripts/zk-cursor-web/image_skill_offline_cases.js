// Cursor 一键「画图」那一步(installImageSkill)的离线台架:临时 HOME,不碰本机 ~/.cursor。
//   ELECTRON_RUN_AS_NODE=1 Cursor image_skill_offline_cases.js   (或直接 node)
"use strict";
const fs = require("fs"), os = require("os"), path = require("path");
const src = fs.readFileSync(path.join(__dirname, "cursor_team_setup.js"), "utf8");
const body = src.slice(src.indexOf("function installImageSkill("), src.indexOf("\nmain().catch"));
let fail = 0;
const ok = (b, m) => { console.log((b ? "  PASS " : "  FAIL ") + m); if (!b) fail++; };
function run(key, base) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cximg-"));
  const fakeOs = { ...os, homedir: () => home };
  const pkg = path.join(__dirname, "..", "..", "codex-oneclick", "src");
  // 10-28 起 skill 里的命令改用 cursorBin()(不再直接读 EXEC);台架照样注入一个假的,不然整腿 ReferenceError
  const fn = new Function("fs", "path", "os", "__dirname", "EXEC", "cursorBin", "DEFAULT_BASE_URL", "console", body + ";return installImageSkill;")(
    fs, path, fakeOs, pkg, "/X/Cursor", () => "/X/Cursor", "https://gw.example/pro/v1", { log() {} });
  fn(key, base);
  return home;
}
const h = run(" sk-test0000​ ");
const cfg = JSON.parse(fs.readFileSync(path.join(h, ".carher", "image.json"), "utf8"));
ok(cfg.key === "sk-test0000" && cfg.base === "https://gw.example/pro/v1", "Key 去空白/零宽后存进 image.json,网关地址来自安装器常量");
ok(process.platform === "win32" || (fs.statSync(path.join(h, ".carher", "image.json")).mode & 0o777) === 0o600, "image.json 权限 600");
const sk = fs.readFileSync(path.join(h, ".cursor", "skills", "carher-image", "SKILL.md"), "utf8");
ok(!sk.includes("@@") && sk.includes('ELECTRON_RUN_AS_NODE=1 "/X/Cursor" "' + path.join(h, ".carher", "carher_image.js") + '"'), "skill 里的命令用 Cursor 二进制跑工具,占位符都替换掉");
ok(fs.existsSync(path.join(h, ".carher", "carher_image.js")), "工具文件已复制");
// 10-29 换域名:安装时新地址不通、退到旧地址 ⇒ image.json 记实际用的那个(刷模型以后再换);不传 = 默认新地址
const h3 = run("sk-test0000", "https://old.example/pro/v1");
ok(JSON.parse(fs.readFileSync(path.join(h3, ".carher", "image.json"), "utf8")).base === "https://old.example/pro/v1", "传了实际用的地址 ⇒ image.json 记它");
const h2 = run("");
ok(!fs.existsSync(path.join(h2, ".carher", "image.json")) && fs.existsSync(path.join(h2, ".cursor", "skills", "carher-image", "SKILL.md")), "--upgrade 没给 Key ⇒ 只装 skill,不写空 Key");
console.log(fail ? "FAIL " + fail : "ALL PASS");
process.exit(fail ? 1 : 0);
