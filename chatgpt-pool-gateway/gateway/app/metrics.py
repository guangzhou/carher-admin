"""Prometheus 指标。

承诺指标 (验收 SLO + 排障):
  gateway_requests_total{path, code}
  gateway_request_duration_seconds{path}
  gateway_picker_total{result, reason}   result=hit|miss
  gateway_affinity_total{result}         hit|miss
  gateway_acct_state{name, state}        gauge 1
  gateway_acct_used_pct{name, window}    primary|secondary
  gateway_compaction_drops_total
  gateway_refresh_total{result}          ok|fail|rate_limited
  gateway_probe_total{result}            ok|fail
  gateway_first_byte_5xx_total           fail-fast 触发计数
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "gateway_requests_total", "HTTP requests", ["path", "code"]
)
DURATION = Histogram(
    "gateway_request_duration_seconds", "Request latency", ["path"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
PICKER = Counter(
    "gateway_picker_total", "Picker decisions", ["result", "reason"]
)
AFFINITY = Counter(
    "gateway_affinity_total", "Affinity lookup", ["result"]
)
ACCT_STATE = Gauge(
    "gateway_acct_state", "Account state (1=current)", ["name", "state"]
)
ACCT_USED_PCT = Gauge(
    "gateway_acct_used_pct", "Window used percent", ["name", "window"]
)
COMPACTION_DROPS = Counter(
    "gateway_compaction_drops_total", "Compaction items dropped"
)
REFRESH = Counter(
    "gateway_refresh_total", "Token refresh outcome", ["result"]
)
PROBE = Counter(
    "gateway_probe_total", "Wham probe outcome", ["result"]
)
FIRST_BYTE_5XX = Counter(
    "gateway_first_byte_5xx_total", "5xx before first byte (fail-fast trigger)", []
)


def record_acct_gauge(name: str, state: str, primary_pct: float, secondary_pct: float) -> None:
    """guage 复位: 同一 acct 只让当前 state 标 1, 其余清 0。"""
    for s in ("healthy", "cooling", "offline", "token_invalidated", "disabled"):
        ACCT_STATE.labels(name=name, state=s).set(1 if s == state else 0)
    ACCT_USED_PCT.labels(name=name, window="primary").set(primary_pct)
    ACCT_USED_PCT.labels(name=name, window="secondary").set(secondary_pct)
