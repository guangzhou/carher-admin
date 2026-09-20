#!/usr/bin/env python3
"""crg_key_grant.py —— 把 14 个 cr-g-* 池名授权给 key，读-合并-写。

三条踩过的规矩：
  · **`/key/update` 的 `models` 是整表覆盖，不是追加**。必须先 `/key/info` 读旧表、
    在本地合并、再整份写回。直接写 14 个 = 把这把 key 的其余 90 个模型全删了。
  · **`/key/update` 吃 `key` 不吃 `key_alias`**（传 alias 报 422 missing）。
    DB `LiteLLM_VerificationToken.token` 那个 64 位哈希可以直接当 `key` 传。
  · 改前整份备份到 198 `/Data/backups/`（属 root，要 `sudo -n tee` 并回读 sha256 核对）。
    回滚 = 把备份里的 models 数组整份写回。

用法：
    python3 crg_key_grant.py                                   # dry-run，默认对我自己的 key
    python3 crg_key_grant.py --apply
    python3 crg_key_grant.py --alias cursor-xxx --alias cursor-yyy [--apply]   # 09-03 起可给别人，多把
只加 14 个池名；**不加 -82/-135 直连名**（那是我钉单腿调试用的），**不动 aliases**（同事的
glm/kimi 映射是团队标配；我 key 上那 8 条 chatgpt-*→sa-gpt-* 是我自己的实验，别照搬）。
09-03 实跑 shangwensheng / linsen ×2：40→54、42→56、40→54，DB 直读核对。
"""
import json
import os
import subprocess
import sys
import time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SUDO = "sudo -n kubectl -n %s " % NS
def _require_env(name):
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：写死一个真 PG 口令等于把凭据提交进仓库，而且口令轮转后
    老默认值还会静默生效，打出来的认证失败看不出是"忘了设 env"还是"口令真的换了"。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<litellm-db-0 的 PG 口令>（别写进文件/命令行历史）"
            % (name, name)
        )
    return v


PG_PW = _require_env("LITELLM_PG_PW")
# 默认给我自己的 key;09-03 起支持 --alias <key_alias>(可多次)给别人。
KEY_ALIAS = "cursor-liuguoxian04-5rub"
GRANT = [
    "cr-g-5.6", "cr-g-5.6-instant", "cr-g-5.6-mini", "cr-g-5.6-t-mini",
    "cr-g-5.6-pro", "cr-g-research", "cr-g-5.6-thinking", "cr-g-5.6-thinking-min",
    "cr-g-5.6-thinking-high", "cr-g-5.6-thinking-max",
    "cr-g-5.6-luna", "cr-g-5.6-luna-min", "cr-g-5.6-luna-high", "cr-g-5.6-luna-max",
]


def sh(cmd, stdin=None, timeout=180):
    return subprocess.run(SSH + [cmd], input=stdin, capture_output=True,
                          text=True, timeout=timeout)


def proxy_py(prog):
    r = sh(SUDO + "exec -i deploy/litellm-proxy -- python3 -", stdin=prog)
    line = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if not line:
        raise SystemExit("proxy 调用失败:\n  %s\n  %s" % (r.stdout[-400:], r.stderr[-400:]))
    return json.loads(line[-1])


def main():
    aliases = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--alias" and i + 1 < len(sys.argv)]
    rc = 0
    for a in (aliases or [KEY_ALIAS]):
        print("\n########## %s ##########" % a)
        rc |= grant_one(a) or 0
    return rc


def grant_one(KEY_ALIAS):
    apply = "--apply" in sys.argv
    ts = time.strftime("%Y%m%d-%H%M%S")

    # token(hash) 不经 argv 拼进命令串，只在 pod 内和这里的变量里流转
    tok = sh(SUDO + "exec litellm-db-0 -- env PGPASSWORD='%s' psql -U litellm -d litellm "
             "-At -c \"select token from \\\"LiteLLM_VerificationToken\\\" "
             "where key_alias='%s';\" 2>/dev/null" % (PG_PW, KEY_ALIAS)).stdout.strip()
    if len(tok) != 64:
        raise SystemExit("按 alias 查不到唯一 token（拿到 %d 字符）" % len(tok))
    print("key alias=%s  token=%s… (只回显前 12 位)" % (KEY_ALIAS, tok[:12]))

    info = proxy_py(
        "import urllib.request,json,os\n"
        "req=urllib.request.Request('http://localhost:4000/key/info?key=%s',"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']})\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req,timeout=30))))\n" % tok)
    old = list((info.get("info") or {}).get("models") or [])
    print("现有模型数 = %d" % len(old))

    merged = list(old) + [m for m in GRANT if m not in old]
    added = [m for m in GRANT if m not in old]
    print("将新增 %d 个: %s" % (len(added), ", ".join(added) or "(无，已全在)"))
    print("改后模型数 = %d" % len(merged))
    if not apply:
        print("\n(dry-run。加 --apply 执行)")
        return 0
    if not added:
        print("无需改动。")
        return 0

    # 备份（/Data/backups 属 root → sudo -n tee；回读 sha256 核对写进去的就是这份）
    path = "/Data/backups/key-%s-%s-pre-crg-pool.json" % (KEY_ALIAS.replace("cursor-", ""), ts)
    blob = json.dumps(info, ensure_ascii=False, sort_keys=True, indent=1)
    r = sh("sudo -n tee %s >/dev/null && sudo -n sha256sum %s" % (path, path), stdin=blob)
    remote_sha = r.stdout.split()[0] if r.stdout.strip() else ""
    import hashlib
    local_sha = hashlib.sha256(blob.encode()).hexdigest()
    if remote_sha != local_sha:
        raise SystemExit("备份 sha 不符，停手\n  远端 %s\n  本地 %s" % (remote_sha, local_sha))
    print("备份 %s  sha256=%s… ✅" % (path, local_sha[:16]))

    payload = json.dumps({"key": tok, "models": merged})
    res = proxy_py(
        "import urllib.request,json,os\n"
        "p=json.loads(%r)\n"
        "req=urllib.request.Request('http://localhost:4000/key/update',"
        "data=json.dumps(p).encode(),"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],"
        "'Content-Type':'application/json'})\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req,timeout=30))))\n" % payload)
    if "error" in res:
        raise SystemExit("update 失败: %s" % json.dumps(res)[:400])

    # 判据：**写后重读**，不信 update 的返回（写后复查≠改动生效，但这里要的正是
    # "库里现在是什么"，重读是最直接的那一步）
    back = proxy_py(
        "import urllib.request,json,os\n"
        "req=urllib.request.Request('http://localhost:4000/key/info?key=%s',"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']})\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req,timeout=30))))\n" % tok)
    now = list((back.get("info") or {}).get("models") or [])
    miss = [m for m in GRANT if m not in now]
    print("\n重读: 模型数 = %d（期望 %d）" % (len(now), len(merged)))
    if miss:
        print("❌ 这些名字没进去: %s" % ", ".join(miss))
        return 1
    if len(now) != len(merged):
        print("❌ 数量对不上")
        return 1
    print("✅ 14 个池名逐个在表里")
    print("回滚: 把 %s 里的 info.models 整份写回 /key/update" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
