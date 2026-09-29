// cursor_keysan_offline_cases.js —— 「Key 里的不可见字符」这一腿。
//
// 病(2026-09-29 同事实报):Cursor 里
//   `Local agent request failed / Cannot convert argument to a ByteString
//    because the character at index 32 has a value of 8203 which is greater than 255`
// 8203 = U+200B。那是 WHATWG `Headers` 抛的 ⇒ **请求一个字节都没出网**,
// 小代理 / 198 / 模型全无关。飞书 key 表 1374 行 key 全长 25,"Bearer "(7)+25 = 32。
//
// 这腿量三件事,每件都带阳性对照:
//   A 真 `new Headers()`:脏 key 必抛 + 报错里的 index 与我们算的一致(不是我们自己写的字符串比较)
//   B sanitizeKey 剥完之后 `new Headers()` 必须不抛,且 clean 与原 key 逐字节相同
//   C 阳性对照 = **就地重实现旧行为**(只 `trim()`),它必须**仍然抛** ——
//     旧行为一旦"也绿了",说明这腿在量别的东西。
//   D scanInvisible 报的 index 是**字符下标**,与 Headers 的 index 差正好 7
//
// 🔴 阳性对照按 skill 的规矩在本腿里就地重写,不许 `git show HEAD:...`:
//    修好的版本一进 HEAD,那种对照就退化成"新 vs 新",永远绿。
//
// 用法:CX_SETUP_JS=…/cursor_team_setup.js node cursor_keysan_offline_cases.js
const fs = require("fs"), path = require("path"), vm = require("vm");

const SRC = process.env.CX_SETUP_JS || path.join(__dirname, "cursor_team_setup.js");
if (!fs.existsSync(SRC)) { console.error("需要 CX_SETUP_JS 指向 cursor_team_setup.js"); process.exit(2); }
const code = fs.readFileSync(SRC, "utf8");

// 从真源码里**切出**那三样再 vm 里跑 —— 不复制粘贴一份副本:
// 副本会漂,漂了这腿照样绿,等于没量线上那份。
function slice(startRe, label) {
  const m = code.match(startRe);
  if (!m) { console.error("FAIL 取不到 " + label + " ⇒ 尺子不可信(源码里改名了?)"); process.exit(3); }
  // 从匹配处起,按大括号配平切到函数/对象字面量结束。
  let i = m.index, depth = 0, started = false;
  for (; i < code.length; i++) {
    const c = code[i];
    if (c === "{") { depth++; started = true; }
    else if (c === "}") { depth--; if (started && depth === 0) return code.slice(m.index, i + 1); }
  }
  console.error("FAIL " + label + " 大括号没配平"); process.exit(3);
}
const src = [
  slice(/const INVIS_CP = \{/, "INVIS_CP"),
  slice(/function scanInvisible\(/, "scanInvisible"),
  slice(/function sanitizeKey\(/, "sanitizeKey"),
].join(";\n") + ";";
const ctx = {};
vm.runInNewContext(src + "\nthis.scanInvisible=scanInvisible;this.sanitizeKey=sanitizeKey;this.INVIS_CP=INVIS_CP;", ctx);
const { scanInvisible, sanitizeKey } = ctx;

// 旧行为(阳性对照):安装器原来就是把 rawKey 原样加密落盘,只有交互那一步 trim 过。
const OLD_sanitize = (raw) => String(raw).trim();

let pass = 0, fail = 0;
const T = (name, ok, why) => {
  if (ok) { pass++; console.log("  PASS " + name); }
  else { fail++; console.log("  FAIL " + name + (why ? " — " + why : "")); }
};
// 真 Headers,不是我们自己判的。返回 null=没抛,否则返回 {index, value}
function headersThrow(v) {
  try { new Headers({ authorization: "Bearer " + v }); return null; }
  catch (e) {
    const m = /index (\d+) has a value of (\d+)/.exec(e.message || "");
    return { index: m ? +m[1] : null, value: m ? +m[2] : null, msg: e.message };
  }
}

// 🔴 这里**只许放合成值**。09-29 第一版我随手粘了一把"看起来像样本"的 key 并在注释里
//    写"值是假的" —— 提交前扫了一遍飞书快照,它在 rows.ndjson 里**命中 2 行**,是一把真 key。
//    注释不是证据:判据是拿候选串去 `.tmp-lark/*.ndjson` 里 grep,命中必须为 0。
//    `FAKE` 前缀是给下一个人看的:一眼能认出它不是从表里复制来的。
//    长度必须仍是 25(与飞书表 1374 行同形),否则 D 段那些下标算术前提就不成立。
const CLEAN = "sk-FAKEvFkdB_eHJj6UMf-2xE";
if (CLEAN.length !== 25) { console.error("样本 key 不是 25 字符,算术前提变了"); process.exit(3); }
if (!CLEAN.startsWith("sk-FAKE")) { console.error("样本 key 必须是合成值(sk-FAKE 前缀),不许从真表里粘"); process.exit(3); }

console.log("--- A 真 Headers:干净 key 不抛(阳性对照:这把尺子会不抛) ---");
T("干净 key 通过 Headers", headersThrow(CLEAN) === null);
T("干净 key 过 sanitizeKey 后逐字节不变", sanitizeKey(CLEAN).clean === CLEAN);
T("干净 key 的 stripped = 0", sanitizeKey(CLEAN).stripped.length === 0);

console.log("--- B 脏 key:复现同事那一发(ZWSP 贴在 key 尾) ---");
const DIRTY_TAIL = CLEAN + "​";
const hTail = headersThrow(DIRTY_TAIL);
T("脏 key 必抛 ByteString", !!hTail, "居然没抛");
T("报错 index = 32(与同事一致)", hTail && hTail.index === 32, hTail && "got index=" + hTail.index);
T("报错 value = 8203(与同事一致)", hTail && hTail.value === 8203, hTail && "got value=" + hTail.value);
T("剥完 Headers 不抛", headersThrow(sanitizeKey(DIRTY_TAIL).clean) === null);
T("剥完 == 干净 key", sanitizeKey(DIRTY_TAIL).clean === CLEAN);
T("剥完报了 1 个", sanitizeKey(DIRTY_TAIL).stripped.length === 1);

console.log("--- C 阳性对照:旧行为(只 trim)必须**仍然抛** ---");
// 🔴 这四条是这腿的承重结构:trim() 去不掉零宽字符,所以旧行为必须一条都救不了。
// 任何一条变成"旧行为也不抛",说明样本被改成了 trim 能处理的普通空白 ⇒ 这腿在量别的东西。
for (const [name, dirty] of [
  ["尾部 ZWSP", CLEAN + "​"],
  ["首部 ZWSP", "​" + CLEAN],
  ["中间 ZWSP", CLEAN.slice(0, 10) + "​" + CLEAN.slice(10)],
  ["首尾空格 + 尾部 ZWSP", "  " + CLEAN + "​  "],
]) {
  T("旧行为(trim)对「" + name + "」仍抛", headersThrow(OLD_sanitize(dirty)) !== null,
    "旧行为居然救回来了 ⇒ 样本不对");
  T("新行为(sanitizeKey)对「" + name + "」不抛 且 == 干净 key",
    headersThrow(sanitizeKey(dirty).clean) === null && sanitizeKey(dirty).clean === CLEAN);
}

console.log("--- D index 对账:scanInvisible 的字符下标 + 7 == Headers 的 index ---");
for (const at of [0, 1, 10, 24, 25]) {
  const dirty = CLEAN.slice(0, at) + "​" + CLEAN.slice(at);
  const h = headersThrow(dirty);
  const s = scanInvisible(dirty);
  T("ZWSP 在第 " + at + " 位:" + (s[0] ? s[0].index : "?") + " + 7 == " + (h ? h.index : "?"),
    !!h && s.length === 1 && s[0].index + 7 === h.index,
    "scan=" + JSON.stringify(s) + " headers=" + JSON.stringify(h));
}

console.log("--- E 不止 ZWSP:凡 Headers 抛的,扫描器都得抓到(白名单会漏) ---");
/* 判据是**超集**,不是"同进同退":
   - `Headers` 只在码位 > 255 时抛。NBSP(U+00A0)≤ 255 ⇒ **它不抛**,
     key 照样出网、照样是错的,回来是 401 —— 与 ByteString 是**两个失败面**,
     不许合并计数。所以扫描器必须比 Headers 更严:它抓的是"这把 key 脏了",
     不是"Headers 会不会抛"。
   - 反向必须成立:Headers 抛 ⇒ 扫描器一定抓到。漏一个就等于 doctor 那格会假绿。 */
for (const [name, ch, expectThrow] of [
  ["NBSP U+00A0", " ", false],        // ≤255:不抛,但 key 是错的 ⇒ 静默 401
  ["全角空格 U+3000", "　", true], ["BOM U+FEFF", "﻿", true],
  ["ZWNJ U+200C", "‌", true], ["WORD JOINER U+2060", "⁠", true],
  ["全角引号 U+201C", "“", true], ["CJK 汉字 U+4E2D", "中", true],
  ["LRM U+200E", "‎", true],
]) {
  const dirty = CLEAN + ch;
  const threw = headersThrow(dirty) !== null;
  T(name + ":Headers 抛=" + threw + "(预期 " + expectThrow + ")", threw === expectThrow);
  // 超集:抛不抛都得抓到 —— 抓到才有人告诉他 key 脏了。
  T(name + ":扫描器抓到(超集,与抛不抛无关)", scanInvisible(dirty).length > 0);
  T(name + ":剥完不抛且 == 干净 key",
    headersThrow(sanitizeKey(dirty).clean) === null && sanitizeKey(dirty).clean === CLEAN);
}
// 反向闭环:Headers 抛 ⇒ 扫描器必抓。随机扫一遍 0x100..0x2FFF 的码位,一个反例都不许有。
{
  let missed = [];
  for (let cp = 0x100; cp <= 0x2fff; cp += 7) {
    const dirty = CLEAN + String.fromCodePoint(cp);
    if (headersThrow(dirty) !== null && scanInvisible(dirty).length === 0) missed.push(cp);
  }
  T("0x100..0x2FFF 抽样:Headers 抛而扫描器漏的 = 0", missed.length === 0,
    "漏了 " + missed.slice(0, 5).map((c) => "U+" + c.toString(16)).join(","));
}

console.log("--- F 边界:全是空白 / 空串 不许当成「有 Key」 ---");
T("全零宽 ⇒ clean 空", sanitizeKey("​​﻿").clean === "");
T("空串 ⇒ clean 空", sanitizeKey("").clean === "");
T("只空格 ⇒ clean 空", sanitizeKey("   ").clean === "");
// 代理对(emoji)不许被劈成两半:for..of 按码位走才对。
T("代理对按一个码位计数", scanInvisible("a\u{1F600}b").length === 1);

console.log("\n" + (fail ? "🔴 FAIL=" + fail + " PASS=" + pass : "✅ 全绿 PASS=" + pass));
process.exit(fail ? 1 : 0);
