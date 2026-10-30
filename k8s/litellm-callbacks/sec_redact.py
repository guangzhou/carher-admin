"""sec_redact.py — 网信《车联天下 AI 出口敏感信息脱敏规则》第一版落地（198 母 router）。

规范：飞书 WHYUdeTnCo0TZQxWCqHcFzOTnBe（rev 9）。方案：飞书 SXegdiYvXoEdXnxClUWcpQ02nfi。

本版做什么（方案文档 P0 + P1 的规则面，按两条已定口径）
--------------------------------------------------------
口径 1：「出口」两个方向都管。
  * 请求方向（发给外部厂商之前）：敏感串就地替换后再发。**密钥也是替换，不是拦截** ——
    10-20 抽样里命中密钥的请求 100% 是历史消息里的（agent 每轮重发整段历史），
    请求方向整条拦截 = 会话从此每轮都被拦 = 会话作废。
  * 响应方向（回给员工之前）：同样就地替换（密钥 ⇒ ``[密钥凭证脱敏]``，其余按规则掩码），**不阻断**。
    10-29 用户定：影子期真实命中全是 agent 历史里重发的凭证，整条阻断会让会话作废；替换掉值本身就够了。
口径 2：编程 agent 只做密钥。
  * key 别名前缀命中 ``SEC_REDACT_AGENT_PREFIXES``（默认 cursor-/claude-code-/codex-）⇒ 只跑「3-」凭证规则。
    IP/MAC/手机号不替换：模型看到 ``[内网IP脱敏]`` 会原样写进代码和命令。
  * 其余 key ⇒ 全量规则（手机/身份证/银行卡/证件/内网IP/MAC + 凭证）。

挂在哪
------
* ``async_pre_call_hook``：请求方向。入口跑一次；兜底换腿时发出去的是同一个已替换的 data。
  替换后调用 ``refresh_proxy_server_request_body`` 刷 SpendLogs 落库快照（上游 docstring 明说：
  就地改 data 的 guardrail 不刷它，落库的就是没脱敏的原文）。
* ``async_post_call_success_hook``：非流式响应（chat ModelResponse / Responses 对象 / anthropic dict）。
* ``async_post_call_streaming_iterator_hook``：流式响应，三种形态分开处理：
  - chat ``ModelResponseStream`` 对象：按 choice 缓冲，留 ``_HOLDBACK`` 字符尾巴防跨片段漏检；
  - Responses 事件对象（``response.output_text.delta``）：同一套缓冲；
  - anthropic 原始 SSE 字节：按 ``\\n\\n`` 切事件，只改 ``text_delta`` 的 ``text``。
  命中一律就地替换后放出，不阻断、不补文案。私钥正文远超尾巴长度：未闭合的 ``BEGIN … PRIVATE KEY``
  规则本身会连着已到的 base64 行一起命中 ⇒ 切点退到 BEGIN 前，一直扣到 END 再整段替换。

**本版不做**（方案文档已列为已知限制）：流式 tool_calls 参数、图片/文件内容、base64 内容、
词库类（2.1/5.2/7.2，等网信给词库）、组合类（1.5/2.2/6.1）、语义类（4.1/5.4/7.1）。

日志（约束项「日志记录规则」，按方案第 4 件口径）：命中类型、方向、位置、片段 sha256 前 12 位、
掩码后片段、key 别名、user_id、时间。**不记敏感原文。** 前缀 ``sec_redact: hit``，WARNING 级（198 只出 WARNING 以上）。

开关（env）
-----------
``SEC_REDACT_DISABLED=1``     整个插件透传（kill switch，改 env 走滚动更新）
``SEC_REDACT_SHADOW=1``       只记日志不改写（影子模式）
``SEC_REDACT_AGENT_PREFIXES`` 逗号分隔的 key 别名前缀，命中只做密钥
``SEC_REDACT_MAX_SCAN_CHARS`` 单段文本超过即只扫尾部这么多字符（默认 400000）

任何内部异常一律放行原内容并打 ``sec_redact: ERROR``，绝不让检测本身拖垮请求。
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
from typing import Any, Callable, Iterable, Optional

from litellm.integrations.custom_logger import CustomLogger

_log = logging.getLogger("sec_redact")

CRED_LABEL = "[密钥凭证脱敏]"
_HOLDBACK = 160  # 流式尾巴：最长规则（JWT/赋值型密钥）一般在这个长度内闭合

# ---------------------------------------------------------------- 开关


def _disabled() -> bool:
    return os.environ.get("SEC_REDACT_DISABLED") == "1"


def _shadow() -> bool:
    return os.environ.get("SEC_REDACT_SHADOW") == "1"


def _agent_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("SEC_REDACT_AGENT_PREFIXES", "cursor-,claude-code-,codex-")
    return tuple(p.strip() for p in raw.split(",") if p.strip())


def _max_scan() -> int:
    try:
        return int(os.environ.get("SEC_REDACT_MAX_SCAN_CHARS", "400000"))
    except ValueError:
        return 400000


# ---------------------------------------------------------------- 校验函数

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_EXAMPLE_MARK = re.compile(r"(示例|样例|测试|演示|example|sample|dummy|fake)", re.I)


def _id18_ok(s: str) -> bool:
    total = sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == s[17].upper()


def _luhn_ok(s: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(s)):
        d = int(c)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def _private_ip(s: str) -> bool:
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETS)


def _looks_random(s: str) -> bool:
    """赋值型密钥的值：≥16 位、不是纯字母/纯数字、不是占位符、字符种类够多。"""
    if len(s) < 16 or s.isdigit() or s.isalpha():
        return False
    low = s.lower()
    if any(p in low for p in ("xxxx", "your", "example", "placeholder", "****", "<", "${", "env", "redact", "脱敏")):
        return False
    return len(set(s)) >= 10


# ---------------------------------------------------------------- 规则表


# 不用 @dataclass：litellm 用 spec_from_file_location 加载 callback 且不登记 sys.modules，
# dataclass 定义时按 __module__ 回查模块会拿到 None ⇒ import 期 AttributeError ⇒ proxy 起不来
# （10-20 全链 T0 实测；单测里手动登记 sys.modules 会把这个坑盖住，见 test 的 _load）。
class Rule:
    __slots__ = ("name", "pattern", "replace", "validate", "credential")

    def __init__(self, name: str, pattern, replace: Callable, validate: Optional[Callable] = None):
        self.name = name  # 对齐规范编号
        self.pattern = pattern
        self.replace = replace
        self.validate = validate
        self.credential = name.startswith("3-")


class Hit:
    __slots__ = ("rule", "start", "end", "replacement")

    def __init__(self, rule: str, start: int, end: int, replacement: str):
        self.rule, self.start, self.end, self.replacement = rule, start, end, replacement

    def __repr__(self):
        return f"Hit({self.rule!r}, {self.start}, {self.end})"


def _mask_phone(m):
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def _mask_id(m):
    s = m.group(0)
    return s[:6] + "*" * (len(s) - 6)


def _mask_card(m):
    s = m.group("num")
    return m.group(0).replace(s, "*" * (len(s) - 4) + s[-4:])


def _cred(_m):
    return CRED_LABEL


def _cred_assign(m):
    # 只换值，保留 `password=` 这类键名，模型还能看懂上下文
    return m.group(0)[: m.start("val") - m.start(0)] + CRED_LABEL


# 顺序 = 优先级（约束项「白名单 > 高风险拦截 > 普通脱敏」：凭证排最前，同一位置先到先得）
RULES: tuple = (
    Rule("3-私钥", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        r"(?:[\s\S]{0,8000}?-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        # 没有 END（截断输出 / 流式还没到）⇒ 连同已到的 base64 正文行一起换掉，不能只换表头
        r"|(?:\r?\n[A-Za-z0-9+/=]{1,100})*)"), _cred),
    Rule("3-云AccessKey", re.compile(
        r"(?<![A-Za-z0-9])(?:(?:AKIA|ASIA)[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,24})(?![A-Za-z0-9])"), _cred),
    Rule("3-平台Token", re.compile(
        r"(?<![A-Za-z0-9])(?:sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{24,}|gh[pousr]_[A-Za-z0-9]{36}"
        r"|xox[abpr]-[A-Za-z0-9\-]{20,}|glpat-[A-Za-z0-9_\-]{20})"), _cred),
    Rule("3-JWT", re.compile(
        r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), _cred),
    Rule("3-赋值型密钥", re.compile(
        r"(?i)(?:api[_\-]?key|secret[_\-]?key|access[_\-]?key(?:[_\-]?secret)?|client[_\-]?secret"
        r"|password|passwd|密码|口令|私钥)"
        r"[\"']?\s*[:=：]\s*[\"']?(?P<val>[A-Za-z0-9_\-+/=.@#$%!]{16,})"),
        _cred_assign, lambda m: _looks_random(m.group("val"))),
    Rule("1.2-身份证", re.compile(
        r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"),
        _mask_id, lambda m: _id18_ok(m.group(0))),
    Rule("1.1-手机号", re.compile(r"(?<![0-9A-Za-z])1[3-9]\d{9}(?![0-9A-Za-z])"), _mask_phone),
    Rule("1.3-银行卡", re.compile(
        r"(?i)(?:卡号|银行卡|储蓄卡|信用卡|card\s*(?:no|number))[^\d\n]{0,12}(?P<num>\d{13,19})(?!\d)"),
        _mask_card, lambda m: _luhn_ok(m.group("num"))),
    Rule("1.4-证件号", re.compile(
        r"(?i)(?:护照|港澳通行证|通行证|军官证)(?:号码|号|编号)?\s*[:：]?\s*(?P<num>[A-Za-z]{1,2}\d{7,9})(?![0-9A-Za-z])"),
        lambda m: m.group(0).replace(m.group("num"), "[证件编号脱敏]")),
    Rule("5.1-内网IP", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
         lambda m: "[内网IP脱敏]", lambda m: _private_ip(m.group(0))),
    # 10-20 抽样：裸 MAC 正则命中里只有 13% 带 mac/网卡 字样，其余是哈希碰巧同形 ⇒ 必须带上下文词
    Rule("5.3-MAC", re.compile(
        r"(?i)(?:mac|ether|hwaddr|网卡|物理地址)[^\n]{0,12}?"
        r"(?P<v>(?<![0-9A-Fa-f:\-])(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:\-]))"),
        lambda m: m.group(0).replace(m.group("v"), "[设备硬件信息脱敏]")),
)
_CRED_RULES = tuple(r for r in RULES if r.credential)


def _is_example(text: str, start: int) -> bool:
    return bool(_EXAMPLE_MARK.search(text[max(0, start - 30): start]))


def scan(text: str, rules: Iterable = RULES) -> list:
    """全部命中，不重叠；规则表顺序即优先级。凭证不吃「示例」白名单（规范：高风险优先）。"""
    if not text:
        return []
    hits, taken = [], []
    for rule in rules:
        for m in rule.pattern.finditer(text):
            if rule.validate and not rule.validate(m):
                continue
            if not rule.credential and _is_example(text, m.start()):
                continue
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            hits.append(Hit(rule.name, s, e, rule.replace(m)))
    hits.sort(key=lambda h: h.start)
    return hits


def apply_hits(text: str, hits: list) -> str:
    out, last = [], 0
    for h in hits:
        out.append(text[last:h.start])
        out.append(h.replacement)
        last = h.end
    out.append(text[last:])
    return "".join(out)


# ---------------------------------------------------------------- 审计


def _sha12(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _who(user_api_key_dict) -> tuple:
    return (getattr(user_api_key_dict, "key_alias", None) or "-",
            getattr(user_api_key_dict, "user_id", None) or "-")


_AUDIT_DETAIL_MAX = int(os.environ.get("SEC_REDACT_AUDIT_DETAIL_MAX", "20"))


def _audit(direction: str, text: str, hits: list, user_api_key_dict, call_id: str, action: str):
    """每条命中一行明细，但同一段文本最多记 _AUDIT_DETAIL_MAX 条，其余按规则合并成一行计数。

    10-28 实测：agent 每轮重发整段历史，一段 3 万字符的代码里内网 IP 能命中 461 次 ⇒ 一个请求打出
    4600 行 WARNING，T0 量到 300K 请求体的耗时几乎全花在写日志上（扫描本身有缓存，几乎不花时间）。
    审计口径不变：命中类型/sha/掩码后片段照记，只是重复的不逐条写。
    """
    alias, uid = _who(user_api_key_dict)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for h in hits[:_AUDIT_DETAIL_MAX]:
        _log.warning(
            "sec_redact: hit dir=%s action=%s rule=%s sha=%s masked=%r len=%d key=%s user=%s req=%s ts=%s",
            direction, action, h.rule, _sha12(text[h.start:h.end]), h.replacement[:40],
            h.end - h.start, alias, uid, call_id, ts,
        )
    if len(hits) > _AUDIT_DETAIL_MAX:
        rest: dict = {}
        for h in hits[_AUDIT_DETAIL_MAX:]:
            rest[h.rule] = rest.get(h.rule, 0) + 1
        _log.warning(
            "sec_redact: hit-summary dir=%s action=%s more=%d by_rule=%s key=%s user=%s req=%s ts=%s",
            direction, action, len(hits) - _AUDIT_DETAIL_MAX,
            ",".join(f"{k}:{v}" for k, v in sorted(rest.items())), alias, uid, call_id, ts,
        )


# ---------------------------------------------------------------- 策略


def _rules_for(user_api_key_dict) -> tuple:
    alias = getattr(user_api_key_dict, "key_alias", None) or ""
    if alias.startswith(_agent_prefixes()):
        return _CRED_RULES
    return RULES


def _scan_bounded_uncached(text: str, rules) -> list:
    """超长文本只扫尾部（agent 的新增内容在末尾）；偏移量换算回原文。"""
    limit = _max_scan()
    if len(text) <= limit:
        return scan(text, rules)
    off = len(text) - limit
    return [Hit(h.rule, h.start + off, h.end + off, h.replacement) for h in scan(text[off:], rules)]


# agent 每轮把整段历史重发一遍 ⇒ 同一段文本会被反复扫。按 (规则集, 文本摘要) 缓存命中结果，
# 历史段只扫一次。10-20 全链 T0：19 万字符请求 无缓存 +60ms ⇒ 方案验收线是 +30ms。
# 缓存的是 Hit（位置 + 掩码后串），不存原文；摘要用 blake2b 16 字节。进程内 LRU，上限按条数。
_SCAN_CACHE: "dict[tuple, list]" = {}
_SCAN_CACHE_MAX = int(os.environ.get("SEC_REDACT_CACHE_ENTRIES", "20000"))
_MIN_CACHE_LEN = 256  # 短串直接扫比算摘要还快


def _scan_bounded(text: str, rules) -> list:
    if len(text) < _MIN_CACHE_LEN:
        return _scan_bounded_uncached(text, rules)
    key = (id(rules), len(text), hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest())
    hit = _SCAN_CACHE.get(key)
    if hit is not None:
        _SCAN_CACHE[key] = _SCAN_CACHE.pop(key)  # LRU：移到末尾
        return hit
    res = _scan_bounded_uncached(text, rules)
    _SCAN_CACHE[key] = res
    if len(_SCAN_CACHE) > _SCAN_CACHE_MAX:
        for k in list(_SCAN_CACHE)[: _SCAN_CACHE_MAX // 10]:
            _SCAN_CACHE.pop(k, None)
    return res


# ---------------------------------------------------------------- 请求方向：遍历 data 里的用户内容

# 这些键是网关/客户端元数据或工具 schema，不是要发给厂商的「内容」
_SKIP_KEYS = frozenset({
    "metadata", "litellm_metadata", "proxy_server_request", "litellm_logging_obj", "secret_fields",
    "tools", "tool_choice", "model", "litellm_call_id", "litellm_trace_id", "litellm_session_id",
    "id", "call_id", "tool_call_id", "tool_use_id", "type", "role", "name", "signature",
    "encrypted_content", "image_url", "url", "file_id", "file_data", "data", "source",
    "stream_options", "reasoning", "thinking", "text_format", "response_format", "user",
})
_CONTENT_ROOTS = ("messages", "input", "system", "instructions", "prompt")


def _walk_replace(obj, rules, sink: list, depth: int = 0):
    """返回替换后的对象（就地改 dict/list）。sink 收集 (原文, hits)。"""
    if depth > 40:
        return obj
    if isinstance(obj, str):
        hits = _scan_bounded(obj, rules)
        if not hits:
            return obj
        sink.append((obj, hits))
        return apply_hits(obj, hits)
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            obj[i] = _walk_replace(v, rules, sink, depth + 1)
        return obj
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k in _SKIP_KEYS and k not in ("arguments", "input"):
                continue
            obj[k] = _walk_replace(obj[k], rules, sink, depth + 1)
        return obj
    return obj


def _call_id(data) -> str:
    try:
        return str((data or {}).get("litellm_call_id") or "-")
    except Exception:
        return "-"


def _refresh_spendlog_body(data):
    """让 SpendLogs 落库的 proxy_server_request.body 也是脱敏后的版本。

    上游真名是 ``refresh_proxy_server_request_body_snapshot``（1.105.1 / 1.106.4 都是；第一版写成了
    不带 _snapshot 的名字 ⇒ import 静默失败，10-28 读码发现）。另外 ``common_request_processing``
    在 pre_call_hook **之后**自己也会调一次（guardrails_applied=True），所以这里只是兜底：
    找不到函数时打一行 WARNING（不再静默），落库仍由上游那次刷新保证。
    """
    try:
        from litellm.proxy.litellm_pre_call_utils import refresh_proxy_server_request_body_snapshot as _refresh
    except Exception as exc:
        _log.warning("sec_redact: refresh_proxy_server_request_body_snapshot not importable %r "
                     "(SpendLogs body relies on the proxy's own post-hook refresh)", exc)
        return
    try:
        _refresh(data, guardrails_applied=True)
    except Exception as exc:
        _log.warning("sec_redact: refresh body failed %r", exc)


# ---------------------------------------------------------------- 响应方向：非流式


def _resp_text_slots(resp) -> list:
    """非流式响应里所有正文槽位：[(getter, setter)]。覆盖 chat / Responses / anthropic dict。"""
    slots = []

    def add(container, key):
        if isinstance(container, dict):
            if isinstance(container.get(key), str):
                slots.append((lambda c=container, k=key: c[k], lambda v, c=container, k=key: c.__setitem__(k, v)))
        elif isinstance(getattr(container, key, None), str):
            slots.append((lambda c=container, k=key: getattr(c, k), lambda v, c=container, k=key: setattr(c, k, v)))

    def field(o, k):
        return o.get(k) if isinstance(o, dict) else getattr(o, k, None)

    for ch in field(resp, "choices") or []:
        msg = field(ch, "message")
        if msg is not None:
            add(msg, "content")
    for item in field(resp, "output") or []:
        for part in field(item, "content") or []:
            add(part, "text")
    if isinstance(resp, dict) and resp.get("type") == "message":
        for block in resp.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                add(block, "text")
    return slots


# ---------------------------------------------------------------- 响应方向：流式缓冲


class _Buf:
    """一路正文的缓冲区：攒够尾巴再放，放出去的部分已替换。"""

    def __init__(self, rules, who, call_id, direction="response"):
        self.rules, self.who, self.call_id, self.dir = rules, who, call_id, direction
        self.pending = ""

    def feed(self, s: str, final: bool = False) -> str:
        """喂入新文本，返回现在可以安全放出去的文本（已替换）。"""
        self.pending += s or ""
        if not self.pending:
            return ""
        hits = scan(self.pending, self.rules)
        if final:
            cut = len(self.pending)
        else:
            cut = max(0, len(self.pending) - _HOLDBACK)
            # 不能把一个命中切成两半：切点落在命中内部就退到命中起点
            for h in hits:
                if h.start < cut < h.end:
                    cut = h.start
        if cut == 0:
            return ""
        head_hits = [h for h in hits if h.end <= cut]
        head = self.pending[:cut]
        self.pending = self.pending[cut:]
        if head_hits:
            _audit(self.dir, head, head_hits, self.who, self.call_id, "shadow" if _shadow() else "replace")
            if not _shadow():
                head = apply_hits(head, head_hits)
        return head


class _Bufs:
    """按 (形态, 位置) 分路的缓冲区集合。"""

    def __init__(self, rules, who, call_id):
        self.rules, self.who, self.call_id = rules, who, call_id
        self.by_key: dict = {}

    def __call__(self, key) -> _Buf:
        if key not in self.by_key:
            self.by_key[key] = _Buf(self.rules, self.who, self.call_id)
        return self.by_key[key]


# Responses 流里会被缓冲改写的文本增量事件，及其 done 事件（done 前要先把尾巴放出去）
_R_BUFFERED_DELTAS = frozenset({
    "response.output_text.delta", "response.reasoning_text.delta",
    "response.reasoning_summary_text.delta", "response.refusal.delta",
})
_R_DONE_TO_DELTA = {d[: -len(".delta")] + ".done": d for d in _R_BUFFERED_DELTAS}
# 不含用户可读文本、量大的增量事件：不做整树扫（function_call_arguments.delta 本版不扫，已知限制）
_R_PASSTHROUGH_DELTAS = frozenset({"response.function_call_arguments.delta", "response.audio.delta"})


def _r_index(item):
    v = _get(item, "content_index")
    return v if v is not None else _get(item, "summary_index")


def _ev_type(item) -> str:
    v = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
    v = getattr(v, "value", v)
    return str(v or "")


def _set(o, k, v):
    if isinstance(o, dict):
        o[k] = v
    else:
        setattr(o, k, v)


def _get(o, k):
    return o.get(k) if isinstance(o, dict) else getattr(o, k, None)


class _ShadowScanner:
    """影子模式的响应侧计数器：只读事件、按路累加文本，流结束时扫一次并记审计日志。

    任何异常都吞掉（只打一行 WARNING），绝不抛进用户的流。累加上限 _SHADOW_MAX_CHARS，超了只留尾部。
    """

    _SHADOW_MAX_CHARS = 400_000

    def __init__(self, rules, who, call_id):
        self.rules, self.who, self.call_id = rules, who, call_id
        self.texts: dict = {}
        self.broken = False

    def _add(self, key, s):
        if isinstance(s, str) and s:
            t = self.texts.get(key, "") + s
            self.texts[key] = t[-self._SHADOW_MAX_CHARS:]

    def observe(self, item):
        if self.broken:
            return
        try:
            if isinstance(item, (bytes, bytearray)):
                for line in bytes(item).split(b"\n"):
                    if line.startswith(b"data:") and b"text_delta" in line:
                        try:
                            o = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        self._add(("a", o.get("index")), (o.get("delta") or {}).get("text"))
                return
            et = _ev_type(item)
            if et in _R_BUFFERED_DELTAS:
                self._add((et, _get(item, "item_id"), _r_index(item)), _get(item, "delta"))
                return
            for ch in _get(item, "choices") or []:
                d = _get(ch, "delta")
                if d is not None:
                    self._add(("c", _get(ch, "index") or 0), _get(d, "content"))
        except Exception as exc:
            self.broken = True
            _log.warning("sec_redact: shadow observe failed %r (stream unaffected)", exc)

    def finish(self):
        try:
            for text in self.texts.values():
                hits = scan(text, self.rules)
                if hits:
                    _audit("response", text, hits, self.who, self.call_id, "shadow")
        except Exception as exc:
            _log.warning("sec_redact: shadow finish failed %r (stream unaffected)", exc)


# ---------------------------------------------------------------- 插件


class SecRedact(CustomLogger):
    # ---------------- 请求方向
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if _disabled() or not isinstance(data, dict):
                return data
            ct = str(getattr(call_type, "value", call_type))
            if ct.startswith(("aembedding", "embedding", "aimage", "image")):
                return data
            rules = _rules_for(user_api_key_dict)
            sink: list = []
            t0 = time.perf_counter()
            target = data if not _shadow() else copy.deepcopy({k: data.get(k) for k in _CONTENT_ROOTS})
            for k in _CONTENT_ROOTS:
                if k in target:
                    target[k] = _walk_replace(target[k], rules, sink)
            ms = (time.perf_counter() - t0) * 1000
            if sink:
                cid = _call_id(data)
                for text, hits in sink:
                    _audit("request", text, hits, user_api_key_dict, cid, "shadow" if _shadow() else "replace")
                _log.warning("sec_redact: request ct=%s segs=%d hits=%d scan_ms=%.1f shadow=%s key=%s",
                             ct, len(sink), sum(len(h) for _, h in sink), ms, _shadow(), _who(user_api_key_dict)[0])
                if not _shadow():
                    _refresh_spendlog_body(data)
            elif ms > 50:
                _log.warning("sec_redact: request ct=%s hits=0 scan_ms=%.1f key=%s", ct, ms, _who(user_api_key_dict)[0])
        except Exception as exc:
            _log.warning("sec_redact: ERROR pre_call %r (request passes through unchanged)", exc)
        return data

    # ---------------- 响应方向：非流式
    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            if _disabled() or response is None:
                return None
            rules = _rules_for(user_api_key_dict)
            cid = _call_id(data)
            slots = _resp_text_slots(response)
            for get, put in slots:
                text = get()
                hits = scan(text, rules)
                if not hits:
                    continue
                _audit("response", text, hits, user_api_key_dict, cid, "shadow" if _shadow() else "replace")
                if not _shadow():
                    put(apply_hits(text, hits))
        except Exception as exc:
            _log.warning("sec_redact: ERROR post_call %r (response passes through unchanged)", exc)
        return None

    # ---------------- 响应方向：流式
    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        if _disabled() or not hasattr(response, "__aiter__"):
            async for item in _as_aiter(response):
                yield item
            return
        if _shadow():
            # 影子模式：原样转发每个事件，另起一条只读的扫描（扫描本身出任何错都不影响这条流）。
            # 不复用下面的改写路径——那条路径为了跨片段替换要缓冲、改字段，出错会打断用户的流；
            # 影子期的目标只是数「会命中多少」，必须做到对用户零影响。
            scanner = _ShadowScanner(_rules_for(user_api_key_dict), user_api_key_dict, _call_id(request_data))
            async for item in response:
                scanner.observe(item)
                yield item
            scanner.finish()
            return
        rules = _rules_for(user_api_key_dict)
        buf = _Bufs(rules, user_api_key_dict, _call_id(request_data))
        last_delta: dict = {}
        sse = b""
        try:
            async for item in response:
                # -------- anthropic 原始 SSE 字节
                if isinstance(item, (bytes, bytearray)):
                    sse += bytes(item)
                    while b"\n\n" in sse:
                        ev, sse = sse.split(b"\n\n", 1)
                        for out in _rewrite_anthropic_event(ev + b"\n\n", buf):
                            yield out
                    continue

                et = _ev_type(item)
                # -------- Responses 事件对象
                if et.startswith("response."):
                    # 正文和推理文本都走缓冲（10-20 gray 真实 deepseek：密钥先出现在 reasoning 里）
                    if et in _R_BUFFERED_DELTAS:
                        rk = (et, _get(item, "item_id"), _r_index(item))
                        last_delta[rk] = item
                        _set(item, "delta", buf(rk).feed(_get(item, "delta") or ""))
                        # 10-20 全链 T0：最后一个 delta 事件上 litellm 会带一个顶层 `text`（到此为止的全文）
                        if isinstance(_get(item, "text"), str):
                            _set(item, "text", _redact_final(_get(item, "text"), rules))
                        yield item
                        continue
                    delta_et = _R_DONE_TO_DELTA.get(et)
                    if delta_et:
                        rk = (delta_et, _get(item, "item_id"), _r_index(item))
                        if rk in buf.by_key:
                            tail = buf(rk).feed("", final=True)
                            if tail:
                                # 尾巴用「最近一个真 delta 事件」做模板复制；不能复制 done 事件 ——
                                # 它带着整段原文的 `text`，pydantic 对象删不掉这个字段，
                                # 10-20 全链 T0 实测手机号/IP 就是从这份拷贝里原样出网的。
                                tmpl = last_delta.get(rk)
                                extra = copy.deepcopy(tmpl) if tmpl is not None else copy.deepcopy(item)
                                _set(extra, "type", delta_et)
                                _set(extra, "delta", tail)
                                if isinstance(extra, dict):
                                    extra.pop("text", None)
                                    extra.pop("logprobs", None)
                                elif isinstance(_get(extra, "text"), str):
                                    _set(extra, "text", _redact_final(_get(extra, "text"), rules))
                                yield extra
                    # 其余所有 response.* 事件（done/completed/item 等）整树扫：带整段正文副本的槽位太多
                    if et not in _R_PASSTHROUGH_DELTAS:
                        _patch_responses_done(item, rules)
                    yield item
                    continue

                # -------- chat ModelResponseStream 对象
                choices = _get(item, "choices")
                if choices:
                    for ch in choices:
                        delta = _get(ch, "delta")
                        if delta is None:
                            continue
                        content = _get(delta, "content")
                        final = bool(_get(ch, "finish_reason"))
                        if not isinstance(content, str) and not final:
                            continue
                        out = buf(("c", _get(ch, "index") or 0)).feed(content if isinstance(content, str) else "", final=final)
                        _set(delta, "content", out if (out or isinstance(content, str)) else content)
                    yield item
                    continue
                yield item
        except Exception as exc:
            _log.warning("sec_redact: ERROR stream %r", exc)
            raise
        if sse:
            yield sse


def _as_aiter(obj):
    async def gen():
        if hasattr(obj, "__aiter__"):
            async for x in obj:
                yield x
        else:
            yield obj
    return gen()


def _redact_final(text, rules):
    if not isinstance(text, str) or _shadow():
        return text
    hits = scan(text, rules)
    return apply_hits(text, hits) if hits else text


_TEXT_FIELDS = ("text", "content", "delta", "output_text", "refusal", "arguments", "summary")


def _scrub_all_text(obj, rules, depth=0):
    """事件对象里**所有**文本字段都过一遍（dict 和 pydantic 对象都走）。

    10-20 gray 真实上游实测：deepseek 走 Responses 时，密钥会从
    reasoning_text.done.text / content_part.done.part.text / output_item.done.item.content[0].text
    三处出网 —— 字段清单式的补丁总会漏下一个形态，所以 done 类事件改成整树扫。
    """
    if depth > 12 or obj is None:
        return obj
    if isinstance(obj, str):
        return _redact_final(obj, rules)
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            obj[i] = _scrub_all_text(v, rules, depth + 1)
        return obj
    if isinstance(obj, dict):
        for k in list(obj):
            if k in ("type", "id", "item_id", "call_id", "status", "role", "model", "object", "encrypted_content"):
                continue
            obj[k] = _scrub_all_text(obj[k], rules, depth + 1)
        return obj
    # pydantic / 普通对象：先按已知文本字段，再按容器字段递归
    for k in _TEXT_FIELDS + ("part", "item", "response", "output", "choices", "message"):
        try:
            v = getattr(obj, k, None)
        except Exception:
            continue
        if v is None:
            continue
        nv = _scrub_all_text(v, rules, depth + 1)
        if nv is not v or isinstance(v, str):
            try:
                setattr(obj, k, nv)
            except Exception:
                pass
    return obj


def _patch_responses_done(item, rules):
    """done/completed 事件里带着整段正文的副本，客户端可能读它 ⇒ 必须同口径改写。"""
    try:
        if _shadow():
            return
        _scrub_all_text(item, rules)
        # (容器, 字段名)：所有可能带整段正文副本的槽位。
        # 10-20 全链 T0 实测：litellm 给 response.completed 额外塞了顶层 `text` 和
        # `response.choices[0].message.content` 两份 chat 形态的副本 —— 只改 output[].content[].text
        # 时密钥从这两处原样出网。
        slots = [(item, "text")]
        part = _get(item, "part")
        if part is not None:
            slots.append((part, "text"))
        for holder in (_get(item, "item"), _get(item, "response")):
            if holder is None:
                continue
            slots.append((holder, "text"))
            for out in (_get(holder, "output") or [holder]):
                slots.extend((c, "text") for c in (_get(out, "content") or []))
            for ch in _get(holder, "choices") or []:
                msg = _get(ch, "message")
                if msg is not None:
                    slots.append((msg, "content"))
        for container, field in slots:
            t = _get(container, field)
            if isinstance(t, str) and t:
                _set(container, field, _redact_final(t, rules))
    except Exception as exc:
        _log.warning("sec_redact: patch done failed %r", exc)


def _anthropic_delta(idx, text: str) -> bytes:
    d = {"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta", "text": text}}
    return b"event: content_block_delta\ndata: " + json.dumps(d, ensure_ascii=False).encode() + b"\n\n"


def _rewrite_anthropic_event(ev: bytes, buf: "_Bufs"):
    """一个 anthropic SSE 事件 → 0~2 个事件。只碰 text_delta；收尾事件前把尾巴放出去。"""
    try:
        lines = ev.split(b"\n")
        di = next((i for i, l in enumerate(lines) if l.startswith(b"data:")), None)
        if di is None:
            yield ev
            return
        obj = json.loads(lines[di][5:].strip())
        t = obj.get("type")
        idx = obj.get("index")
        if t == "content_block_delta" and (obj.get("delta") or {}).get("type") == "text_delta":
            obj["delta"]["text"] = buf(("a", idx)).feed(obj["delta"].get("text") or "")
            lines[di] = b"data: " + json.dumps(obj, ensure_ascii=False).encode()
            yield b"\n".join(lines)
            return
        if t == "content_block_stop":
            tail = buf(("a", idx)).feed("", final=True) if ("a", idx) in buf.by_key else ""
            if tail:
                yield _anthropic_delta(idx, tail)
        elif t in ("message_delta", "message_stop"):
            # 桥接流可能不发 content_block_stop（budget_notice 10-20 gray 实测）⇒ 这里把所有尾巴放掉
            for key, b in list(buf.by_key.items()):
                if key[0] == "a":
                    tail = b.feed("", final=True)
                    if tail:
                        yield _anthropic_delta(key[1], tail)
        yield ev
    except Exception as exc:
        _log.warning("sec_redact: anthropic event passthrough %r", exc)
        yield ev


sec_redact = SecRedact()
_log.warning("sec_redact: installed rules=%d agent_prefixes=%s shadow=%s disabled=%s",
             len(RULES), ",".join(_agent_prefixes()), _shadow(), _disabled())
