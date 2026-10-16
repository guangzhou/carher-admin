// keepmine-fill(v2)离线台架:从**真打完的 bundle**里原样切出插入段,在假 this 上跑。
//   node keepfill_offline_cases.js <已打补丁的 workbench.desktop.main.js>
// 场景对应 10-16 两个现场:liangjingyu(服务端目录缺 20 个名字)、wangzhongwei(名字在但没档位,opus 不能换挡)。
"use strict";
const fs = require("fs"), path = require("path");
const bundle = fs.readFileSync(process.argv[2], "utf8");
const i = bundle.indexOf("/*@cxteam-keepfill2*/");
if (i < 0) { console.log("FAIL 产物里没有 @cxteam-keepfill2"); process.exit(1); }
const END = "}catch(__cxe){}})(),";
const raw = bundle.slice(i, bundle.indexOf(END, i) + END.length - 1);
const v = /__cxh=new Map\(([A-Za-z_$][\w$]*)\.map/.exec(raw)[1];
const tiers = JSON.parse(fs.readFileSync(path.join(__dirname, "cursor_tier_catalog.json"), "utf8"));
const setup = fs.readFileSync(path.join(__dirname, "cursor_team_setup.js"), "utf8");
// 菜单常量里夹着 /* */ 和 // 注释 ⇒ 不 JSON.parse,只抠引号里的名字(同 setup_impl_parity.py 的口径)
const menuBlock = /const DEFAULT_MODELS = \[([\s\S]*?)\];/.exec(setup)[1].replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
const ours = [...menuBlock.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
function run(uam, cat) {
  delete globalThis.__cxTiers;
  const self = { _reactiveStorageService: { applicationUserPersistentStorage: { aiSettings: { userAddedModels: uam } } } };
  const c = cat.map((x) => ({ ...x }));
  new Function(v, raw + ";return " + v).call(self, c);
  return c;
}
let fail = 0;
const ok = (b, m) => { console.log((b ? "  PASS " : "  FAIL ") + m); if (!b) fail++; };
const by = (c) => Object.fromEntries(c.map((e) => [e.name, e]));
const bare = (n) => ({ name: n, isUserAdded: true, parameterDefinitions: [], variants: [], serverModelName: n });
const builtin = ["default", "composer-2", "auto-a", "auto-b", "auto-c"].map((n) => ({ name: n, defaultOn: true }));
ok(ours.length === 39 && Object.keys(tiers).length >= 20, "输入:菜单 39 个、档位模板 " + Object.keys(tiers).length + " 个");
ok(Object.keys(tiers).every((n) => ours.includes(n)), "档位模板里的名字都在菜单里(没有过期名)");
// ① wangzhongwei:名字都在、没档位
const wz = ours.map(bare).concat(builtin), o = by(run(ours, wz));
ok(o["claude-opus-5-5"].variants.length === tiers["claude-opus-5-5"].variants.length && o["claude-opus-5-5"].variants.length > 0, "无档位 ⇒ 补回档位(opus-5-5 " + o["claude-opus-5-5"].variants.length + " 个)");
ok(o["claude-opus-5-5"].parameterDefinitions.some((p) => p.id === "effort"), "有 effort 参数(换挡下拉的来源)");
ok(o["claude-opus-5-5"].isUserAdded === true, "补完仍是自定义条目");
ok(Object.keys(o).length === wz.length, "条目数不变");
ok(o["gpt-6-sol"].variants.length === 0, "模板里没有的名字不乱补");
// ② liangjingyu:名字缺
const lj = wz.filter((e) => !["claude-opus-5-5", "gpt-5.5"].includes(e.name)), l = by(run(ours, lj));
ok(l["claude-opus-5-5"] && l["claude-opus-5-5"].variants.length > 0, "缺的名字补回时直接带档位");
// ③ 目录自带档位 ⇒ 不动
const full = ours.map((n) => (tiers[n] ? { name: n, isUserAdded: true, ...tiers[n], orig: 1 } : bare(n)));
ok(JSON.stringify(run(ours, full)) === JSON.stringify(full), "目录已有档位 ⇒ 一字不动(服务端数据优先)");
// ④ 健壮性
let threw = false; try { new Function(v, raw + ";return " + v).call({ _reactiveStorageService: null }, wz.slice()); } catch (e) { threw = true; }
ok(!threw, "取不到存储 ⇒ 不抛,不打断 Cursor 刷目录");
ok(run([null, 3, ""], builtin).length === builtin.length, "坏数据 ⇒ 跳过");
// ⑤ 阳性对照
ok(wz.find((e) => e.name === "claude-opus-5-5").variants.length === 0, "阳性对照:原状态 opus 无档位");
console.log(fail ? "FAIL " + fail : "ALL PASS");
process.exit(fail ? 1 : 0);
