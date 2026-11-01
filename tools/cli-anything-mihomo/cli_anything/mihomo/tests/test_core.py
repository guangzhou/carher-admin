"""Unit tests: synthetic data only, no network, no 198."""
from __future__ import annotations

import copy

import pytest
import yaml

from cli_anything.mihomo.core import config, exits, instance, shellcrash, subscription

SUB = {
    "mixed-port": 7890, "allow-lan": True, "bind-address": "*", "external-controller": "0.0.0.0:9090",
    "secret": "leak", "authentication": ["u:p"], "tun": {"enable": True, "auto-route": True},
    "redir-port": 7892, "tproxy-port": 7893, "bypass": ["x.com"], "external-ui": "ui",
    "dns": {"enable": True, "listen": "0.0.0.0:53", "enhanced-mode": "fake-ip"},
    "proxies": [
        {"name": "HK - a", "type": "socks5", "server": "1.2.3.4", "port": 1, "password": "pw"},
        {"name": "US - b", "type": "vmess", "server": "5.6.7.8", "port": 2, "uuid": "u-u-i-d"},
        {"name": "ChatGPT & AI 专线", "type": "http", "server": "9.9.9.9", "port": 3},
    ],
    "proxy-groups": [
        {"name": "🔰 节点选择", "type": "select", "proxies": ["auto-US", "HK - a", "US - b"]},
        {"name": "auto-US", "type": "url-test", "proxies": ["US - b"], "interval": 3600, "url": "http://x/204"},
    ],
    "rules": ["MATCH,🔰 节点选择"],
}


def state(**kw):
    return {**copy.deepcopy(config.DEFAULT_STATE), **kw}


class TestBuild:
    def test_strips_everything_that_hijacks_or_exposes(self):
        cfg = config.build(SUB, state())
        for k in ("tun", "redir-port", "tproxy-port", "secret", "authentication", "bypass", "external-ui"):
            assert k not in cfg, k
        assert cfg["allow-lan"] is False
        assert cfg["bind-address"] == "127.0.0.1"
        assert cfg["external-controller"] == "127.0.0.1:19090"
        assert "listen" not in cfg["dns"] and cfg["dns"]["enhanced-mode"] == "fake-ip"
        assert config.audit(cfg) == []

    def test_does_not_mutate_input(self):
        before = copy.deepcopy(SUB)
        config.build(SUB, state(pins=[{"port": 17891, "target": "US - b"}]))
        assert SUB == before

    def test_pins_become_localhost_listeners(self):
        cfg = config.build(SUB, state(pins=[{"port": 17891, "target": "auto-US"}, {"port": 17892, "target": "HK - a"}]))
        assert cfg["listeners"] == [
            {"name": "pin-17891", "type": "mixed", "listen": "127.0.0.1", "port": 17891, "proxy": "auto-US"},
            {"name": "pin-17892", "type": "mixed", "listen": "127.0.0.1", "port": 17892, "proxy": "HK - a"},
        ]
        assert config.audit(cfg) == []

    def test_pin_to_unknown_target(self):
        with pytest.raises(config.ConfigError, match="neither a node nor a group"):
            config.build(SUB, state(pins=[{"port": 17891, "target": "nope"}]))

    def test_duplicate_port(self):
        with pytest.raises(config.ConfigError, match="used twice"):
            config.build(SUB, state(pins=[{"port": 17890, "target": "HK - a"}]))

    def test_privileged_port(self):
        with pytest.raises(config.ConfigError, match="outside"):
            config.build(SUB, state(mixed_port=80))

    def test_default_group_must_exist(self):
        with pytest.raises(config.ConfigError, match="default_group"):
            config.build(SUB, state(default_group="nope"))

    def test_url_test_interval_override(self):
        cfg = config.build(SUB, state(url_test_interval=300))
        g = {g["name"]: g for g in cfg["proxy-groups"]}
        assert g["auto-US"]["interval"] == 300
        assert "interval" not in g["🔰 节点选择"]

    def test_interval_zero_keeps_feed_value(self):
        cfg = config.build(SUB, state(url_test_interval=0))
        assert {g["name"]: g for g in cfg["proxy-groups"]}["auto-US"]["interval"] == 3600

    def test_store_selected_on(self):
        assert config.build(SUB, state())["profile"]["store-selected"] is True

    def test_dump_roundtrip(self):
        cfg = config.build(SUB, state())
        assert yaml.safe_load(config.dump(cfg)) == cfg


class TestAudit:
    def test_flags_unsafe(self):
        bad = config.audit({"tun": {}, "allow-lan": True, "bind-address": "*", "external-controller": ":9090",
                            "dns": {"listen": ":53"}})
        assert {"tun present", "allow-lan true", "dns.listen set"} <= set(bad)
        assert any(b.startswith("bind-address") for b in bad)
        assert any(b.startswith("external-controller") for b in bad)

    def test_flags_public_listener(self):
        bad = config.audit({"bind-address": "127.0.0.1", "external-controller": "127.0.0.1:1",
                            "listeners": [{"listen": "0.0.0.0", "port": 1}]})
        assert "listeners present" in bad


class TestSubscription:
    def test_region(self):
        assert subscription.region("HK - 香港原生 - 04") == "HK"
        assert subscription.region("ChatGPT & AI 专线") == "OTHER"

    def test_summary(self):
        s = subscription.summary(SUB)
        assert s["proxies"] == 3 and s["by_region"] == {"HK": 1, "US": 1, "OTHER": 1}
        assert s["duplicate_names"] == []

    def test_nodes_have_no_secrets(self):
        text = repr(subscription.nodes(SUB))
        for secret in ("1.2.3.4", "pw", "u-u-i-d", "5.6.7.8"):
            assert secret not in text

    def test_userinfo(self):
        assert subscription.parse_userinfo("upload=1; download=2; total=3; expire=4; junk; x=y") == \
            {"upload": 1, "download": 2, "total": 3, "expire": 4}

    def test_load_rejects_non_clash(self, tmp_path):
        p = tmp_path / "s.yaml"
        p.write_text("dm1lc3M6Ly8=\n")  # a base64 feed is a YAML string, not a dict
        with pytest.raises(subscription.SubscriptionError, match="no proxies"):
            subscription.load(str(p))


class TestExits:
    ALL = {
        "🔰 节点选择": {"type": "Selector", "now": "auto-US"},
        "auto-US": {"type": "URLTest", "now": "US - b"},
        "US - b": {"type": "Vmess"},
        "loopA": {"type": "Selector", "now": "loopB"},
        "loopB": {"type": "Selector", "now": "loopA"},
    }

    def test_chain(self):
        assert exits.resolve_chain(self.ALL, "🔰 节点选择") == ["🔰 节点选择", "auto-US", "US - b"]

    def test_chain_leaf(self):
        assert exits.resolve_chain(self.ALL, "US - b") == ["US - b"]

    def test_chain_cycle_terminates(self):
        assert exits.resolve_chain(self.ALL, "loopA") == ["loopA", "loopB"]

    def test_scholar_hits(self):
        assert exits.scholar_hits('<div class="gs_ri">a</div><div class="gs_ri">b</div>') == 2
        assert exits.scholar_hits("<title>Error 403 (Forbidden)!!1</title>") == 0


class TestInstanceName:
    @pytest.mark.parametrize("bad", ["../x", "a b", "a/b", ""])
    def test_rejects(self, bad):
        with pytest.raises(config.ConfigError):
            instance.rdir(bad)

    def test_accepts(self):
        assert instance.rdir("e2e_1-a") == "mihomo-harness/e2e_1-a"


def test_shellcrash_facts_are_sourced():
    assert all(f["where"] for f in shellcrash.FACTS)
    assert [a["area"] for a in shellcrash.FIREWALL_AREAS if a["on_198"].startswith("OK")] == [4]


class TestAccounts:
    def test_account_dirs(self):
        assert subscription.account_dir("default") == subscription.BASE_DIR
        assert subscription.account_dir("acct2") == subscription.BASE_DIR + "-acct2"
        with pytest.raises(subscription.SubscriptionError):
            subscription.account_dir("../x")

    def test_use_account_switches_every_path(self, monkeypatch):
        monkeypatch.setattr(subscription, "_STATE_DIR", subscription.BASE_DIR)
        subscription.use_account("acct2")
        d = subscription.BASE_DIR + "-acct2"
        assert subscription.account() == "acct2"
        assert subscription.sub_url_file() == d + "/sub.url"
        assert subscription.sub_yaml() == d + "/sub.yaml"
        assert config.instances_dir() == d + "/instances"
        subscription.use_account(None)  # None keeps the current one
        assert subscription.account() == "acct2"
        subscription.use_account("default")
        assert subscription.account() == "default"

    def test_binary_cache_is_shared(self, monkeypatch):
        from cli_anything.mihomo.core import binary
        monkeypatch.setattr(subscription, "_STATE_DIR", subscription.BASE_DIR + "-acct2")
        assert binary.local_gz("v1.19.33").startswith(subscription.BASE_DIR + "/")


class TestOwnerGuard:
    def test_mismatch_refused(self, monkeypatch):
        monkeypatch.setattr(subscription, "_STATE_DIR", subscription.BASE_DIR + "-acct2")
        monkeypatch.setattr(instance.remote198, "run", lambda *a, **k: "default\n")
        with pytest.raises(instance.InstanceError, match="belongs to account 'default'"):
            instance.check_owner("default")

    def test_match_and_absent_ok(self, monkeypatch):
        monkeypatch.setattr(subscription, "_STATE_DIR", subscription.BASE_DIR + "-acct2")
        monkeypatch.setattr(instance.remote198, "run", lambda *a, **k: "acct2")
        assert instance.check_owner("acct2") == "acct2"
        monkeypatch.setattr(instance.remote198, "run", lambda *a, **k: "")
        assert instance.check_owner("new") is None

    @pytest.mark.parametrize("fn", ["stop", "start", "destroy", "push_config"])
    def test_every_mutation_checks_owner(self, monkeypatch, fn):
        monkeypatch.setattr(subscription, "_STATE_DIR", subscription.BASE_DIR + "-acct2")
        monkeypatch.setattr(instance.remote198, "run", lambda *a, **k: "default")
        with pytest.raises(instance.InstanceError, match="belongs to account"):
            getattr(instance, fn)("default")


class TestTableAndGeo:
    def _fake(self, monkeypatch, results):
        calls = {}

        def ctl(name, jobs, **kw):
            calls["jobs"], calls["kw"] = jobs, kw
            return results(jobs)
        monkeypatch.setattr(exits.instance, "ctl", ctl)
        return calls

    def test_table(self, monkeypatch):
        monkeypatch.setattr(exits, "ports", lambda name: [{"port": 1, "kind": "pin", "via": "X", "chain": ["X"], "exit_node": "X"}])
        ip = '{"ip":"1.2.3.4","country":"KR","city":"Seoul","org":"AS1 x"}'
        calls = self._fake(monkeypatch, lambda jobs: [{"status": 200, "text": ip, "ms": 5},
                                                      {"status": 404, "ms": 900}, {"status": 0, "error": "t", "ms": 15000},
                                                      {"status": 404, "ms": 1100}])
        r = exits.table("n", samples=3)[0]
        assert calls["kw"]["workers"] == 1  # serial: parallel probes skew a single node's latency
        assert (r["exit_ip"], r["country"], r["kiro_ms"], r["kiro_ok"]) == ("1.2.3.4", "KR", 1100, "2/3")

    def test_geo_retries_and_flags_label(self, monkeypatch):
        fr = '{"ip":"9.9.9.9","country":"FR","city":"Lauterbourg","org":"AS51167 Contabo"}'
        self._fake(monkeypatch, lambda jobs: [{"status": 0, "error": "reset"}, {"status": 200, "text": fr}, {"status": 200, "text": fr},
                                              {"status": 0, "error": "reset"}, {"status": 0, "error": "reset"}, {"status": 0, "error": "timeout"}])
        rows = exits.geo("n", [(1, "DE - 德国2线"), (2, "IN - 印度孟买")])
        assert rows[0]["ok"] and rows[0]["country"] == "FR" and rows[0]["label_region"] == "DE"
        assert not rows[1]["ok"] and rows[1]["error"] == "timeout"


class TestTunnelAndQuota:
    def test_tunnel_already_up_does_not_spawn(self, monkeypatch):
        monkeypatch.setattr(subscription, "_listening", lambda port: True)
        monkeypatch.setattr(subscription.subprocess, "run", lambda *a, **k: pytest.fail("must not start ssh"))
        assert subscription.ensure_tunnel("socks5h://127.0.0.1:18880")["started"] is False

    def test_tunnel_never_uses_clear_all_forwardings(self, monkeypatch):
        seen = {}
        state = iter([False, True])
        monkeypatch.setattr(subscription, "_listening", lambda port: next(state))
        def run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")
        import subprocess
        monkeypatch.setattr(subscription.subprocess, "run", run)
        assert subscription.ensure_tunnel("socks5h://127.0.0.1:18880")["started"] is True
        assert "-D" in seen["cmd"] and not any("ClearAllForwardings" in c for c in seen["cmd"])

    def test_tunnel_not_managed_for_other_proxies(self):
        assert subscription.ensure_tunnel("http://10.0.0.1:3128")["managed"] is False

    def test_accounts_lists_dirs_with_url(self, tmp_path, monkeypatch):
        base = tmp_path / "cli-anything-mihomo"
        for d in (base, tmp_path / "cli-anything-mihomo-acct2", tmp_path / "cli-anything-mihomo-empty"):
            d.mkdir()
        (base / "sub.url").write_text("u")
        (tmp_path / "cli-anything-mihomo-acct2" / "sub.url").write_text("u")
        monkeypatch.setattr(subscription, "BASE_DIR", str(base))
        assert subscription.accounts() == ["default", "acct2"]

    def test_failed_fetch_keeps_last_good_headers(self, tmp_path, monkeypatch):
        monkeypatch.setattr(subscription, "_STATE_DIR", str(tmp_path))
        (tmp_path / "sub.url").write_text("https://feed.example/x\n")
        (tmp_path / "sub.headers").write_text("subscription-userinfo: upload=0; download=1; total=2; expire=3\n")
        import subprocess
        monkeypatch.setattr(subscription, "ensure_tunnel", lambda p: {})
        monkeypatch.setattr(subscription.time, "sleep", lambda s: None)
        monkeypatch.setattr(subscription.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 97, "", "curl: (97) connection to proxy closed"))
        with pytest.raises(subscription.SubscriptionError, match="rc=97"):
            subscription.fetch("socks5h://127.0.0.1:18880")
        assert subscription.quota()["total_bytes"] == 2  # previous header still readable

    def test_quota_cli_warns(self, tmp_path, monkeypatch):
        from click.testing import CliRunner
        from cli_anything.mihomo import mihomo_cli
        monkeypatch.setattr(subscription, "_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(subscription, "use_account", lambda a: None)
        (tmp_path / "sub.headers").write_text(f"subscription-userinfo: upload=0; download=90; total=100; expire={2**31}\n")
        r = CliRunner().invoke(mihomo_cli.cli, ["--json", "sub", "quota", "--cached"])
        assert r.exit_code == 1 and '"status": "WARN"' in r.output
        r = CliRunner().invoke(mihomo_cli.cli, ["--json", "sub", "quota", "--cached", "--warn-pct", "95"])
        assert r.exit_code == 0 and '"status": "OK"' in r.output


class TestReset:
    def test_next_reset_same_month(self):
        import time as t
        exp = int(t.mktime((2027, 10, 28, 12, 0, 0, 0, 0, -1)))
        now = t.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1))
        assert subscription.next_reset(exp, now) == "2026-11-28"
        now = t.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))
        assert subscription.next_reset(exp, now) == "2026-11-28"

    def test_next_reset_short_month_and_year_end(self):
        import time as t
        exp = int(t.mktime((2027, 1, 31, 12, 0, 0, 0, 0, -1)))
        assert subscription.next_reset(exp, t.mktime((2027, 2, 3, 0, 0, 0, 0, 0, -1))) == "2027-02-28"
        assert subscription.next_reset(exp, t.mktime((2026, 12, 31, 13, 0, 0, 0, 0, -1))) == "2027-01-31"

    def test_no_expire(self):
        assert subscription.next_reset(None) is None


def test_account_status_paused_at_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(subscription, "_STATE_DIR", str(tmp_path))
    (tmp_path / "sub.headers").write_text("subscription-userinfo: upload=0; download=200; total=200; expire=1824717469\n")
    assert subscription.quota()["account_status"] == "暂停"
    (tmp_path / "sub.headers").write_text("subscription-userinfo: upload=0; download=24; total=200; expire=1824717469\n")
    q = subscription.quota()
    assert q["account_status"] == "正常" and q["left_gb"] is not None
