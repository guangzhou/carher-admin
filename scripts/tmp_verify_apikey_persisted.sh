#!/usr/bin/env bash
# DB 里 litellm_params 是加密存的,`like '%api_key%'` 只能证明字段名在,
# 证不了值对。这里做真正的判据: 让 proxy 解密后比对 9router 自己那把
# litellm-bridge key —— 只比 sha256 前 12 位,明文不出容器、不过 jms。
#
# 为什么必须验: 重启会从 DB 重新加载部署配置。如果 DB 里存的还是我猜的
# "router9",现在 worker 内存里的正确值就只是临时的,下次重启 401 复活。
set -uo pipefail
NS=litellm-product
LP=$(sudo kubectl -n $NS get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
RP=$(sudo kubectl -n $NS get pods -l app=9router -o jsonpath='{.items[0].metadata.name}')
echo "[pods] litellm=$LP 9router=$RP"

cat > /tmp/tmp_9r_fp_v1.js <<'JS'
const crypto = require('crypto');
const D = require('/app/node_modules/better-sqlite3');
const db = new D('/app/data/db/data.sqlite', {readonly:true});
const r = db.prepare("select key from apiKeys where isActive=1 and name='litellm-bridge'").get();
if (!r) { process.stderr.write('no active litellm-bridge key\n'); process.exit(1); }
process.stdout.write('__FP__' + crypto.createHash('sha256').update(r.key).digest('hex').slice(0,12)
                     + ' len=' + r.key.length);
JS
sudo kubectl -n $NS cp /tmp/tmp_9r_fp_v1.js "$NS/$RP:/tmp/tmp_9r_fp_v1.js" 2>/dev/null
NINE=$(sudo kubectl -n $NS exec -i "$RP" -- node /tmp/tmp_9r_fp_v1.js < /dev/null 2>/dev/null)
echo "[9router] apiKeys.litellm-bridge  ${NINE#__FP__}"

cat > /tmp/tmp_lp_fp_v1.py <<'PY'
import hashlib, json, os, urllib.request, urllib.error
BASE="http://127.0.0.1:4000"; MK=os.environ["LITELLM_MASTER_KEY"]
IDS=["9router/claude-fable-5-1-medium","9router/claude-opus-5-medium",
     "9router/composer-2.5"]
def call(p):
    r=urllib.request.Request(BASE+p,headers={"Authorization":"Bearer "+MK})
    return json.load(urllib.request.urlopen(r,timeout=115))

# /model/info 抹掉 api_key,所以走 /v1/model/info?...  也一样抹。
# 唯一能拿到解密值的地方是 router 自己的内存: proxy 进程里的 llm_router。
try:
    import litellm.proxy.proxy_server as ps
    router = ps.llm_router
    seen = 0
    for d in (router.model_list or []):
        mid = str((d.get("model_info") or {}).get("id"))
        if mid.startswith("9router/"):
            k = (d.get("litellm_params") or {}).get("api_key") or ""
            fp = hashlib.sha256(k.encode()).hexdigest()[:12] if k else "(empty)"
            print(f"[litellm] {mid:34} api_key sha256={fp} len={len(k)}")
            seen += 1
    if not seen:
        print("[litellm] router.model_list 里没有 9router/* —— 该进程还没加载?")
except Exception as e:
    print("[litellm] 读 llm_router 失败:", type(e).__name__, str(e)[:200])
PY
sudo kubectl -n $NS cp /tmp/tmp_lp_fp_v1.py "$NS/$LP:/tmp/tmp_lp_fp_v1.py" 2>/dev/null
echo "[note] 下面是从 proxy 进程外单独起的 python,读不到 llm_router 是预期的;"
echo "       真正的判据在其后的 DB 解密腿。"
sudo kubectl -n $NS exec -i "$LP" -c litellm -- python3 /tmp/tmp_lp_fp_v1.py < /dev/null

# 判据腿: 用 proxy 自己的解密函数直接解 DB 里那两行
cat > /tmp/tmp_db_dec_v1.py <<'PY'
import hashlib, json, os, subprocess
os.chdir("/app")
# 只用解密函数,不碰 PrismaClient —— 它的 import 链上有个在这个版本里
# 已经改名的私有符号(_get_parent_otel_span_from_metadata),会直接 ImportError。
from litellm.proxy.common_utils.encrypt_decrypt_utils import decrypt_value_helper  # noqa: E402
import litellm.proxy.proxy_server as _ps                                            # noqa: E402

# 解密用的 salt: _get_salt_key() 读 LITELLM_SALT_KEY,没有就退回
# proxy_server.master_key。独立进程里那个模块级变量是 None,所以必须自己填,
# 否则 decrypt 一律失败、raw 原样返回,读出来就是"没解密"的假红。
if not os.getenv("LITELLM_SALT_KEY"):
    _ps.master_key = os.environ["LITELLM_MASTER_KEY"]

SQL = ("select model_id, litellm_params::text from \"LiteLLM_ProxyModelTable\" "
       "where model_id like '9router/%';")
out = subprocess.run(
    ["python3", "-c", """
import json,os,urllib.request
"""], capture_output=True, text=True)

# DB 行从 psql 拿(proxy pod 里没有 psql,所以由外层 shell 传进来)
rows = json.loads(os.environ["DB_ROWS"])
for mid, lp_text in rows:
    lp = json.loads(lp_text)
    raw = lp.get("api_key")
    # 第二个参数只是报错时的字段标签,不是签名密钥(签名密钥来自 SALT_KEY)
    dec = decrypt_value_helper(raw, "api_key") if raw else None
    val = dec if dec else raw
    fp = hashlib.sha256((val or "").encode()).hexdigest()[:12]
    print(f"[DB] {mid:34} api_key sha256={fp} len={len(val or '')} "
          f"decrypted={'yes' if dec else 'NO(raw-or-plaintext)'}")
PY
sudo kubectl -n $NS cp /tmp/tmp_db_dec_v1.py "$NS/$LP:/tmp/tmp_db_dec_v1.py" 2>/dev/null

# DB 行由 db pod 取出(proxy pod 里没有 psql),再交给 proxy pod 解密
DB_ROWS=$(sudo kubectl -n $NS exec -i litellm-db-0 -- psql -U litellm -d litellm -A -t -c \
  "select json_agg(json_build_array(model_id, litellm_params::text)) from \"LiteLLM_ProxyModelTable\" where model_id like '9router/%';" < /dev/null 2>/dev/null | tr -d '\n')
sudo kubectl -n $NS exec -i "$LP" -c litellm -- env DB_ROWS="$DB_ROWS" python3 /tmp/tmp_db_dec_v1.py < /dev/null
echo
echo "判据: [DB] 两行的 sha256 必须等于 [9router] 那个。不等 = 重启后 401 会复活。"
