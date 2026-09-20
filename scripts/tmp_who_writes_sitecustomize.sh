#!/usr/bin/env bash
# sitecustomize.py 的 mtime == 容器启动时刻,但它不在 containers[0].volumeMounts 里。
# 这是重启前必须回答的问题: 谁在启动时把它放进 venv?
#   - initContainer / lifecycle hook / entrypoint 写的  => 重启会重新写,不会丢
#   - 别人某次 kubectl cp 进去的                        => 重启就没了
set -uo pipefail
NS=litellm-product

echo "=== 所有容器(含 init)的 command/args/lifecycle + 带 sitecustom 的挂载 ==="
sudo kubectl -n $NS get deploy litellm-proxy -o json > /tmp/lp_deploy.json
python3 - <<'PY'
import json
d = json.load(open('/tmp/lp_deploy.json'))['spec']['template']['spec']
for kind in ('initContainers', 'containers'):
    for c in d.get(kind) or []:
        print(f"== {kind}/{c['name']}  image={c.get('image')}")
        print("   command =", c.get('command'))
        print("   args    =", c.get('args'))
        if c.get('lifecycle'):
            print("   lifecycle =", json.dumps(c['lifecycle'])[:900])
        for m in c.get('volumeMounts') or []:
            blob = (m.get('subPath') or '') + m['mountPath']
            if 'sitecustom' in blob.lower():
                print("   SITECUSTOM MOUNT:", json.dumps(m))
print("=== volumes ===")
for v in d.get('volumes') or []:
    src = v.get('configMap') or v.get('secret') or v.get('emptyDir') or v
    print(" ", v['name'], '->', json.dumps(src)[:300])
PY

echo
echo "=== 各 CM 的 key 列表(找 sitecustomize) ==="
for CM in litellm-callbacks litellm-hooks litellm-config litellm-deepcopy-patch; do
  echo "--- $CM ---"
  sudo kubectl -n $NS get cm $CM -o json > /tmp/cm_$CM.json 2>/dev/null || { echo "(读不到)"; continue; }
  python3 -c "
import json
ks = sorted(json.load(open('/tmp/cm_$CM.json')).get('data') or {})
print(' ', len(ks), 'keys')
hit = [k for k in ks if 'sitecustom' in k.lower()]
print('  sitecustomize key:', hit or '(无)')
"
done

echo
echo "=== 容器内: 谁能在启动时写 venv? 看 entrypoint 脚本 ==="
P=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
sudo kubectl -n $NS exec -i "$P" -c litellm -- sh -c '
  for F in /app/entrypoint.sh /entrypoint.sh /app/start.sh /docker-entrypoint.sh; do
    [ -f "$F" ] && { echo "--- $F ---"; cat "$F"; }
  done
  echo "--- grep sitecustomize 在 /app 顶层脚本里 ---"
  grep -rln sitecustomize /app --include="*.sh" --include="Dockerfile*" 2>/dev/null | head
' < /dev/null
