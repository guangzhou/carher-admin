"""在本机跑 worker 被杀分诊，传输换成 ssh key + `sudo -n`（不读记忆里的密码）。

用法：python3 scripts/litellm-198-worker-kill-triage-keyauth.py --lane all --since 30m --probe
（参数与 scripts/litellm-198-worker-kill-triage.py 相同；198 sudo 密码明文问题见记忆 project_198_cltx_sudo_password_in_git_2026_10_14）
"""
import base64, importlib.util, re, subprocess, sys
spec = importlib.util.spec_from_file_location("t", __import__("pathlib").Path(__file__).with_name("litellm-198-worker-kill-triage.py"))
t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)
def remote(script, pw, timeout=300):
    b64 = base64.b64encode(script.encode()).decode()
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", t.HOST,
                        f"sudo -n bash -c \"$(echo {b64} | base64 -d)\""], capture_output=True, text=True, timeout=timeout)
    out = p.stdout + "\n".join(l for l in p.stderr.splitlines() if "remote port forwarding" not in l)
    return re.sub(r"sk-[A-Za-z0-9._~-]{8,}", "<R>", out)
t.remote = remote; t.ssh_password = lambda: ""
sys.exit(t.main())
