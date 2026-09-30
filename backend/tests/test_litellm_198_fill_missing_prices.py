import importlib.util
import json
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "litellm-198-fill-missing-prices.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("fill_prices", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CM_FIXTURE = """model_list:
- litellm_params:
    api_base: https://x/cheliantianxia1
    input_cost_per_token: 1.4e-06
    model: custom_openai/glm-5.1
    output_cost_per_token: 4.4e-06
  model_info:
    id: wangsu/glm-5.1
    input_cost_per_token: 1.4e-06
    output_cost_per_token: 4.4e-06
  model_name: wangsu-glm-5.1
- litellm_params:
    model: anthropic/glm-4.7
    output_cost_per_token: 2.2e-06
  model_info:
    id: zai-max/glm-4.7
    output_cost_per_token: 2.2e-06
  model_name: zai-max-glm-4.7
- litellm_params:
    model: custom_openai/glm-5.1
    output_cost_per_token: 4.4e-06
  model_info:
    id: wangsu-cheliantianxia1/glm-5.1
    output_cost_per_token: 4.4e-06
  model_name: wangsu-cheliantianxia1-glm-5.1
- litellm_params:
    model: openai/untouched
    output_cost_per_token: 1.0e-06
  model_info:
    id: other/untouched
  model_name: untouched
"""


def test_prices_are_official_list_not_premultiplied(mod):
    # 阳性对照：×1.3 由 overlay 乘，这里必须是原价
    assert mod.DB_PRICES["claude-cursor/opus-5"]["input_cost_per_token"] == pytest.approx(5e-6)
    assert mod.DB_PRICES["claude-cursor/opus-5"]["output_cost_per_token"] == pytest.approx(25e-6)
    assert mod.DB_PRICES["cursor-ultra/claude-sonnet-4-6"]["cache_read_input_token_cost"] == pytest.approx(0.3e-6)
    assert mod.CM_PRICES["zai-max/glm-4.7"]["cache_read_input_token_cost"] == pytest.approx(0.11e-6)


def test_cache_only_rows_do_not_touch_input_output(mod):
    for mid in ("claude-cursor/composer-2.5", "cursor/composer-2.5", "pplx-sonar-prod-20260923"):
        assert set(mod.DB_PRICES[mid]) == {"cache_read_input_token_cost"}


def _snapshot(mod):
    # 形状照 09-29 生产备份：composer 带 request_timeout，别的行带密文 model/api_base
    rows = {mid: {"model": "enc:" + mid, "api_base": "enc:b", "input_cost_per_token": 0.0,
                  "output_cost_per_token": 0.0} for mid in mod.DB_PRICES}
    for mid in ("claude-cursor/composer-2.5", "cursor/composer-2.5"):
        rows[mid]["request_timeout"] = 300
    return rows


def _as_psql(rows):
    return "\n".join(f"{k}|{json.dumps(v)}" for k, v in rows.items()) + "\n"


def test_fix_sql_keeps_every_non_price_field_and_round_trips(mod):
    before = _snapshot(mod)
    sql = mod.fix_sql(mod.load_rows(_as_psql(before)))
    after = {}
    for line in sql.splitlines():
        m = re.match(r"update .* set litellm_params='(.*)'::jsonb, .* where model_id='(.*)';$", line)
        if m:
            after[m.group(2).replace("''", "'")] = json.loads(m.group(1).replace("''", "'"))
    assert set(after) == set(mod.DB_PRICES)
    assert after["cursor/composer-2.5"]["request_timeout"] == 300
    assert mod.diff_rows(before, after) == []
    # 回滚 SQL 写回的就是原行
    restored = [json.loads(m.replace("''", "'")) for m in
                re.findall(r"litellm_params='(.*?)'::jsonb", mod.fix_sql(before, restore=True))]
    assert restored == [before[mid] for mid in mod.DB_PRICES]


def test_diff_rows_catches_the_model_update_side_effects(mod):
    """阳性对照：复现 /model/update 那次的两种越界改动，守卫必须报。"""
    before = _snapshot(mod)
    after = {k: {**v, **mod.DB_PRICES[k]} for k, v in before.items()}
    assert mod.diff_rows(before, after) == []
    del after["cursor/composer-2.5"]["request_timeout"]
    after["claude-cursor/opus-5"]["allow_client_keepalive_override"] = False
    bad = mod.diff_rows(before, after)
    assert any("cursor/composer-2.5.request_timeout" in b for b in bad)
    assert any("allow_client_keepalive_override" in b for b in bad)


def test_fix_sql_refuses_incomplete_snapshot(mod):
    rows = _snapshot(mod)
    rows.pop("pplx-sonar-prod-20260923")
    with pytest.raises(SystemExit):
        mod.fix_sql(rows)


def test_row_status_reads_billing_block_only(mod):
    want = {"cache_read_input_token_cost": 2e-7}
    only_info = {"litellm_params": {}, "model_info": dict(want)}
    assert mod.row_status(only_info, want) == ["cache_read_input_token_cost"]
    good = {"litellm_params": dict(want)}
    assert mod.row_status(good, want) == []
    zero = {"litellm_params": {"cache_read_input_token_cost": 0}}
    assert mod.row_status(zero, want) == ["cache_read_input_token_cost"]


def test_patch_cm_adds_exactly_two_lines_per_target(mod):
    out, touched = mod.patch_cm_text(CM_FIXTURE)
    assert sorted(touched) == sorted(mod.CM_PRICES)
    assert out.count("\n") - CM_FIXTURE.count("\n") == 2 * len(mod.CM_PRICES)
    # 未命中的块逐字节不变
    tail = "- litellm_params:\n    model: openai/untouched"
    assert out[out.index(tail):] == CM_FIXTURE[CM_FIXTURE.index(tail):]
    yaml = pytest.importorskip("yaml")
    doc = yaml.safe_load(out)
    by_id = {d["model_info"]["id"]: d for d in doc["model_list"]}
    for mid, want in mod.CM_PRICES.items():
        for blk in ("litellm_params", "model_info"):
            assert by_id[mid][blk]["cache_read_input_token_cost"] == pytest.approx(
                want["cache_read_input_token_cost"])
        # input/output 原样
        assert by_id[mid]["litellm_params"]["output_cost_per_token"] in (4.4e-06, 2.2e-06)
    assert "cache_read_input_token_cost" not in by_id["other/untouched"]["litellm_params"]


def test_patch_cm_refuses_double_insert_and_missing_ids(mod):
    once, _ = mod.patch_cm_text(CM_FIXTURE)
    with pytest.raises(SystemExit):
        mod.patch_cm_text(once)
    with pytest.raises(SystemExit):
        mod.patch_cm_text(CM_FIXTURE.replace("id: zai-max/glm-4.7", "id: zai-max/gone"))


def test_backup_sql_quotes_ids(mod):
    sql = mod.backup_sql(["a/b", "it's"])
    assert "'a/b'" in sql and "'it''s'" in sql
