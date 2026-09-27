"""门禁：`scripts/litellm-198-max-input-tokens-audit.sh` 不许退回到"把闸门关掉当修复建议"。

为什么要钉住它（2026-09-25 的坑）：
`model_info.max_input_tokens` 在 198 上是**真闸门**（`_pre_call_checks` 按它过滤 deployment），
`10000000` 不是能力声明，是把闸门写到够不着 = 关掉。用户面表现是模型目录里出现 `10M | 128k`。

这个问题"反复出现了多次"的机械原因就在这个脚本里，两个缺陷都实测复现过：
  ① 它只把**空值**算 missing ⇒ 在 15 行全是 `10000000` 的 pattern 上输出
     `missing=0 / AUDIT OK / EXIT=0`。现存唯一的门禁把错值判为正确。
  ② 它打印的"修复方式"里硬编 `{"max_input_tokens": 10000000}` ⇒ 照它办一次就关一次闸门。
  ③ 它只读 DB，而 DB 里 `<none>` 不等于运行时没值（zerokey-pool-gpt-5.6-* 在 DB 全 `<none>`，
     `/model/info` 读到 10,000,000）⇒ 只查 DB 看不见大多数病灶。

取值真源是 `scripts/litellm-198-model-ceilings.json`，不许再在脚本里硬编数字。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(HERE, "scripts", "litellm-198-max-input-tokens-audit.sh")
CEILINGS = os.path.join(HERE, "scripts", "litellm-198-model-ceilings.json")


def _src() -> str:
    with open(SCRIPT, encoding="utf-8") as f:
        return f.read()


def _spec() -> dict:
    with open(CEILINGS, encoding="utf-8") as f:
        return json.load(f)


def test_script_exists_and_is_executable():
    """先断言尺子本身读得到 —— 会数东西的测试必须先证明自己数得到。

    `#!` 没有 +x 位 = 文档/skill 里那条 `./scripts/...` 命令跑不了。
    """
    assert os.path.isfile(SCRIPT), SCRIPT
    src = _src()
    assert len(src) > 1000, "脚本被读空/截断，后面的断言全是假绿"
    assert src.startswith("#!"), "缺 shebang"
    assert os.access(SCRIPT, os.X_OK), "缺 +x：文档里的 ./scripts/... 跑不起来"


def test_never_prescribes_gate_off_value():
    """脚本里不许把 10000000 当成要写进去的值。

    出现在"这是错的"这类**注释**里是允许的（脚本头部就在讲这段历史），但不许出现在
    `SET model_info = ... 10000000` 这种赋值形状里 —— 那正是老版本教人关闸门的那一行。
    """
    code = "\n".join(
        line for line in _src().splitlines() if not line.lstrip().startswith("#")
    ).lower()
    for bad in ("max_input_tokens\": 10000000", "max_input_tokens': 10000000",
                "max_input_tokens: 10000000", "max_input_tokens=10000000"):
        assert bad not in code, f"又把关闸门的值写成赋值了：{bad}"


def test_absurd_value_is_a_failure_not_a_pass():
    """>= 1e7 必须是一个会判红的分支，而不是"已配置"。"""
    src = _src()
    assert "ABSURD" in src, "丢了荒谬值这一类"
    assert "_absurd_threshold" in src, "阈值必须从官方口径表读，不许在脚本里硬编"
    assert "AUDIT FAIL" in src and "sys.exit(1)" in src, "有问题行必须以退出码 1 结束"


def test_reads_the_official_ceilings_table():
    """取值真源必须是那张 JSON，且 JSON 里是官方数。"""
    src = _src()
    assert "litellm-198-model-ceilings.json" in src, "脚本没引用官方口径表"
    spec = _spec()
    assert spec["_absurd_threshold"] == 10000000
    ceil = spec["ceilings"]
    for name in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert ceil[name]["max_input_tokens"] == 872000, f"{name} 的官方天花板被改动了"
    assert ceil["gpt-5.5"]["max_input_tokens"] == 272000
    src_meta = spec["sources"]["codex_catalog"]
    assert "client_version=1.0.0" in src_meta["url"], "目录接口按 client_version 发货，低版本返空列表"
    assert src_meta["field"] == "max_context_window"


def test_retired_models_are_a_separate_class():
    """给已退役模型填上限是空操作 —— 必须单独分类，不能混进"缺配置"去补。"""
    src = _src()
    assert "RETIRED" in src
    spec = _spec()
    assert "gpt-5.4" in spec["retired"], "gpt-5.4 已退役这件事得记在表里"


def test_runtime_model_info_is_the_judge():
    """DB 里 `<none>` 不是判据，只有逐副本 /model/info 是。"""
    src = _src()
    assert "/v1/model/info" in src, "丢了运行时判据"
    assert "carher.net/litellm-production-route=enabled" in src, "生产车道标签（在 Pod 上，不在 Deployment 上）"
    # 空输出不是阴性结论
    assert "空输出不是阴性结论" in src


def test_no_stale_rollout_restart_advice():
    """`model_info` 改完靠 DB 轮询 90~100s 自行收敛，两次实测过；
    "必须 rollout restart" 是陈旧信息，而且会引到"重启在服务的 pod"这条红线上去。"""
    src = _src()
    assert "必须 rollout restart" not in src
    assert "不需要 rollout" in src


def test_points_at_the_surgical_writer():
    """修复建议必须指向带五道门的写库脚本，而不是给一条裸 UPDATE 让人手敲。"""
    src = _src()
    assert "db_patch_window_generic.sh" in src
    assert "OLD=" in src, "改错值只能走替换旧值模式（is null 改不动已有值）"


REGRESS = os.path.join(HERE, ".claude", "skills", "litellm-context-window-govern",
                       "scripts", "regress_window.py")
REPLICA = os.path.join(HERE, ".claude", "skills", "litellm-context-window-govern",
                       "scripts", "modelinfo_per_replica.sh")


def _regress() -> str:
    with open(REGRESS, encoding="utf-8") as f:
        return f.read()


def _regress_code() -> str:
    """只看非注释行。脚本头部会**引用**这些错值来讲它们为什么错，那是允许的；
    不许的是它们出现在真正会被执行的那一行上。"""
    return "\n".join(l for l in _regress().splitlines() if not l.lstrip().startswith("#"))


def test_gate_probe_sizes_are_derived_not_hardcoded():
    """探针档位必须从闸门实际值算出来。

    2026-09-25 的坑：这里焊着 `PASS_TOK, GATE_TOK = 919000, 923000`（922,000 时代的两侧）。
    闸门改到 872,000 之后这两发全在阈值上方 ⇒ 两条腿都 400，"放行腿"永远亮不了 ——
    尺子横跨了一个已经不存在的阈值，而且**它不会报错**，看着就像闸门设低了。
    """
    src = _regress_code()
    for stale in ("919000", "923000", "PASS_TOK, GATE_TOK ="):
        assert stale not in src, f"探针档位又被写死了：{stale}"
    assert "GATE_VAL" in src, "必须先从 /model/info 读出闸门实际值"
    assert "GATE_VAL + 2000" in src and "GATE_VAL - 3000" in src, "两条腿都要由闸门值推出来"


def test_block_leg_runs_before_the_expensive_pass_leg():
    """放行腿要烧 ~$12，预算被它吃掉后拦截腿会返 429 —— 那是判据被顶掉，不是闸门拦住。"""
    src = _regress()
    gate_at = src.index('gate_probe(GATE_VAL + 2000, "GATE")')
    pass_at = src.index('gate_probe(GATE_VAL - 3000, "PASS")')
    assert gate_at < pass_at, "拦截腿必须排在放行腿前面"
    assert 'BUDGET = float(os.environ.get("BUDGET", "200"))' in src, "默认预算要够放行腿烧"


def test_pass_leg_does_not_trust_a_fixed_token_ratio():
    """闸门数出来的 token 比上游真实分词高约 0.8%（869,000 条填充物被数成 875,801）。
    所以放行腿被拦了必须按它自己报的数回算重打，不许调一个固定比例。"""
    src = _regress()
    assert "re.search" in src and "counted" in src, "缺少按闸门自报数回算的重试"
    assert "import json, os, re" in src, "用了 re 就得导入它，否则这条分支一跑就 NameError"


def test_neighbour_smoke_names_exist_on_198():
    """邻居名必须是 198 上真实存在的组。`claude-opus-4-7` 在 198 上不存在，
    它稳定返 400 ProxyModelNotFoundError ⇒ 每轮回归挂一条长期红腿，
    把"我改坏了"和"这个名字是编的"混成同一个形状。"""
    assert "claude-opus-4-7" not in _regress_code(), "又把不存在的模型名写进邻居冒烟了"


def test_per_replica_reader_is_parameterized_and_loud_on_empty():
    """名字不能焊死（焊死就会在读别的行时仍然读 astra = 假绿）；
    读不到要显式喊出来（空输出不是阴性结论）。"""
    with open(REPLICA, encoding="utf-8") as f:
        src = f.read()
    assert 'NAMES="${NAMES:?' in src, "目标名必须传入，不许焊死"
    assert "空输出不是阴性结论" in src
    assert "NO-SUCH-ROW" in src, "组不存在要和读取失败区分开"
