# 在 open-webui pod 里跑。走 OWUI 自己的 admin API 改配置(v0.11.4 的 Config.get 每次直读
# config 表、无缓存 ⇒ 立即生效,不用重启 OWUI;重启 = Recreate 单副本硬中断)。
#
# ARGS: {"allowed": [...], "apply": bool}
#   1. ENABLE_DIRECT_CONNECTIONS=false   —— 堵用户自己填 API 绕过反代
#   2. DEFAULT_MODELS = allowed[0]       —— 新会话默认选中它
#   3. base_model_id 不在白名单的 active workspace 模型 → toggle 成停用
#      (否则列表里留一个点了就 "Model not found" 的死项)
# 读-改-写:先 GET 整份表单再只改目标字段,别的字段原样回写。
import sqlite3, urllib.parse, urllib.request

from open_webui.utils.auth import create_token

allowed = ARGS["allowed"]
apply = ARGS["apply"]
db = sqlite3.connect("/app/backend/data/webui.db")
admin = db.execute("select id from user where role='admin' order by created_at limit 1").fetchone()[0]
H = {"Authorization": "Bearer " + create_token({"id": admin}), "Content-Type": "application/json"}
U = "http://127.0.0.1:8080/api/v1"


def api(method, path, body=None):
    r = urllib.request.Request(U + path, method=method, headers=H,
                               data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(r, timeout=60))


plan = []
conn = api("GET", "/configs/connections")
if conn.get("ENABLE_DIRECT_CONNECTIONS"):
    plan.append(("POST", "/configs/connections", {**conn, "ENABLE_DIRECT_CONNECTIONS": False}))
mcfg = api("GET", "/configs/models")
if mcfg.get("DEFAULT_MODELS") != allowed[0]:
    plan.append(("POST", "/configs/models", {**mcfg, "DEFAULT_MODELS": allowed[0]}))
low = {a.lower() for a in allowed}
for mid, base in db.execute("select id,base_model_id from model where is_active=1 and base_model_id is not null"):
    if base.lower() not in low:
        plan.append(("POST", f"/models/model/toggle?id={urllib.parse.quote(mid)}", None))

print("current: direct=%s default_models=%r" % (conn.get("ENABLE_DIRECT_CONNECTIONS"), mcfg.get("DEFAULT_MODELS")))
for m, p, b in plan:
    print("PLAN", m, p, "" if b is None else {k: v for k, v in b.items() if k in ("ENABLE_DIRECT_CONNECTIONS", "DEFAULT_MODELS")})
if not plan:
    print("OWUI 侧已是目标状态,无事可做")
if apply:
    for m, p, b in plan:
        res = api(m, p, b)
        print("DONE", p, (res.get("is_active") if isinstance(res, dict) and "is_active" in res else "ok"))
