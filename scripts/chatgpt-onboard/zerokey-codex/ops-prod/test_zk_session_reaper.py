"""zk-session-reaper api() 传输层重试的验证。

阳性对照：连接被拒 → 重试 API_RETRIES-1 次 → 抛 TransportDown（不是 URLError 裸穿）。
阴性对照 1：正常 200 → 一次成功，不重试，返回解析后的 dict。
阴性对照 2：/pro 返回 404 → 换空 prefix 重试（这条语义是原有的，不能被我改坏）。
阴性对照 3：500 → 立刻抛 HTTPError，**不重试**（状态码是业务信号）。
"""
import json
import os
import socket
import sys
import urllib.error
import urllib.request

os.environ["LITELLM_MK"] = "test-not-a-real-key"
os.environ["API_BACKOFF"] = "0"  # 测试里不真等
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zk_session_reaper as R  # noqa: E402


class FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run(name, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"FAIL {name}: {e}")
        return 1
    print(f"PASS {name}")
    return 0


def t_positive_transport_refused():
    calls = []

    def fake(req, *a, **k):
        calls.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    R.urllib.request.urlopen = fake
    try:
        R.api("GET", "/model/info")
    except R.TransportDown as e:
        assert "ConnectionRefused" in str(e) or "111" in str(e), f"message lost the cause: {e}"
    else:
        raise AssertionError("expected TransportDown, got no exception")
    # 4 次尝试 × 2 个 prefix 里的第一个就该抛出（抛出后不会走到第二个 prefix）
    assert len(calls) == R.API_RETRIES, f"expected {R.API_RETRIES} attempts, got {len(calls)}"


def t_positive_timeout():
    calls = []

    def fake(req, *a, **k):
        calls.append(1)
        raise socket.timeout("timed out")

    R.urllib.request.urlopen = fake
    try:
        R.api("GET", "/model/info")
    except R.TransportDown:
        pass
    else:
        raise AssertionError("timeout should also raise TransportDown")
    assert len(calls) == R.API_RETRIES, f"timeout not retried: {len(calls)}"


def t_negative_happy_path():
    calls = []

    def fake(req, *a, **k):
        calls.append(req.full_url)
        return FakeResp({"data": [{"model_info": {"id": "zk-81-x"}}]})

    R.urllib.request.urlopen = fake
    out = R.api("GET", "/model/info")
    assert out["data"][0]["model_info"]["id"] == "zk-81-x", out
    assert len(calls) == 1, f"happy path must not retry: {len(calls)}"
    assert calls[0].endswith("/pro/model/info"), calls[0]


def t_negative_404_falls_through_to_bare_prefix():
    calls = []

    def fake(req, *a, **k):
        calls.append(req.full_url)
        if "/pro/" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
        return FakeResp({"data": []})

    R.urllib.request.urlopen = fake
    out = R.api("GET", "/model/info")
    assert out == {"data": []}, out
    assert len(calls) == 2, f"404 must fall through exactly once: {calls}"


def t_negative_500_not_retried():
    calls = []

    def fake(req, *a, **k):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)

    R.urllib.request.urlopen = fake
    try:
        R.api("GET", "/model/info")
    except urllib.error.HTTPError as e:
        assert e.code == 500
    else:
        raise AssertionError("500 should propagate as HTTPError")
    assert len(calls) == 1, f"5xx must not be retried, got {len(calls)} attempts"


if __name__ == "__main__":
    bad = 0
    for n, f in [
        ("positive/transport-refused-raises-TransportDown", t_positive_transport_refused),
        ("positive/timeout-retried", t_positive_timeout),
        ("negative/happy-path-single-call", t_negative_happy_path),
        ("negative/404-falls-through", t_negative_404_falls_through_to_bare_prefix),
        ("negative/500-not-retried", t_negative_500_not_retried),
    ]:
        bad += run(n, f)
    print(f"{'OK' if not bad else 'FAILURES'}: {5 - bad}/5 passed")
    raise SystemExit(1 if bad else 0)
