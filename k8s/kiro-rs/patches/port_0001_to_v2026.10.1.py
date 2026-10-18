#!/usr/bin/env python3
"""把 0001-carher-base-models 移植到上游 v2026.10.1 (22f36e69)。

上游已自带 opus-5.5 映射 / 窗口 / 单测 / /v1/models 条目和 fable-5.1，
这里只补上游没有的：透传表、kiro- 前缀、sonnet-5.5、对应单测与 /v1/models 条目。
每处替换都断言恰好命中一次，上游再变就直接失败而不是静默错位。
"""
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
patch = Path(sys.argv[2]).read_text(encoding="utf-8")


def sub_once(text, old, new, what):
    n = text.count(old)
    if n != 1:
        sys.exit(f"[{what}] 期望命中 1 次，实际 {n} 次")
    return text.replace(old, new)


def added_block(start_marker, end_marker):
    """从 0001 里取一段新增行（去掉行首 '+'），[start, end) 按行内容定位。"""
    lines = patch.splitlines()
    s = next(i for i, l in enumerate(lines) if l.startswith("+") and start_marker in l)
    e = next(i for i in range(s + 1, len(lines)) if end_marker in lines[i])
    return "\n".join(l[1:] for l in lines[s:e] if l.startswith("+")) + "\n"


# ---------- converter.rs ----------
p = root / "src/anthropic/converter.rs"
s = p.read_text(encoding="utf-8")

helpers = added_block("/// Kiro 上游的非 Claude 模型", "pub fn map_model")
s = sub_once(s, "pub fn map_model(model: &str) -> Option<String> {\n    let model_lower = model.to_lowercase();\n",
             helpers + "pub fn map_model(model: &str) -> Option<String> {\n    let model_lower = model.to_lowercase();\n"
             "    let model_lower = strip_kiro_alias_prefix(&model_lower).to_string();\n"
             "    let model_lower = model_lower.as_str();\n\n"
             "    if let Some((id, _)) = passthrough_model(model_lower) {\n"
             "        return Some((*id).to_string());\n    }\n",
             "map_model 开头")

s = sub_once(s, """    } else if model_lower.contains("sonnet") {
        if model_lower.contains("sonnet-5") {""",
             """    } else if model_lower.contains("sonnet") {
        // ⚠️ 顺序敏感：`sonnet-5-5` / `sonnet-5.5` 必须排在 `sonnet-5` 之前。
        // `contains("sonnet-5")` 对 "claude-sonnet-5-5" 同样为真，放后面会被静默
        // 劫持成 claude-sonnet-5（HTTP 200 + 原样回显请求名 + in_tok 同族，
        // 外观与成功完全同形）。2026-10-02 实测过这个劫持：发 sonnet-5-5 得 200、
        // in_tok 6475 与 sonnet-5 同族。判落点只认 RUST_LOG=debug 的上游 modelId。
        if model_lower.contains("sonnet-5-5") || model_lower.contains("sonnet-5.5") {
            Some("claude-sonnet-5.5".to_string())
        } else if model_lower.contains("sonnet-5") {""",
             "sonnet-5.5 分支")

s = sub_once(s, "pub fn get_context_window_size(model: &str) -> i32 {\n    match map_model(model) {",
             "pub fn get_context_window_size(model: &str) -> i32 {\n"
             "    if let Some((_, window)) = passthrough_model(&model.to_lowercase()) {\n"
             "        return *window;\n    }\n\n    match map_model(model) {",
             "窗口透传")
s = sub_once(s, '                || mapped == "claude-sonnet-5"\n',
             '                || mapped == "claude-sonnet-5"\n                || mapped == "claude-sonnet-5.5"\n',
             "sonnet-5.5 窗口")
s = sub_once(s, "/// Sonnet 5 / Opus 4.7 / 4.8 / Opus 5 / 5.5 / Fable 5.1 同 1M",
             "/// Sonnet 5 / 5.5 / Opus 4.7 / 4.8 / Opus 5 / 5.5 / Fable 5.1 同 1M", "窗口注释")

# 单测：透传 / 前缀 / sonnet-5.5（opus-5.5 单测上游已有，不再加）
tests_a = added_block("fn test_map_model_passthrough_non_claude", "fn test_map_model_thinking_suffix_sonnet")
# added_block 从 fn 行起取，它上一行的 #[test] 是 patch 的新增行但落在区间外 ⇒ 手动补回
tests_a = "    #[test]\n" + tests_a
# 上游已支持 fable-5.1：旧断言「fable 不存在」作废，改成正向断言
tests_a = sub_once(tests_a, '        assert_eq!(map_model("claude-fable-5"), None);\n', "", "rejects_unknown 去 fable")
tests_a = sub_once(tests_a, '        assert_eq!(map_model("kiro-claude-fable-5-1"), None);\n',
                   '        // 前缀只是剥掉，落点与裸名一致（上游 10-08 起支持 fable-5.1）\n'
                   '        assert_eq!(map_model("kiro-claude-fable-5-1").as_deref(), Some("claude-fable-5.1"));\n',
                   "前缀 fable 断言")
tests_b = added_block("fn test_map_model_sonnet_5_5()", "fn test_map_model_sonnet_5()")

anchor = "    #[test]\n    fn test_map_model_thinking_suffix_sonnet() {"
s = sub_once(s, anchor, tests_a.rstrip("\n") + "\n\n" + anchor, "插单测 A")
anchor = "    #[test]\n    fn test_map_model_sonnet_5() {"
tests_b = "    #[test]\n" + tests_b
s = sub_once(s, anchor, tests_b.rstrip("\n") + "\n\n" + anchor, "插单测 B")
for m in re.finditer(r"\n(.*)\n    fn (test_\w+)\(", s):
    if m.group(1).strip() != "#[test]":
        sys.exit(f"{m.group(2)} 上一行不是 #[test]: {m.group(1)!r}")
p.write_text(s, encoding="utf-8")

# ---------- handlers.rs ----------
p = root / "src/anthropic/handlers.rs"
s = p.read_text(encoding="utf-8")
models = added_block("// Kiro 上游的非 Claude 模型，id 直接透传", 'id: "claude-opus-5-5".to_string()')
# 去掉末尾那个 opus-5-5 条目开头残留的 "Model {"（上游已有 opus-5-5 条目）
models = re.sub(r"\s*Model \{\s*$", "\n", models)
s = sub_once(s, "    let models = vec![\n", "    let models = vec![\n" + models, "/v1/models")
p.write_text(s, encoding="utf-8")
print("port 0001 ok")
