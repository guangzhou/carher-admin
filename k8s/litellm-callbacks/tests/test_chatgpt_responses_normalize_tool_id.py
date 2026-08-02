"""test_chatgpt_responses_normalize_tool_id.py — 工具调用 id 清洗的回归网。

背景（2026-08-02 生产故障）
--------------------------
198 prod 上 cursor / her 的 codex 流量频繁莫名落到 wangsu qwen 兜底。根因不在
号池、不在 key 配置：ChatGPT 的 Responses 上游**硬性要求** history 里
function_call item 的 ``id`` 以 ``fc`` 开头，而回放上来的是 ``call_<24hex>``，
于是整单 400::

    Invalid 'input[61].id': 'call_70eac2a105794171b50ace1f'.
    Expected an ID that begins with 'fc'.

``chatgpt_responses_normalize.py`` 本来就是为这类污染写的，但它的判据是
"以 ``toolu_`` 开头才重写" —— 一个**前缀白名单**。实测各兜底目标吐出的形状
各不相同（同一个 anthropic 网关，非流式给 ``toolu_``、流式给 ``call_``；
custom_openai/compat 条目给 ``call_``；kimi-k3 给 ``shell_0``），白名单必然漏。
codex 永远走流式，所以漏的恰好是最常见的那一种：3 小时 228 次报错，前缀
100% 是 ``call_``。

修法：判据从"是不是 toolu_"改成**"是不是已经 fc_"**——不是就确定性重写。
配对靠 ``call_id`` 不靠 ``id``，所以改写 ``id`` 不破坏 function_call 与
function_call_output 的配对。

本文件钉住修复后的行为，防止有人把判据改回前缀白名单。
用法::

    python3 test_chatgpt_responses_normalize_tool_id.py [被测文件路径]
"""
import importlib.util
import sys
import types

DEFAULT_PATH = "../chatgpt_responses_normalize.py"


def _load(path):
    """加载被测模块；litellm 不在时用 stub 顶掉，使本地也能跑回归。"""
    if "litellm.integrations.custom_logger" not in sys.modules:
        try:
            import litellm.integrations.custom_logger  # noqa: F401
        except Exception:
            litellm = types.ModuleType("litellm")
            integrations = types.ModuleType("litellm.integrations")
            custom_logger = types.ModuleType("litellm.integrations.custom_logger")

            class CustomLogger:  # minimal stand-in
                pass

            custom_logger.CustomLogger = CustomLogger
            litellm.integrations = integrations
            integrations.custom_logger = custom_logger
            sys.modules["litellm"] = litellm
            sys.modules["litellm.integrations"] = integrations
            sys.modules["litellm.integrations.custom_logger"] = custom_logger
    spec = importlib.util.spec_from_file_location("chatgpt_responses_normalize", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH)
TARGET = "chatgpt-gpt-5.6-terra"  # 命中 _TARGET_MODEL_PARTS
POISON = "call_70eac2a105794171b50ace1f"  # 生产实际报错的那个 id

_results = []


def check(name, cond, detail=""):
    _results.append((name, bool(cond), detail))


def norm(items, model=TARGET, **extra):
    data = {"model": model, "input": list(items)}
    data.update(extra)
    return MOD._normalize_data(data, "test")["input"]


def fc(id_, call_id=None, name="shell", **extra):
    item = {"type": "function_call", "name": name, "arguments": "{}"}
    if id_ is not None:
        item["id"] = id_
    if call_id is not None:
        item["call_id"] = call_id
    item.update(extra)
    return item


# --- 1. 生产故障本体：call_<24hex> 必须变 fc_ -------------------------------
out = norm([fc(POISON, POISON)])[0]
check("call_ 前缀的 id 被改成 fc_", out["id"] == "fc_70eac2a105794171b50ace1f", out.get("id"))
check("call_id 保持 call_ 前缀（上游只校验 id）", out["call_id"] == POISON, out.get("call_id"))

# --- 2. 向后兼容：原有 toolu_ 行为不能变 ------------------------------------
out = norm([fc("toolu_abc123", "toolu_abc123")])[0]
check("toolu_ 的 id 仍映射到 fc_", out["id"] == "fc_abc123", out.get("id"))
check("toolu_ 的 call_id 仍映射到 call_", out["call_id"] == "call_abc123", out.get("call_id"))

# --- 3. 其它上游形状（kimi-k3 的 shell_0 / 裸串） ---------------------------
out = norm([fc("shell_0", "shell_0")])[0]
check("shell_0 形状的 id 被改成 fc_", out["id"] == "fc_shell0", out.get("id"))
out = norm([fc("weird.id-99", "weird.id-99")])[0]
check("非字母数字被剥掉", out["id"] == "fc_weirdid99", out.get("id"))

# --- 4. 已经合规的不动 -----------------------------------------------------
out = norm([fc("fc_already", "call_already")])[0]
check("fc_ 开头的 id 原样不动", out["id"] == "fc_already", out.get("id"))
check("call_ 开头的 call_id 原样不动", out["call_id"] == "call_already", out.get("call_id"))

# --- 5. 非工具类 item 的 id 绝不能被改写 ------------------------------------
out = norm([
    {"type": "message", "role": "assistant", "id": "msg_keepme", "content": [{"type": "output_text", "text": "hi"}]},
    {"type": "reasoning", "id": "rs_keepme", "summary": [{"type": "summary_text", "text": "think"}]},
])
check("message 的 id 不动", out[0].get("id") == "msg_keepme", out[0].get("id"))
check("reasoning 的 id 不动", out[1].get("id") == "rs_keepme", out[1].get("id"))

# --- 6. 期望前缀按 item TYPE 决定,不是一个全局常量 --------------------------
# 2026-08-02 事故: 第一版对所有工具类一律强制 fc_,把合规的 custom_tool_call
# (上游要求 ctc_) 改坏,15 分钟 70 次 400 —— 比它修的 bug 更糟。
ctc = norm([{"type": "custom_tool_call", "name": "apply_patch",
             "id": "ctc_1a2b3c4d5e6f7a8b9c0d1e2f", "call_id": "call_x1"}])[0]
check("合规的 ctc_ id 绝不能被改成 fc_", ctc["id"] == "ctc_1a2b3c4d5e6f7a8b9c0d1e2f", ctc.get("id"))
ctc_dirty = norm([{"type": "custom_tool_call", "name": "apply_patch",
                   "id": POISON, "call_id": POISON}])[0]
check("custom_tool_call 的脏 id 补 ctc_ 而非 fc_",
      ctc_dirty["id"] == "ctc_70eac2a105794171b50ace1f", ctc_dirty.get("id"))
fcall = norm([fc("ctc_shouldbefc", "call_x2")])[0]
check("function_call 拿到 ctc_ 形状时改成 fc_", fcall["id"] == "fc_shouldbefc", fcall.get("id"))
for t in ("tool_call", "local_shell_call"):
    got = norm([{"type": t, "name": "shell", "id": POISON, "call_id": POISON}])[0]
    check("未确认期望前缀的 %s 不瞎猜(保持原值)" % t, got["id"] == POISON, got.get("id"))
    got2 = norm([{"type": t, "name": "shell", "id": "toolu_zz1", "call_id": "toolu_zz1"}])[0]
    check("%s 仍走旧 toolu_ 规则" % t, got2["id"] == "fc_zz1", got2.get("id"))

# --- 7. 配对不能断：function_call 与 function_call_output 的 call_id 要一致 --
pair = norm([
    fc("toolu_pair1", "toolu_pair1"),
    {"type": "function_call_output", "call_id": "toolu_pair1", "output": "ok"},
])
check("配对的 call_id 改写后仍相等", pair[0]["call_id"] == pair[1]["call_id"],
      "%s vs %s" % (pair[0].get("call_id"), pair[1].get("call_id")))
check("配对项 id 与 call_id 前缀正确",
      pair[0]["id"].startswith("fc_") and pair[0]["call_id"].startswith("call_"))

pair2 = norm([
    fc(POISON, POISON),
    {"type": "function_call_output", "call_id": POISON, "output": "ok"},
])
check("call_ 形状配对也不断", pair2[0]["call_id"] == pair2[1]["call_id"],
      "%s vs %s" % (pair2[0].get("call_id"), pair2[1].get("call_id")))

# --- 8. 确定性：多轮回放同一个脏值必须得到同一个干净值 -----------------------
a = norm([fc(POISON, POISON)])[0]
b = norm([fc(POISON, POISON)])[0]
check("同输入同输出（确定性）", a == b)
c = norm([fc(a["id"], a["call_id"])])[0]
check("二次归一化是幂等的", c["id"] == a["id"] and c["call_id"] == a["call_id"],
      "%s / %s" % (c.get("id"), c.get("call_id")))

# --- 9. 非目标模型：只强制 id，其余一概不碰 --------------------------------
# gpt-5.4 / gpt-5.2 / gpt-5.3-codex 不在 _TARGET_MODEL_PARTS 里，2026-08-02 修完
# id 判据后它们仍在漏 fc_ 400，因为整个清洗对这些组根本不跑。补一个与模型无关的
# 最小 pass：只改 id，不引入重型清洗（避免给从未经历过的流量换行为）。
untouched = MOD._normalize_data({
    "model": "chatgpt-gpt-5.4",
    "input": [
        fc(POISON, POISON),
        {"type": "reasoning", "id": "encitem_keep", "encrypted_content": "litellm_enc:zzz",
         "summary": [{"type": "summary_text", "text": "t"}]},
        {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "sys"}]},
    ],
    "previous_response_id": "resp_keepme",
}, "test")
check("非目标模型的工具 id 也被强制 fc_", untouched["input"][0]["id"] == "fc_70eac2a105794171b50ace1f",
      untouched["input"][0].get("id"))
check("非目标模型不做 encrypted_content 剥离",
      untouched["input"][1].get("encrypted_content") == "litellm_enc:zzz", untouched["input"][1])
check("非目标模型不剥 encitem_ 的 id", untouched["input"][1].get("id") == "encitem_keep", untouched["input"][1])
check("非目标模型不改写 system message",
      untouched["input"][2].get("type") == "message", untouched["input"][2])
check("非目标模型保留 previous_response_id",
      untouched.get("previous_response_id") == "resp_keepme", untouched.get("previous_response_id"))
check("非目标模型无脏 id 时不复制 input（零改动零副作用）",
      MOD._normalize_data({"model": "chatgpt-gpt-5.4", "input": [fc("fc_clean", "call_clean")]}, "t")["input"][0]["id"] == "fc_clean")
check("非目标模型下 ctc_ 同样不被改坏",
      MOD._normalize_data({"model": "chatgpt-gpt-5.4", "input": [
          {"type": "custom_tool_call", "id": "ctc_keepme", "call_id": "call_k"}]}, "t")["input"][0]["id"] == "ctc_keepme")
check("非目标模型 + 无 input 字段不报错",
      MOD._normalize_data({"model": "chatgpt-gpt-5.4"}, "t") == {"model": "chatgpt-gpt-5.4"})

# --- 10. 既有能力不能被我改坏 ----------------------------------------------
enc = norm([{"type": "reasoning", "id": "encitem_x1", "encrypted_content": "litellm_enc:zzz",
            "summary": [{"type": "summary_text", "text": "t"}]}])[0]
check("encitem_ 的 id 仍被剥掉", "id" not in enc, enc)
check("encrypted_content 仍被剥掉", "encrypted_content" not in enc, enc)
named = norm([fc(POISON, POISON, name="查天气.v2")])[0]
check("函数名仍被 sanitize", MOD._NAME_RE.search(named["name"]) is None, named.get("name"))
check("sanitize 是确定性的", named["name"] == norm([fc(POISON, POISON, name="查天气.v2")])[0]["name"],
      named.get("name"))
check("sanitize 带 sha1 摘要后缀（避免不同脏名撞车）", len(named["name"].rsplit("_", 1)[-1]) == 8,
      named.get("name"))

# --- 11. 真实载荷形状：62 个 item，毒 id 在 index 61 ------------------------
big = []
for i in range(61):
    big.append({"type": "message", "role": "user", "id": "msg_%d" % i,
                "content": [{"type": "input_text", "text": "t%d" % i}]})
big.append(fc(POISON, POISON))
res = norm(big)
bad = [it.get("id") for it in res
       if isinstance(it, dict) and it.get("type") in MOD._TOOL_CALL_TYPES
       and isinstance(it.get("id"), str) and not it["id"].startswith("fc_")]
check("整条 history 里没有残留非 fc_ 的工具 id", not bad, bad)
check("非工具 item 数量与 id 未被波及",
      sum(1 for it in res if it.get("type") == "message") == 61)

# --- 12. 判据不能退回前缀白名单（护栏） ------------------------------------
check("_force_tool_id_prefix 对任意不符前缀的串都生效",
      MOD._force_tool_id_prefix("totally_new_shape_123", "fc_")[1] is True)
check("已符合期望前缀的串不动", MOD._force_tool_id_prefix("fc_x", "fc_")[1] is False)
check("ctc_ 目标下 fc_ 串会被改", MOD._force_tool_id_prefix("fc_x", "ctc_") == ("ctc_x", True))
check("期望前缀表覆盖 function_call/custom_tool_call",
      MOD._TOOL_ID_EXPECTED_PREFIX == {"function_call": "fc_", "custom_tool_call": "ctc_"},
      MOD._TOOL_ID_EXPECTED_PREFIX)


# --- 13. 字符串形式的 input 必须被归一成数组 -------------------------------
# Responses 规范允许 input 是 string 或 array，但 ChatGPT(codex) 上游只吃 array，
# 裸字符串会 400 {"detail":"Input must be a list"}（2026-08-02: 20 分钟 31 次，
# 全在 chatgpt-gpt-5.5）。客户端并没写错，所以我们归一而不是让它整单落兜底。
d = MOD._normalize_data({"model": TARGET, "input": "hello world"}, "test")
check("字符串 input 变成数组", isinstance(d["input"], list) and len(d["input"]) == 1, d.get("input"))
check("归一后是 user 角色的 input_text",
      d["input"][0]["role"] == "user"
      and d["input"][0]["content"][0]["type"] == "input_text"
      and d["input"][0]["content"][0]["text"] == "hello world", d["input"][0])
d2 = MOD._normalize_data({"model": TARGET, "input": ""}, "test")
check("空字符串不凭空造消息", d2["input"] == "", d2.get("input"))
d3 = MOD._normalize_data({"model": TARGET, "input": [fc(POISON, POISON)]}, "test")
check("数组 input 行为不变", d3["input"][0]["id"] == "fc_70eac2a105794171b50ace1f", d3["input"][0].get("id"))
d4 = MOD._normalize_data({"model": "chatgpt-gpt-5.4", "input": "hi"}, "test")
check("非目标模型不做 input 归一（不扩大爆炸半径）", d4["input"] == "hi", d4.get("input"))


# --- 汇总 -----------------------------------------------------------------
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, d) for n, ok, d in _results if not ok]
for name, ok, detail in _results:
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "   -> got: %r" % (detail,)))
print("\n%d/%d passed" % (passed, len(_results)))
if failed:
    sys.exit(1)
