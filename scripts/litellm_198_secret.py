"""litellm_198_secret.py —— 198 生产网关的 secret 名**此刻**叫什么(给别的脚本 import 用)。

为什么要有它(2026-10-29):
  10-28 17:41 master key 被轮换,proxy 改从 `secret/litellm-secrets-r2` envFrom 读,
  旧的 `litellm-secrets` 被删。仓里 100 来处写死了旧名,当天一批脚本直接 FATAL
  (最典型的是从它解 DATABASE_URL 的那些,连只读查询都跑不了)。
  这次统一改,但**不改成写死 `-r2`** —— 下次轮换叫 `-r3` 又会全断。改成跟着
  `deploy/litellm-proxy` 的 envFrom 走:proxy 用哪个,脚本就读哪个。

只管 198 `litellm-product`(AIYJY-litellm 是同一个集群)。**不要**拿它去找:
  - ACK `carher` 命名空间的 `litellm-secrets`(那边没轮换,名字仍是旧的)
  - 198 `litellm-dev` / `litellm-staging`(也没轮换)

用法:
  from litellm_198_secret import secret_name, SHELL_RESOLVE
  name = secret_name(run)     # run(cmd:str)->str,在 198 上执行一条 shell 命令
  # 或者把 SHELL_RESOLVE 拼进你要发到 198 的那段脚本,之后用 "$LITELLM_SECRET"
"""

NS = "litellm-product"
FALLBACK = "litellm-secrets-r2"   # envFrom 读不出来时才用(并在 stderr 说一声)

# 拼进远端 shell 用:执行后 $LITELLM_SECRET 就是当前名字。
# 挑「envFrom 里第一个带 LITELLM_MASTER_KEY 的 secret」—— 与 litellm-198-master-key.sh 同一判据。
SHELL_RESOLVE = r'''
LITELLM_SECRET=""
for __s in $(sudo -n kubectl -n litellm-product get deploy litellm-proxy -o jsonpath='{range .spec.template.spec.containers[0].envFrom[*]}{.secretRef.name}{"\n"}{end}' 2>/dev/null); do
  if sudo -n kubectl -n litellm-product get secret "$__s" -o jsonpath='{.data.LITELLM_MASTER_KEY}' 2>/dev/null | grep -q .; then LITELLM_SECRET="$__s"; break; fi
done
[ -n "$LITELLM_SECRET" ] || { LITELLM_SECRET=litellm-secrets-r2; echo "!! 从 deploy/litellm-proxy 读不出 secret 名,退回 $LITELLM_SECRET" >&2; }
'''


def secret_name(run) -> str:
    """run(cmd) 在 198 上执行 shell 并返回 stdout。返回当前 secret 名。"""
    import sys
    out = run(SHELL_RESOLVE + 'printf "%s" "$LITELLM_SECRET"')
    name = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else ""
    if not name:
        print(f"!! 读不出 secret 名,退回 {FALLBACK}", file=sys.stderr)
        return FALLBACK
    return name


# ── 按命名空间给名字(参数化 namespace 的脚本用这个)──────────────────────────
# 10-29 实测:同一个名字在不同 ns 里不一样 —— 只有 198 `litellm-product`(含 AIYJY-litellm,
# 同一集群)轮换成了 -r2;198 `litellm-dev` / `litellm-staging`、ACK `carher` 仍是旧名。
# ⇒ 带 --namespace 参数的脚本**不能**整体改成 -r2,得按 ns 取。
ROTATED = {"litellm-product": "litellm-secrets-r2"}
UNROTATED = "litellm-secrets"


def name_for(namespace: str) -> str:
    """静态映射:只看 ns。要「跟着 proxy 实际在用的」那个,用 secret_name()/SHELL_RESOLVE。"""
    return ROTATED.get(namespace, UNROTATED)
