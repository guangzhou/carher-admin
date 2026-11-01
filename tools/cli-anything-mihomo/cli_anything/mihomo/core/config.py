"""Build a 198-safe mihomo config from the subscription + this instance's local state.

"Safe" here means what ShellCrash's 纯净模式 (firewall_area=4) gives you, enforced in the
file instead of by a menu: listen on 127.0.0.1 only, no tun, no redir/tproxy ports, no DNS
listener, controller on 127.0.0.1 with the secret injected at start time by env (never in
the file). Everything that would hijack 198's traffic (iptables/nft, tun auto-route) is
simply never configured. See MIHOMO.md "Why not ShellCrash's other modes".
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

import yaml

from cli_anything.mihomo.core import subscription

DEFAULT_STATE: dict[str, Any] = {
    "mixed_port": 17890,
    "ctl_port": 19090,
    "pins": [],               # [{"port": 17891, "target": "<group or node name>"}]
    "url_test_interval": 300,  # the feed ships 3600s; a dead node then sticks for up to an hour
    "default_group": "🔰 节点选择",
}
# keys that would make 198 a gateway / expose it / hijack traffic
STRIP_KEYS = ("tun", "redir-port", "tproxy-port", "port", "socks-port", "external-ui", "external-ui-url",
              "external-ui-name", "external-controller-tls", "external-controller-unix", "external-doh-server",
              "secret", "authentication", "bypass", "listeners", "tuic-server", "ss-config", "vmess-config",
              "interface-name", "routing-mark", "iptables", "ebpf", "auto-redirect")
PORT_RANGE = (1024, 65535)


class ConfigError(ValueError):
    pass


def instances_dir() -> str:
    return os.path.join(subscription.state_dir(), "instances")


def list_states() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(instances_dir()) if f.endswith(".json"))
    except FileNotFoundError:
        return []


def _state_path(name: str) -> str:
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"instance name {name!r}: letters, digits, - and _ only")
    return os.path.join(instances_dir(), f"{name}.json")


def load_state(name: str) -> dict[str, Any]:
    try:
        with open(_state_path(name)) as fh:
            st = json.load(fh)
    except FileNotFoundError:
        st = {}
    return {**copy.deepcopy(DEFAULT_STATE), **st}


def save_state(name: str, st: dict[str, Any]) -> None:
    os.makedirs(instances_dir(), mode=0o700, exist_ok=True)
    path = _state_path(name)
    tmp = path + ".part"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def names_in(data: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(node names, group names) defined by the subscription."""
    nodes = {str(p.get("name")) for p in data.get("proxies") or []}
    groups = {str(g.get("name")) for g in data.get("proxy-groups") or []}
    return nodes, groups


def check_ports(st: dict[str, Any]) -> list[int]:
    ports = [int(st["mixed_port"]), int(st["ctl_port"]), *(int(p["port"]) for p in st["pins"])]
    for p in ports:
        if not PORT_RANGE[0] <= p <= PORT_RANGE[1]:
            raise ConfigError(f"port {p} outside {PORT_RANGE}")
    dup = sorted({p for p in ports if ports.count(p) > 1})
    if dup:
        raise ConfigError(f"port(s) used twice: {dup}")
    return ports


def build(data: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    """Return the config dict mihomo will run. Pure: no I/O."""
    check_ports(st)
    nodes, groups = names_in(data)
    if st["default_group"] not in groups:
        raise ConfigError(f"default_group {st['default_group']!r} not in subscription groups")
    for pin in st["pins"]:
        if pin["target"] not in nodes | groups | {"DIRECT"}:
            raise ConfigError(f"pin {pin['port']}: target {pin['target']!r} is neither a node nor a group")

    cfg = copy.deepcopy(data)
    for k in STRIP_KEYS:
        cfg.pop(k, None)
    cfg.update({
        "mixed-port": int(st["mixed_port"]),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "external-controller": f"127.0.0.1:{int(st['ctl_port'])}",
        "log-level": cfg.get("log-level") or "info",
        "find-process-mode": "off",
        "profile": {**(cfg.get("profile") or {}), "store-selected": True},
    })
    if isinstance(cfg.get("dns"), dict):
        cfg["dns"] = {k: v for k, v in cfg["dns"].items() if k != "listen"}
    interval = int(st.get("url_test_interval") or 0)
    for g in cfg.get("proxy-groups") or []:
        if interval and g.get("type") in ("url-test", "fallback", "load-balance"):
            g["interval"] = interval
    if st["pins"]:
        cfg["listeners"] = [
            {"name": f"pin-{int(p['port'])}", "type": "mixed", "listen": "127.0.0.1",
             "port": int(p["port"]), "proxy": p["target"]}
            for p in st["pins"]
        ]
    return cfg


def audit(cfg: dict[str, Any]) -> list[str]:
    """Problems that would make the config unsafe on 198. Empty list = OK."""
    bad: list[str] = []
    for k in STRIP_KEYS:
        if k in cfg and not (k == "listeners" and all(l.get("listen") == "127.0.0.1" for l in cfg[k])):
            bad.append(f"{k} present")
    if cfg.get("allow-lan"):
        bad.append("allow-lan true")
    if cfg.get("bind-address") not in ("127.0.0.1", "localhost"):
        bad.append(f"bind-address {cfg.get('bind-address')!r}")
    if not str(cfg.get("external-controller", "")).startswith("127.0.0.1:"):
        bad.append(f"external-controller {cfg.get('external-controller')!r}")
    if isinstance(cfg.get("dns"), dict) and cfg["dns"].get("listen"):
        bad.append("dns.listen set")
    return bad


def dump(cfg: dict[str, Any]) -> str:
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
