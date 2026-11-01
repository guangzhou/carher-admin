"""What ShellCrash does on a host, read from its source (juewuy/ShellCrash b6e4c629, 2026-10-24).

ShellCrash = shell menus around the same mihomo (it calls it CrashCore) + a firewall layer.
This harness keeps the core and drops the firewall layer; this table is why. Every row names
the file:line it was read from, so it can be re-checked when ShellCrash changes.
"""
from __future__ import annotations

SOURCE_REV = "juewuy/ShellCrash@b6e4c629 (2026-10-24)"

FIREWALL_AREAS = [
    {"area": 1, "label": "仅局域网", "hijacks": "LAN clients (PREROUTING)", "ip_forward": True, "resolv_conf": False,
     "on_198": "NO: turns 198 into a gateway; rewrites PREROUTING that k3s/flannel also uses"},
    {"area": 2, "label": "仅本机", "hijacks": "198's own traffic (OUTPUT)", "ip_forward": False, "resolv_conf": True,
     "on_198": "NO: every pod/process on 198 (litellm, kiro) would be routed by the rules; adds nameserver 127.0.0.1"},
    {"area": 3, "label": "局域网+本机", "hijacks": "both", "ip_forward": True, "resolv_conf": True,
     "on_198": "NO: both of the above"},
    {"area": 4, "label": "纯净模式", "hijacks": "nothing (only listens)", "ip_forward": False, "resolv_conf": False,
     "on_198": "OK: equivalent to what this harness runs"},
    {"area": 5, "label": "旁路转发", "hijacks": "forwards to another bypass router", "ip_forward": True, "resolv_conf": False,
     "on_198": "NO: adds a default route in a custom table"},
]

REDIR_MODES = [
    {"mode": "Redir", "tcp": "iptables REDIRECT", "udp": "-"},
    {"mode": "Mix", "tcp": "iptables REDIRECT", "udp": "tun"},
    {"mode": "Tproxy", "tcp": "TPROXY", "udp": "TPROXY"},
    {"mode": "Tun", "tcp": "tun", "udp": "tun"},
]

FACTS = [
    {"fact": "Pure mode is firewall_area=4; fw_start.sh skips all routing/firewall when area==4",
     "where": "scripts/starts/fw_start.sh:18"},
    {"fact": "Areas 1/3/5 write net.ipv4.ip_forward=1 into /etc/sysctl.conf (persistent)",
     "where": "scripts/starts/fw_start.sh:43-47"},
    {"fact": "Areas 2/3 insert `nameserver 127.0.0.1` into /etc/resolv.conf (non-container)",
     "where": "scripts/starts/fw_start.sh:55-58"},
    {"fact": "Generated config always sets allow-lan: true and external-controller on all interfaces (:9999)",
     "where": "scripts/starts/clash_modify.sh:93,97"},
    {"fact": "Default ports: mixed 7890, dashboard/controller 9999; secret empty unless set in menu",
     "where": "scripts/libs/get_config.sh:9,12"},
    {"fact": "No redir_mod configured => area forced to 4 (pure)",
     "where": "scripts/starts/fw_start.sh:15"},
    {"fact": "`crash -s start|stop|restart` = scripts/start.sh actions; everything else is an interactive menu",
     "where": "scripts/menu.sh (case \"$1\"), scripts/start.sh:39-112"},
    {"fact": "Docker image needs `docker exec -it ... sh -l` to import a subscription (menu-driven, not declarative)",
     "where": "docker/README.md"},
]

EQUIVALENTS = [
    {"shellcrash": "crash menu 6 -> import subscription URL", "here": "sub set-url / sub fetch"},
    {"shellcrash": "crash -s start / stop", "here": "instance start / stop"},
    {"shellcrash": "dashboard :9999 node switch", "here": "exit select GROUP NODE"},
    {"shellcrash": "dashboard latency test", "here": "exit measure"},
    {"shellcrash": "task: auto-update subscription", "here": "sub fetch && instance reload"},
    {"shellcrash": "routing modes (Redir/Tproxy/Tun)", "here": "not offered on 198 (see `shellcrash modes`)"},
]
