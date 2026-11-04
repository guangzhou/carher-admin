"""ai-portal/server.py 的离线回归:本地起一个**假飞书** http 服务,全程不碰线上。

跑法:python3 -m pytest scripts/ai-portal -q

拒绝类测试(DENY)都配了对照,防「桩不完整导致的假绿」:
  - 阳性对照(POSITIVE):同一个请求只改掉攻击那一点 ⇒ 必须成功。证明被拒是因为那道校验,
    而不是路由不存在 / 桩没实现 / 参数本来就错。
  - 放开对照(UNGUARDED):用 monkeypatch 把那道校验关掉 ⇒ 攻击必须得逞。证明这条测试有牙。
"""
import http.client, http.server, importlib.util, json, os, secrets, socket, subprocess, sys, threading, time
import urllib.parse
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ai_portal_server", HERE / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

ORIGIN = "https://chat.auto-link.com.cn"
OUR_TENANT = "1088406f1c155740"
APP_ID, APP_SECRET = "cli_aa8505afb0f89cb2", "fake-app-secret-value"   # app id 不是凭据;secret 是假的
INTERNAL = "test-internal-secret-0123456789-ABCDEFGHIJKLMN"
SECRET = "test-session-secret-0123456789-abcdefghijklmnop"
REDIRECT = ORIGIN + "/ai/auth/callback"

USERS = {
    "A": {"union_id": "on_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "open_id": "ou_aaaa", "tenant_key": OUR_TENANT,
          "name": "张三", "email": "zs@auto-link.com.cn", "avatar_url": "https://img/a"},
    "B": {"union_id": "on_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "open_id": "ou_bbbb", "tenant_key": OUR_TENANT,
          "name": "李四", "email": "ls@auto-link.com.cn", "avatar_url": "https://img/b"},
    # 外部人:租户不同、没有 email 字段
    "X": {"union_id": "on_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "open_id": "ou_xxxx", "tenant_key": "2a0b_other_tenant",
          "name": "外部王五", "avatar_url": "https://img/x"},
    # 新人:表里没有任何行
    "N": {"union_id": "on_nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn", "open_id": "ou_nnnn", "tenant_key": OUR_TENANT,
          "name": "新人", "email": "new@auto-link.com.cn", "avatar_url": ""},
}
# tenant_key → 公司映射(形状同 .work/ai-portal/org/tenant_map.json)。tenant_key 用真实值,方便对照
BAIC_TENANT, PERSONAL_TENANT, WUHU_TENANT = "2d3a8ecde0cf575e", "142a7d85c499574f", "1bf59f4a67ca574f"
TENANT_MAP = {
    "_meta": {"org_options": ["无锡车联", "上海车联", "北汽集团", "妙趣童年", "奇瑞", "巧虎", "美团"]},
    "2a0b_other_tenant": {"name": "上海车联", "org": "上海车联", "source": "trust_party", "verified_at": "2026-11-04"},
    BAIC_TENANT: {"name": "北汽集团", "org": "北汽集团", "source": "manual", "verified_at": "2026-11-04"},
    # 公司认出来了(关联组织),但 key 表没有对应选项 ⇒ org=null
    WUHU_TENANT: {"name": "芜湖车联天下智能科技有限公司", "org": None, "source": "trust_party", "verified_at": "2026-11-04"},
}
KEY_A1 = "sk-AAAAaaaa1111cursorA0001"
KEY_A2 = "sk-AAAAaaaa2222claudeA0002"
KEY_B = "sk-BBBBbbbb3333cursorB0003"
KEY_X = "sk-XXXXxxxx4444cursorX0004"


def person(u):
    return [{"id": USERS[u]["union_id"], "name": USERS[u]["name"]}]


DAY, MIN = 86_400_000, 60_000
T0 = 1_790_000_000_000        # 申请时间基准(毫秒)


def row(who, key, status, at, typ="Cursor", org="", **extra):
    f = {"姓名": person(who), "API Key": key, "账户类型": [typ], "状态": status, "申请时间": at}
    if org:
        f["组织名称"] = org
    f.update(extra)
    return f


def seed():
    d = {
        # A:current = 状态「有效」开头 + Cursor + sk- 里申请时间最新的 recA1;明文还散在体检链接/Cursor Key 里
        "recA1": row("A", [{"type": "text", "text": KEY_A1}], "有效", T0 + 3 * DAY,
                     体检链接={"link": "https://chat.auto-link.com.cn/keyprobe/#k=" + KEY_A1, "text": "体检"},
                     点我体检="https://chat.auto-link.com.cn/keyprobe/#k=" + KEY_A1, **{"Cursor Key": KEY_A1}),
        "recA2": row("A", KEY_A2, "有效（已重新生成）", T0 + 1 * DAY, **{"Claude Code Key": KEY_A2}),
        "recA3": row("A", "sk-AAAAold9999disabled", "已禁用", T0 + 5 * DAY),
        "recA4": row("A", "__provisioning__", "", T0 + 6 * DAY),
        "recA5": row("A", "", "生成失败: 网关创建 key 超时 sk-LEAKinREASON1234", T0 + 7 * DAY),
        "recA6": row("A", "sk-AAAAdone8888finished", "已完成", T0 + 2 * DAY),
        # A 名下的客户 key:组织名称命中 EXCLUDE_ORGS
        "recQ1": row("A", "sk-QIAOHUqiaohu00001111", "有效", T0 + 9 * DAY, org="巧虎"),
        "recB1": row("B", KEY_B, "有效", T0 + DAY),
        "recX1": row("X", KEY_X, "有效", T0 + DAY),
        "recAB": row("A", "sk-SHAREDsharedshared77", "有效", T0 + 4 * DAY),
    }
    d["recAB"]["姓名"] = person("A") + person("B")
    # A 名下连续批量生成的 6 行(组织名不在名单里,间隔 1 秒)⇒ 按「连续 ≥5 行」排除
    for i in range(6):
        d["recBT%d" % i] = row("A", "sk-BATCHbatch%04dxxxx" % i, "有效", T0 + 10 * DAY + i * 1000, org="某新客户")
    return d


# 「状态」是单选列,只能写**表里已有的选项**。这 16 个是 11-04 读真表拿到的全集
# (.work/ai-portal/data/fields.json)。假飞书按它卡,防止代码里写出一个表上不存在的状态。
REAL_STATUS_OPTIONS = {
    "有效", "已禁用", "有效（重复提交，复用已有key）", "生成中", "有效（已重新生成）", "重新生成中",
    "待生成（第三方合作伙伴）", "待生成（第三方合作伙伴补录）", "已完成",
    "生成失败: timeout", "生成失败: Client network socket disconnected before secure T",
    '生成失败(Cursor): LiteLLM error: {"error":{"message":"Auth',
    '生成失败: LiteLLM error: {"error":{"message":"Authentication',
    '生成失败(Cursor): Non-JSON response: <html>\r\n<head><title>',
    '生成失败: Non-JSON response: <html>\r\n<head><title>502 Bad Ga',
    '生成失败: Non-JSON response: <html>\r\n<head><title>500 Intern',
}


# ── 假飞书 ──────────────────────────────────────────────────────────────
class Fake:
    def reset(self):
        self.calls = []            # (method, path)
        self.tables = {}           # (app, table) -> {rid: fields}
        self.codes = {}            # code -> user key
        self.uats = {}             # user_access_token -> user key
        self.tenant_tokens = set()
        self.tenant_issued = 0
        self.leaky = False         # True = search 忽略过滤条件,把整张表回出去(模拟过滤器失效)
        self.userinfo_flat = False
        self.fail_create_person = False
        self.n = 0
        self.search_error = None   # 非 None ⇒ search 直接回这个错误码(模拟权限/限流等真失败)
        self.update_error = None   # 非 None ⇒ 记录更新直接回这个错误码(模拟写表失败)


FAKE = Fake()
FAKE.reset()


class FakeFeishu(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj, loc=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        if loc:
            self.send_header("Location", loc)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def bearer(self):
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        FAKE.calls.append(("GET", u.path))
        if u.path == "/authorize":
            # 模拟「用户在飞书授权页点了同意」:测试用 as= 指定是谁
            if q.get("client_id") != APP_ID or q.get("redirect_uri") != REDIRECT:
                return self.out(400, {"code": 20029, "msg": "redirect_uri 不在白名单"})
            if q.get("as") == "deny":
                return self.out(302, {}, q["redirect_uri"] + "?" + urllib.parse.urlencode(
                    {"error": "access_denied", "state": q["state"]}))
            FAKE.n += 1
            code = "fake-code-%04d-%s" % (FAKE.n, q["as"])
            FAKE.codes[code] = q["as"]
            return self.out(302, {}, q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]}))
        if u.path == "/open-apis/authen/v1/user_info":
            who = FAKE.uats.get(self.bearer())
            if not who:
                return self.out(401, {"code": 99991668, "msg": "invalid access token"})
            d = dict(USERS[who])
            return self.out(200, d if FAKE.userinfo_flat else {"code": 0, "msg": "success", "data": d})
        if u.path.startswith("/open-apis/contact/"):
            # 跨租户用户查通讯录必 41050;门户不该调这里
            return self.out(400, {"code": 41050, "msg": "no user authority error"})
        self.out(404, {"code": 404, "msg": "fake: no route " + u.path})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        b = self.body()
        FAKE.calls.append(("POST", u.path))
        if u.path == "/open-apis/authen/v2/oauth/token":
            who = FAKE.codes.pop(b.get("code"), None)        # 授权码一次性
            if (b.get("client_id"), b.get("client_secret")) != (APP_ID, APP_SECRET):
                return self.out(400, {"code": 20002, "error": "invalid_client"})
            if not who or b.get("redirect_uri") != REDIRECT or b.get("grant_type") != "authorization_code":
                return self.out(400, {"code": 20003, "error": "invalid_grant"})
            FAKE.n += 1
            tok = "u-fake-uat-%04d" % FAKE.n
            FAKE.uats[tok] = who
            return self.out(200, {"code": 0, "access_token": tok, "expires_in": 7200, "token_type": "Bearer"})
        if u.path == "/open-apis/auth/v3/tenant_access_token/internal":
            if (b.get("app_id"), b.get("app_secret")) != (APP_ID, APP_SECRET):
                return self.out(400, {"code": 10014, "msg": "app secret invalid"})
            FAKE.tenant_issued += 1
            tok = "t-fake-%04d" % FAKE.tenant_issued
            FAKE.tenant_tokens.add(tok)
            return self.out(200, {"code": 0, "tenant_access_token": tok, "expire": 7200})
        if u.path.startswith("/open-apis/bitable/v1/apps/"):
            if self.bearer() not in FAKE.tenant_tokens:
                return self.out(400, {"code": 99991663, "msg": "Invalid access token for authorization"})
            parts = u.path.split("/")      # ['', open-apis, bitable, v1, apps, APP, tables, TBL, records, (search)]
            tbl = FAKE.tables.setdefault((parts[5], parts[7]), {})
            if u.path.endswith("/records/search"):
                return self.search(tbl, q, b)
            if u.path.endswith("/records"):
                f = b["fields"]
                if FAKE.fail_create_person and "提交人" in f:
                    return self.out(400, {"code": 1254066, "msg": "UserFieldConvFail"})
                FAKE.n += 1
                rid = "recNEW%04d" % FAKE.n
                tbl[rid] = f
                return self.out(200, {"code": 0, "data": {"record": {"record_id": rid, "fields": f}}})
        self.out(404, {"code": 404, "msg": "fake: no route " + u.path})

    def do_PUT(self):
        """逐字段覆盖(真飞书的 PUT /records/<rid> 语义):只合并发来的字段,其它列不动。"""
        u = urllib.parse.urlsplit(self.path)
        b = self.body()
        FAKE.calls.append(("PUT", u.path))
        if not u.path.startswith("/open-apis/bitable/v1/apps/"):
            return self.out(404, {"code": 404, "msg": "fake: no route " + u.path})
        if self.bearer() not in FAKE.tenant_tokens:
            return self.out(400, {"code": 99991663, "msg": "Invalid access token for authorization"})
        parts = u.path.split("/")   # [...,apps,APP,tables,TBL,records,RID]
        tbl = FAKE.tables.setdefault((parts[5], parts[7]), {})
        rid = parts[9]
        if FAKE.update_error is not None:
            return self.out(400, {"code": FAKE.update_error, "msg": "fake update error"})
        if rid not in tbl:
            return self.out(400, {"code": 1254043, "msg": "RecordIdNotFound"})
        # 单选列只接受表里已有的选项(真飞书写新值会报错);这里按 test 里的真实选项集合卡
        st = b["fields"].get("状态")
        if st is not None and st not in REAL_STATUS_OPTIONS:
            return self.out(400, {"code": 1254045, "msg": "FieldNameNotFound/option not exist: %s" % st})
        tbl[rid].update(b["fields"])
        return self.out(200, {"code": 0, "data": {"record": {"record_id": rid, "fields": tbl[rid]}}})

    def search(self, tbl, q, b):
        id_type = q.get("user_id_type", "open_id")
        conds = (b.get("filter") or {}).get("conditions") or []
        if FAKE.search_error is not None:
            return self.out(400, {"code": FAKE.search_error, "msg": "fake error"})
        # 真飞书:人员格 is 一个对不上 user_id_type 形态的值(假 id/截断 id/类型不匹配)⇒ 1254018 InvalidFilter
        for c in conds:
            if c["field_name"] == "姓名" and id_type == "union_id" and not all(
                    isinstance(v, str) and v.startswith("on_") and len(v) == 35 for v in c["value"]):
                return self.out(400, {"code": 1254018, "msg": "InvalidFilter"})
        items = []
        for rid, f in tbl.items():
            ok = True
            for c in conds:
                if FAKE.leaky:
                    break
                cell = f.get(c["field_name"])
                if not isinstance(cell, list) or not cell or not isinstance(cell[0], dict):
                    ok = False
                    continue
                # 人员格:按 user_id_type 呈现 id;不是 union_id 的话 on_ 值永远对不上
                # 真飞书对「拿不到 union_id 的成员」会回什么形状没实测过 ⇒ 桩要容得下
                # 缺 id / id 为空 / 非 dict 的成员,否则这类输入永远测不到(红队 11-04 指出)
                ids = [i if id_type == "union_id" else i.replace("on_", "ou_")
                       for i in ((x.get("id") or "") if isinstance(x, dict) else "" for x in cell) if i]
                if c["operator"] == "is":
                    ok &= len(ids) == 1 and ids[0] in c["value"]   # 多人格 is 单人 ⇒ 不命中
                elif c["operator"] == "contains":
                    ok &= any(i in c["value"] for i in ids)
                else:
                    ok = False
            if ok:
                items.append({"record_id": rid, "fields": f})
        size = min(int(q.get("page_size", 20)), 2)        # 故意 2 条一页,逼出分页
        start = int(q.get("page_token") or 0)
        page = items[start:start + size]
        more = start + size < len(items)
        return self.out(200, {"code": 0, "data": {"items": page, "has_more": more,
                                                  "page_token": str(start + size) if more else "",
                                                  "total": len(items)}})


@pytest.fixture(scope="module")
def fake():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeFeishu)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % s.server_port
    s.shutdown()


# ── 假 198 LiteLLM ──────────────────────────────────────────────────────
# 🔴 全程不碰真网关。形状照 .work/ext-user-onboarding/15-key-lifecycle.md 的实测:
#   /key/info?key=<sha256>  → {"info": {...}}，**token 列被置空**（实测如此，代码不能信它）
#   /key/generate           → 200 {"key": "sk-…"}；alias 重名 → 400（唯一性在应用层）
#   /key/update             → 改名 / blocked
#   /key/delete             → {"key_aliases":[...]}
#   /v1/chat/completions    → 带 key 的真调用；blocked / 不存在 ⇒ 401
MASTER = "sk-fake-master-key-for-tests-0001"
# 旧 key 在网关上的样子:按 198 cursor-* 族的真实形状(逐个列模型名,不是 all-team-models)
GW_MODELS = ["gpt-5.5", "claude-haiku-4-5", "claude-opus-5-5", "kimi-k3"]


class FakeLite:
    def reset(self):
        self.calls = []        # (method, path)
        self.keys = {}         # sha256(plain) -> row
        self.plain = {}        # sha256(plain) -> plain(只有假网关自己知道,服务端不读)
        self.n = 0
        self.fail = {}         # path 片段 -> (status, body)，模拟某一步真失败
        self.probe_401_for = set()   # sha256:探针一律 401(模拟"建出来了但调不动")
        self.generate_hook = None    # 生成成功后的回调(测并发/竞态)

    def add(self, plain, alias, **over):
        row = {"key_alias": alias, "key_name": S.mask_key(plain), "token": None,  # 🔴 实测 token 被置空
               "models": list(GW_MODELS), "aliases": {}, "team_id": "team-cursor-198",
               "user_id": S.alias_base(alias), "max_budget": 20.0, "budget_duration": "30d",
               "spend": 1.25, "blocked": False, "key_type": "default",
               "metadata": {"lark_open_id": "ou_xxx", "base_alias": S.alias_base(alias),
                            "account_type": "Cursor", "email": "x@auto-link.com.cn"}}
        row.update(over)
        h = S.sha(plain)
        self.keys[h] = row
        self.plain[h] = plain
        return row


LITE = FakeLite()
LITE.reset()


class FakeLiteLLM(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def bearer(self):
        return (self.headers.get("Authorization") or "").removeprefix("Bearer ")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def hit_fail(self, path):
        for frag, (st, msg) in LITE.fail.items():
            if frag in path:
                self.out(st, {"error": {"message": msg}})
                return True
        return False

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        LITE.calls.append(("GET", u.path))
        if self.bearer() != MASTER:
            return self.out(401, {"error": {"message": "Invalid proxy server token passed"}})
        if self.hit_fail(u.path):
            return
        if u.path == "/pro/key/info":
            row = LITE.keys.get(q.get("key", ""))
            if not row:
                return self.out(404, {"error": {"message": "Key not found"}})
            return self.out(200, {"key": q["key"], "info": dict(row)})
        self.out(404, {"error": {"message": "fake-litellm: no route " + u.path}})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        b = self.body()
        LITE.calls.append(("POST", u.path))
        if u.path == "/pro/v1/chat/completions":
            return self.chat(b)
        if self.bearer() != MASTER:
            return self.out(401, {"error": {"message": "Invalid proxy server token passed"}})
        if self.hit_fail(u.path):
            return
        if u.path == "/pro/key/generate":
            alias = b.get("key_alias")
            if any(r["key_alias"] == alias for r in LITE.keys.values()):
                # 实测:重名 400,**不会建出第二把**
                return self.out(400, {"error": {"message": "Key with alias '%s' already exists. Unique key "
                                                           "aliases across all keys are required." % alias}})
            LITE.n += 1
            plain = "sk-fakenew%04d%s" % (LITE.n, secrets.token_hex(6))
            row = {k: v for k, v in b.items()}
            row.setdefault("models", [])
            row.update({"token": None, "key_name": S.mask_key(plain), "spend": 0.0, "blocked": False,
                        "key_type": row.get("key_type") or "default"})
            LITE.keys[S.sha(plain)] = row
            LITE.plain[S.sha(plain)] = plain
            if LITE.generate_hook:
                LITE.generate_hook(plain, row)
            return self.out(200, {"key": plain, "key_alias": alias})
        if u.path == "/pro/key/update":
            row = LITE.keys.get(b.get("key") or "")
            if not row:
                return self.out(400, {"error": {"message": "Key not found"}})
            for k, v in b.items():
                if k != "key":
                    row[k] = v
            return self.out(200, {"key": b["key"]})
        if u.path == "/pro/key/delete":
            gone = []
            for h, r in list(LITE.keys.items()):
                if r.get("key_alias") in (b.get("key_aliases") or []):
                    gone.append(r["key_alias"])
                    LITE.keys.pop(h)
                    LITE.plain.pop(h, None)
            return self.out(200, {"deleted_keys": gone})
        self.out(404, {"error": {"message": "fake-litellm: no route " + u.path}})

    def chat(self, b):
        h = S.sha(self.bearer())
        row = LITE.keys.get(h)
        if not row or row.get("blocked"):
            return self.out(401, {"error": {"message": "Authentication Error, Key is blocked."}})
        if h in LITE.probe_401_for:
            return self.out(401, {"error": {"message": "Authentication Error"}})
        if b.get("model") not in (row.get("models") or []):
            return self.out(400, {"error": {"message": "Invalid model name passed in model=%s" % b.get("model")}})
        return self.out(200, {"model": b["model"], "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                              "usage": {"completion_tokens": 2}})


@pytest.fixture(scope="module")
def lite():
    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/pro" % s.server_port
    s.shutdown()


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def make_cfg(fake_url, tmp_path, **over):
    static = tmp_path / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>AI 自助</title>PORTAL-INDEX", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    # 外部人 X 的租户在映射里(老测试里 X 申请要能成功);映射本身的规则在 §18 专门测
    tmap = tmp_path / "tenant_map.json"
    tmap.write_text(json.dumps(TENANT_MAP, ensure_ascii=False), encoding="utf-8")
    cfg = S.load_config(environ={})
    cfg.update(FEISHU_APP_ID=APP_ID, FEISHU_APP_SECRET=APP_SECRET, SESSION_SECRET=SECRET,
               FEISHU_OPEN_BASE=fake_url + "/open-apis", FEISHU_AUTHORIZE_URL=fake_url + "/authorize",
               DB_PATH=str(tmp_path / "portal.db"), STATIC_DIR=str(static), TRUST_PROXY="1",
               # my_rows 缓存默认关:老测试大量「改假表 → 立刻再读」,它们测的是规则不是缓存。
               # 缓存自己的行为在 §17 专门测(MY_ROWS_TTL 显式打开)。
               MY_ROWS_TTL="0", TENANT_MAP_PATH=str(tmap))
    cfg.update(over)
    return cfg


class Portal:
    """一个跑起来的门户 + 一个不跟随跳转、手工管 cookie 的客户端(cookie 带 Secure,http 下 cookiejar 不会回传)。"""

    def __init__(self, fake_url, cfg, clock):
        self.fake_url, self.clock = fake_url, clock
        self.app = S.App(cfg, now=clock)
        self.srv = S.make_server(self.app)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def req(self, method, path, body=None, cookie=None, origin=ORIGIN, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        if cookie:
            h["Cookie"] = cookie
        if origin and method == "POST":
            h["Origin"] = origin
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
        try:
            j = json.loads(b)
        except ValueError:
            j = b.decode("utf-8", "replace")
        out = (r.status, j, r.getheader("Location"), r.headers.get_all("Set-Cookie") or [])
        c.close()
        return out

    def login(self, who, nxt=None):
        """走完整流程,返回 (callback 的 status, Location, sid cookie 串)。"""
        path = "/ai/auth/login" + ("?" + urllib.parse.urlencode({"next": nxt}) if nxt is not None else "")
        st, _, loc, sc = self.req("GET", path)
        assert st == 302 and loc.startswith(self.fake_url + "/authorize?")
        state_cookie = cookie_of(sc, "ai_oauth")
        st2, _, loc2, _ = fake_get(loc + "&as=" + who)
        assert st2 == 302 and loc2.startswith(REDIRECT + "?")
        cb = loc2[len(ORIGIN):]
        st3, body3, loc3, sc3 = self.req("GET", cb, cookie="ai_oauth=" + state_cookie)
        return st3, loc3, ("ai_sid=" + cookie_of(sc3, "ai_sid")) if cookie_of(sc3, "ai_sid") else None, body3

    def sid(self, who):
        st, _, sid, _ = self.login(who)
        assert st == 302 and sid
        return sid

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def cookie_of(set_cookies, name):
    for c in set_cookies:
        k, _, rest = c.partition("=")
        if k == name:
            return rest.split(";", 1)[0]
    return None


def fake_get(url):
    u = urllib.parse.urlsplit(url)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    c.request("GET", u.path + "?" + u.query)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, b, r.getheader("Location"), None


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def portal(fake, tmp_path, clock):
    FAKE.reset()
    FAKE.tables[("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")] = seed()
    p = Portal(fake, make_cfg(fake, tmp_path), clock)
    yield p
    p.close()


@pytest.fixture
def portal_factory(fake, tmp_path, clock):
    made = []

    def mk(**over):
        FAKE.reset()
        FAKE.tables[("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")] = seed()
        p = Portal(fake, make_cfg(fake, tmp_path, **over), clock)
        made.append(p)
        return p
    yield mk
    for p in made:
        p.close()


KEYTBL = ("DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A")


@pytest.fixture
def rotate_portal(fake, lite, tmp_path, clock):
    """开了「重置 key」的门户 + 假 198 网关。表里 A 的 current(recA1)在假网关上有对应的行。

    默认 ROTATE_DRY_RUN=0(真写),因为每条测试都要看到「真写之后表和网关变成什么样」;
    dry-run 的默认值另有专门一条钉它。
    """
    made = []

    def mk(**over):
        FAKE.reset()
        FAKE.tables[KEYTBL] = seed()
        LITE.reset()
        LITE.add(KEY_A1, "cursor-zhangsan-ab12")        # A 的 current
        LITE.add(KEY_B, "cursor-lisi-cd34")             # B 的(用来证明 A 动不了它)
        cfg = make_cfg(fake, tmp_path, LITELLM_BASE=lite, LITELLM_MASTER_KEY=MASTER, ROTATE_DRY_RUN="0")
        cfg.update(over)
        p = Portal(fake, cfg, clock)
        made.append(p)
        return p
    yield mk
    for p in made:
        p.close()


def do_rotate(p, sid, body=None, ip="10.4.4.4"):
    return p.req("POST", "/ai/api/keys/rotate", {} if body is None else body, cookie=sid,
                 headers={"X-Real-IP": ip})


def row_key(rid="recA1"):
    """表里那一行当前的 API Key 明文(文本列可能是富文本数组)。"""
    v = FAKE.tables[KEYTBL][rid].get("API Key")
    return v if isinstance(v, str) else "".join(x.get("text", "") for x in (v or []))


def gw_alias(plain):
    r = LITE.keys.get(S.sha(plain))
    return r and r["key_alias"]


def audit_rows(p):
    rows, _ = p.app.q("select step, result, old_alias, new_alias, union_id, ip, name, login, detail, at"
                      " from rotate_audit order by id")
    return rows


# ════════════════════════════════════════════════════════════════════════
# 1. 登录全流程
# ════════════════════════════════════════════════════════════════════════
def test_login_full_flow_internal(portal):
    st, loc, sid, _ = portal.login("A", nxt="/ai/#keys")
    assert st == 302 and loc == "/ai/#keys" and sid
    # 授权页参数
    _, _, auth, sc = portal.req("GET", "/ai/auth/login")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth).query))
    assert q["client_id"] == APP_ID and q["redirect_uri"] == REDIRECT and len(q["state"]) >= 40
    c = [x for x in sc if x.startswith("ai_oauth=")][0]
    assert "HttpOnly" in c and "Secure" in c and "SameSite=Lax" in c and "Path=/ai/auth/" in c
    # me
    s, d, _, _ = portal.req("GET", "/ai/api/me", cookie=sid)
    assert s == 200 and d["union_id"] == USERS["A"]["union_id"] and d["external"] is False
    assert d["email"] == "zs@auto-link.com.cn" and d["name"] == "张三" and d["avatar"] == "https://img/a"
    # 身份全来自 user_info,从没碰通讯录
    assert not [c for c in FAKE.calls if "/contact/" in c[1]]


def test_session_cookie_flags(portal):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    _, _, _, sc3 = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    c = [x for x in sc3 if x.startswith("ai_sid=")][0]
    assert all(f in c for f in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/ai/"))
    assert any(x.startswith("ai_oauth=;") and "Max-Age=0" in x for x in sc3), "state cookie 用完要清掉"


def test_userinfo_flat_shape_also_accepted(portal):
    """OWUI 实测 user_info 是平铺的;文档是 data 包装。两种都要认。"""
    FAKE.userinfo_flat = True
    st, _, sid, _ = portal.login("A")
    assert st == 302 and sid
    assert portal.req("GET", "/ai/api/me", cookie=sid)[1]["union_id"] == USERS["A"]["union_id"]


def test_logout_kills_session_server_side(portal):
    sid = portal.sid("A")
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200
    assert portal.req("POST", "/ai/auth/logout", {}, cookie=sid)[0] == 200
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401, "退出后旧 cookie 必须作废(服务端删行)"


def test_session_expires(portal, clock):
    sid = portal.sid("A")
    clock.t += int(S.DEFAULTS["SESSION_TTL"]) + 1
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    clock.t -= 2          # POSITIVE: 没过期时同一个 cookie 能用
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200


def test_user_denied_on_feishu(portal):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=deny")
    st, body, _, sc3 = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    assert st == 400 and not cookie_of(sc3, "ai_sid")


def test_code_cannot_be_replayed(portal):
    """同一个 callback URL 再打一次(state 已用 + code 已用)⇒ 不发会话。"""
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    ck = "ai_oauth=" + cookie_of(sc, "ai_oauth")
    assert portal.req("GET", loc2[len(ORIGIN):], cookie=ck)[0] == 302
    st, _, _, sc2 = portal.req("GET", loc2[len(ORIGIN):], cookie=ck)
    assert st == 400 and not cookie_of(sc2, "ai_sid")


# ════════════════════════════════════════════════════════════════════════
# 2. 外部用户
# ════════════════════════════════════════════════════════════════════════
def test_external_user_without_email_logs_in_and_sees_own_keys(portal):
    assert "email" not in USERS["X"]
    st, _, sid, _ = portal.login("X")
    assert st == 302 and sid
    s, d, _, _ = portal.req("GET", "/ai/api/me", cookie=sid)
    assert s == 200 and d["external"] is True and d["email"] == "" and d["tenant_key"] != OUR_TENANT
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sid)
    assert s == 200 and [x["record_id"] for x in d["keys"]] == ["recX1"]
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recX1"}, cookie=sid)
    assert s == 200 and d["key"] == KEY_X
    assert not [c for c in FAKE.calls if "/contact/" in c[1]], "外部人查通讯录必 41050,门户不许调"


# ════════════════════════════════════════════════════════════════════════
# 3. 未登录 401
# ════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/ai/api/me", None), ("GET", "/ai/api/keys", None),
    ("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}), ("POST", "/ai/api/apply", {"account_type": "Cursor"}),
    ("POST", "/ai/api/install-code", {"tool": "cursor"}),
    ("POST", "/ai/api/feedback", {"title": "x", "类型": "其他建议", "工具": "其他"}),
])
def test_unauthenticated_is_401(portal, method, path, body):
    sid = portal.sid("A")
    for bad in (None, "ai_sid=", "ai_sid=forged.sig", sid + "x", sid.split(".")[0] + ".deadbeef"):
        assert portal.req(method, path, body, cookie=bad)[0] == 401, bad
    # POSITIVE:同一请求带真 cookie 不是 401(证明 401 来自鉴权,不是路由不存在)
    assert portal.req(method, path, body, cookie=sid)[0] != 401


def test_cookie_from_other_secret_rejected(fake, tmp_path, clock):
    """另一个 SESSION_SECRET 签的 cookie(比如别的环境)不能用。"""
    FAKE.reset()
    FAKE.tables[KEYTBL] = seed()
    p1 = Portal(fake, make_cfg(fake, tmp_path), clock)
    sid = p1.sid("A")
    p1.close()
    p2 = Portal(fake, make_cfg(fake, tmp_path, SESSION_SECRET="another-secret-0123456789-abcdefghijklmnop"), clock)
    try:
        assert p2.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    finally:
        p2.close()
    p3 = Portal(fake, make_cfg(fake, tmp_path), clock)   # POSITIVE:同库同密钥 ⇒ 认
    try:
        assert p3.req("GET", "/ai/api/me", cookie=sid)[0] == 200
    finally:
        p3.close()


# ════════════════════════════════════════════════════════════════════════
# 4. key 列表 / 打码 / reveal 越权
# ════════════════════════════════════════════════════════════════════════
A_OWN = ["recA1", "recA2", "recA3", "recA4", "recA5", "recA6"]


def test_keys_are_masked_and_exact(portal):
    sid = portal.sid("A")
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sid)
    ids = sorted(x["record_id"] for x in d["keys"])
    assert s == 200 and ids == A_OWN, "精确:不含 B 的行、A+B 多人格行、客户批量行"
    raw = json.dumps(d, ensure_ascii=False)
    assert "sk-" in raw and KEY_A1 not in raw and KEY_A2 not in raw and "AAAAaaaa" not in raw
    assert "LEAKinREASON" not in raw, "生成失败原因里夹的 key 也要打码"
    m = {x["record_id"]: x for x in d["keys"]}
    assert m["recA1"]["masked"] == "sk-…0001" and m["recA1"]["account_type"] == "Cursor"
    assert m["recA1"]["account_types"] == ["Cursor"]
    # 过滤用的是 union_id
    assert ("POST", "/open-apis/bitable/v1/apps/DlT9bsrwMad12VsogEpcK9Ptncc/tables/tblJT2s6Y6xjYj5A/records/search") \
        in FAKE.calls


def test_keys_follow_pagination(portal):
    """假飞书 2 条一页;A 名下 13 行(含排除的)⇒ 必须翻 7 页才拿得全。"""
    sid = portal.sid("A")
    FAKE.calls.clear()
    assert len(portal.req("GET", "/ai/api/keys", cookie=sid)[1]["keys"]) == 6
    assert len([c for c in FAKE.calls if c[1].endswith("/records/search")]) == 7


def test_a_cannot_see_b_keys(portal):
    sa, sb = portal.sid("A"), portal.sid("B")
    da = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert "recB1" not in json.dumps(da) and "3333" not in json.dumps(da)
    # POSITIVE:B 自己能看见 recB1(证明 recB1 真在表里、不是因为数据不存在才看不到)
    db = portal.req("GET", "/ai/api/keys", cookie=sb)[1]["keys"]
    assert [x["record_id"] for x in db] == ["recB1"]


def test_a_cannot_see_b_keys_even_if_filter_leaks(portal, monkeypatch):
    """过滤器失效(假飞书把全表回出来)时,服务端的归属复核仍把别人的行挡掉。"""
    sa = portal.sid("A")
    FAKE.leaky = True
    d = portal.req("GET", "/ai/api/keys", cookie=sa)[1]
    assert sorted(x["record_id"] for x in d["keys"]) == A_OWN
    # UNGUARDED:把归属复核关掉 ⇒ 同一请求就会看到 B 的行(证明是 owner_ok 在挡,不是桩碰巧没回)
    monkeypatch.setattr(S, "owner_ok", lambda cell, uid: True)
    d2 = portal.req("GET", "/ai/api/keys", cookie=sa)[1]
    assert "recB1" in [x["record_id"] for x in d2["keys"]]


def test_reveal_own_ok_other_denied(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa)
    assert s == 200 and d["key"] == KEY_A1                                  # POSITIVE
    for rid in ("recB1", "recX1", "recAB", "recNOPE"):
        s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": rid}, cookie=sa)
        assert s == 404 and "sk-" not in json.dumps(d), rid
    # 别人的行 和 不存在的行 回答完全一样(不泄露存在性)
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)[1] == \
        portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recNOPE"}, cookie=sa)[1]
    # POSITIVE:B 自己 reveal recB1 能成功(证明 recB1 可被 reveal,A 被拒是因为归属)
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=portal.sid("B"))[1]["key"] == KEY_B


def test_reveal_other_denied_even_if_filter_leaks(portal, monkeypatch):
    sa = portal.sid("A")
    FAKE.leaky = True
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)[0] == 404
    # UNGUARDED:关掉归属复核 ⇒ A 就能拿到 B 的明文 key。这条变红说明上面那条是真在挡。
    monkeypatch.setattr(S, "owner_ok", lambda cell, uid: True)
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa)
    assert s == 200 and d["key"] == KEY_B


def test_reveal_bad_record_id(portal):
    sa = portal.sid("A")
    for bad in (None, 123, "", "../x", "recA1' or 1=1", ["recA1"]):
        assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": bad}, cookie=sa)[0] == 400


def test_mask_key():
    assert S.mask_key("sk-abcdefghijklmnop1234") == "sk-…1234"
    assert S.mask_key("sk-short") == "sk-…"            # 太短不露尾
    assert S.mask_key("") == "" and S.mask_key(None) == ""
    assert S.mask_key("  sk-abcdefghijkWXYZ \n") == "sk-…WXYZ"
    assert S.mask_key("pk-abcdefghijk9876") == "…9876"
    for k in ("sk-abcdefghijklmnop1234", "sk-short"):
        assert "abcdefgh" not in S.mask_key(k)


def test_owner_ok():
    u = "on_1"
    assert S.owner_ok([{"id": u}], u)
    assert not S.owner_ok([{"id": u}, {"id": "on_2"}], u), "多人格不算任何人的"
    assert not S.owner_ok([{"id": "on_2"}], u)
    assert not S.owner_ok([], u) and not S.owner_ok(None, u) and not S.owner_ok("on_1", u)
    assert not S.owner_ok([{"id": ""}], "") and not S.owner_ok([{}], None)


def test_feishu_tenant_token_expired_is_refreshed(portal):
    sa = portal.sid("A")
    portal.req("GET", "/ai/api/keys", cookie=sa)
    issued = FAKE.tenant_issued
    FAKE.tenant_tokens.clear()                           # 飞书侧把 token 作废
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 200 and len(d["keys"]) == 6 and FAKE.tenant_issued == issued + 1


def test_feishu_down_is_502_not_leak(portal, monkeypatch):
    sa = portal.sid("A")
    monkeypatch.setattr(portal.app.client, "base", "http://127.0.0.1:1/open-apis")
    monkeypatch.setattr(portal.app.client, "_tok", "")
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 502 and "sk-" not in json.dumps(d)


# ════════════════════════════════════════════════════════════════════════
# 5. OAuth state / 开放重定向 / Origin
# ════════════════════════════════════════════════════════════════════════
def _get_callback(portal, who="A"):
    _, _, loc, sc = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=" + who)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc2).query))
    return q["code"], q["state"], cookie_of(sc, "ai_oauth")


def _cb(portal, code, state, ck):
    st, _, loc, sc = portal.req("GET", "/ai/auth/callback?" + urllib.parse.urlencode({"code": code, "state": state}),
                                cookie=("ai_oauth=" + ck) if ck else None)
    return st, cookie_of(sc, "ai_sid")


def test_state_mismatch_rejected(portal):
    code, state, ck = _get_callback(portal)
    _, state2, ck2 = _get_callback(portal)
    # 1) 浏览器 cookie 和 URL 里的 state 对不上(登录 CSRF:攻击者把自己的 callback 塞给受害者)
    assert _cb(portal, code, state2, ck) == (400, None)
    # 2) 没有 cookie
    assert _cb(portal, code, state, None) == (400, None)
    # 3) 服务端没签发过的 state(cookie 和 URL 一致但是伪造的)
    assert _cb(portal, code, "forged" * 8, "forged" * 8) == (400, None)
    # POSITIVE:同一个 code + 配对的 state/cookie ⇒ 成功(证明 code 本身有效,上面被拒是 state 造成的)
    st, sid = _cb(portal, code, state, ck)
    assert st == 302 and sid


def test_state_mismatch_unguarded_control(portal, monkeypatch):
    """UNGUARDED:把 state 比较改成恒真 ⇒ 不配对的 state 就能登录。证明上一条测试真在测 cookie 比对。"""
    code, state, ck = _get_callback(portal)
    _, state2, _ = _get_callback(portal)
    monkeypatch.setattr(S.hmac, "compare_digest", lambda a, b: True)
    st, sid = _cb(portal, code, state2, state2)     # cookie=URL=别人那次的 state(服务端有且未用)
    assert st == 302 and sid


def test_state_expires(portal, clock):
    code, state, ck = _get_callback(portal)
    clock.t += 601
    assert _cb(portal, code, state, ck) == (400, None)


@pytest.mark.parametrize("nxt", [
    "https://evil.example/", "//evil.example/ai/", "/\\evil.example", "\\\\evil.example", "http:/evil.example",
    "/ai/../admin", "/ai/%2e%2e/admin", "/other/", "javascript:alert(1)", "/ai/\r\nSet-Cookie:x=1", "ai/x",
    "https://chat.auto-link.com.cn.evil.example/ai/",
])
def test_open_redirect_rejected(portal, nxt):
    st, loc, sid, _ = portal.login("A", nxt=nxt)
    assert st == 302 and sid and loc == "/ai/", nxt


def test_open_redirect_positive_and_unguarded(portal, monkeypatch):
    # POSITIVE:本站相对路径原样保留
    assert portal.login("A", nxt="/ai/keys?tab=1#x")[1] == "/ai/keys?tab=1#x"
    # UNGUARDED:放开 safe_next ⇒ 外站地址就会被原样跳过去
    monkeypatch.setattr(S, "safe_next", lambda n, base="/ai": n)
    assert portal.login("A", nxt="https://evil.example/")[1] == "https://evil.example/"


@pytest.mark.parametrize("path,body", [
    ("/ai/api/keys/reveal", {"record_id": "recA1"}), ("/ai/api/apply", {"account_type": "Cursor"}),
    ("/ai/api/install-code", {"tool": "cursor"}), ("/ai/auth/logout", {}),
    ("/ai/api/feedback", {"title": "t", "类型": "其他建议", "工具": "其他"}),
])
def test_write_endpoints_check_origin(portal, path, body):
    sid = portal.sid("A")
    for o in ("https://evil.example", "null", "https://chat.auto-link.com.cn.evil.example", "http://chat.auto-link.com.cn"):
        assert portal.req("POST", path, body, cookie=sid, origin=o)[0] == 403, o
    # 没 Origin 也没 Referer ⇒ 拒;只有本站 Referer ⇒ 放
    assert portal.req("POST", path, body, cookie=sid, origin=None)[0] == 403
    assert portal.req("POST", path, body, cookie=sid, origin=None,
                      headers={"Referer": "https://evil.example/ai/"})[0] == 403
    # POSITIVE:同一请求换成本站 Origin 就不是 403
    assert portal.req("POST", path, body, cookie=sid)[0] != 403


def test_post_requires_json_content_type(portal):
    """text/plain 的跨站「简单请求」不触发预检 ⇒ 只收 application/json 再加一层。"""
    sid = portal.sid("A")
    s = portal.req("POST", "/ai/api/keys/reveal", cookie=sid, raw=b'{"record_id":"recA1"}',
                   headers={"Content-Type": "text/plain"})[0]
    assert s == 415


# ════════════════════════════════════════════════════════════════════════
# 6. 申请:幂等 / dry-run
# ════════════════════════════════════════════════════════════════════════
def test_apply_dry_run_does_not_write(portal):
    sn = portal.sid("N")
    before = json.dumps(FAKE.tables[KEYTBL], sort_keys=True)
    FAKE.calls.clear()
    s, d, _, _ = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)
    assert s == 200 and d["dry_run"] is True
    assert d["fields"] == {"姓名": [{"id": USERS["N"]["union_id"]}], "账户类型": ["Cursor"]}
    assert json.dumps(FAKE.tables[KEYTBL], sort_keys=True) == before
    assert not [c for c in FAKE.calls if c[1].endswith("/records")], "dry-run 不许调 create"
    # 写之前先精确查过一遍
    assert [c for c in FAKE.calls if c[1].endswith("/records/search")]


def test_apply_dry_run_is_default():
    assert S.DEFAULTS["APPLY_DRY_RUN"] == "1"


def test_apply_writes_exact_shape_when_enabled(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0")
    sn = p.sid("N")
    FAKE.calls.clear()
    s, d, _, _ = p.req("POST", "/ai/api/apply", {}, cookie=sn)            # account_type 省略 ⇒ Cursor
    assert s == 200 and d["dry_run"] is False and d["record_id"].startswith("recNEW")
    assert FAKE.tables[KEYTBL][d["record_id"]] == {"姓名": [{"id": USERS["N"]["union_id"]}], "账户类型": ["Cursor"]}
    creates = [c for c in FAKE.calls if c[1].endswith("/records")]
    assert len(creates) == 1
    forbidden = {"API Key", "open_id", "邮箱前缀", "状态", "key_alias"}
    assert not forbidden & set(FAKE.tables[KEYTBL][d["record_id"]])


def test_apply_org_name_optional(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0", APPLY_ORG_NAME="AI Infra部")
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("N"))[1]
    assert FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "AI Infra部"


def test_apply_uses_union_id_type(portal_factory, monkeypatch):
    p = portal_factory(APPLY_DRY_RUN="0")
    seen = []
    real = p.app.client._http
    monkeypatch.setattr(p.app.client, "_http", lambda m, u, b=None, token=None: (seen.append(u), real(m, u, b, token))[1])
    p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("N"))
    assert [u for u in seen if u.endswith("/records?user_id_type=union_id")]


def test_apply_returns_existing_current_without_writing(portal_factory, monkeypatch):
    p = portal_factory(APPLY_DRY_RUN="0")
    sa = p.sid("A")
    n = len(FAKE.tables[KEYTBL])
    s, d, _, _ = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)
    assert s == 200 and d["existing"] is True and d["record_id"] == "recA1" and len(FAKE.tables[KEYTBL]) == n
    # UNGUARDED:让「先精确查一遍」返回空 ⇒ 就会重复写一行
    monkeypatch.setattr(p.app, "my_rows", lambda u, **kw: [])
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)[1]["dry_run"] is False
    assert len(FAKE.tables[KEYTBL]) == n + 1


def test_apply_when_only_disabled_rows_writes(portal_factory):
    """只有已禁用的行(没有 current)⇒ 允许申请新的。"""
    p = portal_factory(APPLY_DRY_RUN="0")
    FAKE.tables[KEYTBL]["recB1"]["状态"] = "已禁用"
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("B"))[1]
    assert d.get("record_id", "").startswith("recNEW")


def test_apply_pending_row_not_duplicated(portal_factory):
    p = portal_factory(APPLY_DRY_RUN="0")
    FAKE.tables[KEYTBL]["recB1"].update({"状态": "", "API Key": "__provisioning__"})
    n = len(FAKE.tables[KEYTBL])
    d = p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=p.sid("B"))[1]
    assert d["pending"] is True and len(FAKE.tables[KEYTBL]) == n


def test_apply_double_click_while_table_lags(portal_factory, monkeypatch):
    """表格搜索有延迟:第一次写的行还没被搜到时连点第二下 ⇒ 本地锁兜住。"""
    p = portal_factory(APPLY_DRY_RUN="0")
    sn = p.sid("N")
    real = p.app.my_rows
    monkeypatch.setattr(p.app, "my_rows", lambda u, **kw: [r for r in real(u, **kw) if not r["record_id"].startswith("recNEW")])
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[1]["dry_run"] is False
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[1]["pending"] is True
    assert len([r for r in FAKE.tables[KEYTBL] if r.startswith("recNEW")]) == 1


def test_apply_bad_type(portal):
    sn = portal.sid("N")
    for bad in ("", "Admin", None, ["Cursor"], "Claude Code"):
        assert portal.req("POST", "/ai/api/apply", {"account_type": bad}, cookie=sn)[0] == 400, bad


# ════════════════════════════════════════════════════════════════════════
# 7. 一次性安装码
# ════════════════════════════════════════════════════════════════════════
def _redeem(p, code, ip="10.0.0.1", origin=None):
    s, d, _, _ = p.req("POST", "/ai/api/install-code/redeem", {"code": code}, origin=origin,
                       headers={"X-Real-IP": ip})
    return s, d


def test_install_code_happy_path_no_login_needed(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)
    assert s == 200 and d["expires_in"] == 600
    import re
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,63}", d["code"]), "前端 CODE_RE:会拼进 shell 命令"
    raw = d["code"].replace("-", "")
    assert len(raw) * 5 >= 128 and set(raw) <= set(S.CODE_ALPHABET), "base32×32 = 160 bit ≥ 128 bit"
    s, r = _redeem(portal, d["code"])                  # 无 cookie、无 Origin(脚本)
    assert s == 200 and r["key"] == KEY_A1 and r["account_type"] == "Cursor"
    # 三个工具共用同一把 current 的 Cursor key(A 名下另有一把 Claude Code 也不用)
    for tool in ("claude", "codex"):
        c = portal.req("POST", "/ai/api/install-code", {"tool": tool}, cookie=sa)[1]["code"]
        r = _redeem(portal, c)[1]
        assert (r["key"], r["account_type"], r["tool"]) == (KEY_A1, "Cursor", tool), tool
    # 脚本那边把码小写 / 去掉横线也能换(人肉抄码时常见)
    c3 = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, c3.lower().replace("-", ""))[1]["key"] == KEY_A1


def test_install_code_stored_hashed_only(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    db = Path(portal.app.cfg["DB_PATH"]).read_bytes()
    raw = code.replace("-", "")
    assert code.encode() not in db and raw.encode() not in db and raw.lower().encode() not in db
    row = portal.app.q("select code_hash from install_codes", one=True)
    assert row and len(row[0]) == 64 and row[0] not in (S.sha(code), S.sha(raw)), "HMAC(带服务端密钥),不是裸 sha256"


def test_install_code_single_use(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code)[0] == 200                        # POSITIVE:第一次成功
    s, d = _redeem(portal, code)
    assert s == 400 and "key" not in d


def test_install_code_single_use_unguarded(portal, monkeypatch):
    """UNGUARDED:把「标记已用」那条 update 换成无条件成功 ⇒ 同一个码能换两次。"""
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    real_q = portal.app.q

    def q(sql, args=(), one=False):
        if sql.startswith("update install_codes"):
            return [], 1
        if sql.startswith("select union_id, account_type from install_codes"):
            sql = sql.replace(" and used is null", "")
        return real_q(sql, args, one)
    monkeypatch.setattr(portal.app, "q", q)
    assert _redeem(portal, code)[0] == 200 and _redeem(portal, code)[0] == 200


def test_install_code_expires(portal, clock):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    clock.t += 601
    assert _redeem(portal, code)[0] == 400
    # POSITIVE:599 秒时还能用
    clock.t -= 601
    code2 = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    clock.t += 599
    assert _redeem(portal, code2)[0] == 200


def test_install_code_wrong_code(portal):
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    answers = set()
    for bad in (flipped, "A" * 32, "", "nope", None, 123, good + "A", good[:-1], "0" + good[1:]):
        s, d = _redeem(portal, bad, ip="10.9.%d.1" % len(answers))
        assert s == 400 and "key" not in d
        answers.add(json.dumps(d, ensure_ascii=False))
    assert len(answers) == 1, "错码/格式错/过期 同一个回答"
    assert _redeem(portal, good)[0] == 200                        # POSITIVE:真码没被上面的错码影响


def test_install_code_binds_to_issuer(portal):
    """B 签的码只换得出 B 的 key,和谁来换无关(换取不看 cookie)。"""
    sa, sb = portal.sid("A"), portal.sid("B")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sb)[1]["code"]
    s, d, _, _ = portal.req("POST", "/ai/api/install-code/redeem", {"code": code}, cookie=sa,
                            headers={"X-Real-IP": "10.0.0.2"})
    assert s == 200 and d["key"] == KEY_B


def test_install_code_skips_disabled_and_needs_key(portal):
    sn = portal.sid("N")
    assert portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sn)[0] == 404
    assert portal.req("POST", "/ai/api/install-code", {"tool": "rm -rf"}, cookie=sn)[0] == 400
    # A 的已禁用 recA3 申请时间比 recA1 新,客户 key 更新 ⇒ 换出来的仍一定是 current 那把
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code)[1]["key"] == KEY_A1


def test_redeem_rate_limited_per_ip(portal, clock):
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    codes = [_redeem(portal, "Z" * 32, ip="10.1.1.1")[0] for _ in range(10)]
    assert codes == [400] * 10
    # 第 11 次:即使是真码也 429(限流先于校验 ⇒ 撞库拿不到「对/错」信号)
    assert _redeem(portal, good, ip="10.1.1.1")[0] == 429
    # POSITIVE:换个 IP 同一个真码能换(证明 429 是限流,不是码坏了)
    assert _redeem(portal, good, ip="10.1.1.2")[0] == 200
    # 窗口过去后恢复
    clock.t += 61
    assert _redeem(portal, "Z" * 32, ip="10.1.1.1")[0] == 400


def test_redeem_rate_limit_unguarded(portal, monkeypatch):
    """UNGUARDED:限流关掉 ⇒ 同 IP 打 30 次都不会 429。证明上一条的 429 来自限流器。"""
    monkeypatch.setattr(portal.app, "redeem_blocked", lambda ip: False)
    assert 429 not in [_redeem(portal, "Z" * 32, ip="10.1.1.9")[0] for _ in range(30)]


def test_redeem_global_rate_limit(portal_factory):
    """全站失败数过线(多 IP 撞库)⇒ 失败过的 IP 只许失败 1 次;换 IP 也只能每个 IP 试 1 次。"""
    p = portal_factory(REDEEM_GLOBAL_PER_MIN="5")
    codes = [_redeem(p, "Q" * 32, ip="10.2.0.%d" % i)[0] for i in range(7)]
    assert codes == [400] * 7, "过线前后,新 IP 的第一次都照常判(没失败过的 IP 不受全局闸影响)"
    # 过线后:已经失败过的 IP 再试 ⇒ 429(没过线时这几个 IP 本来还有 9 次额度)
    assert [_redeem(p, "Q" * 32, ip="10.2.0.%d" % i)[0] for i in range(3)] == [429] * 3


# ── P1-2:全局闸只数失败,打满了也锁不住正常用户 ──────────────────────────
def test_redeem_global_flood_does_not_lock_out_real_users(portal_factory):
    """红队 8.2 原样复现:12 个 IP × 10 个错码 = 120 次失败把全局闸打满 ⇒ 真用户照样装得上。"""
    p = portal_factory(REDEEM_GLOBAL_PER_MIN="120", REDEEM_PER_IP_PER_MIN="10")
    sa = p.sid("A")
    good = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    junk = [_redeem(p, "Z" * 32, ip="11.0.%d.%d" % (i // 10, i % 10))[0] for i in range(120)]
    assert junk == [400] * 120
    assert not p.app.rl_redeem_all.allowed("*"), "前提:全局失败线确实打满了"
    assert _redeem(p, good, ip="12.0.0.1")[0] == 200, "没失败过的 IP 拿真码必须能换"
    # 攻击者自己的 IP 在过线后被收紧到 0 次(真码也不判)
    good2 = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(p, good2, ip="11.0.0.1")[0] == 429


def test_redeem_global_flood_unguarded_old_behavior_locks_everyone(portal_factory, monkeypatch):
    """反向对照:把闸改回「每次尝试都数」(旧实现)⇒ 同样的 120 次错码之后真用户 429。证明上一条测得出 P1-2。"""
    p = portal_factory(REDEEM_GLOBAL_PER_MIN="120", REDEEM_PER_IP_PER_MIN="10")
    sa = p.sid("A")
    good = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    app = p.app
    monkeypatch.setattr(app, "redeem_blocked",
                        lambda ip: not (app.rl_redeem_ip.hit(ip) and app.rl_redeem_all.hit("*")))
    monkeypatch.setattr(app, "redeem_fail", lambda ip: None)
    [_redeem(p, "Z" * 32, ip="11.0.%d.%d" % (i // 10, i % 10)) for i in range(120)]
    assert _redeem(p, good, ip="12.0.0.1")[0] == 429


def test_redeem_success_does_not_count_against_ip(portal):
    """只数失败:同一个 IP(198 上 nginx 没开 realip 时**所有人**都是前置代理那一个 IP)连续换 15 个真码都成功。"""
    sa, sb = portal.sid("A"), portal.sid("B")
    out = []
    for i in range(15):
        sid = sa if i % 2 else sb      # 每人每分钟只能签 5 个码 ⇒ 两个人轮着签,再推时钟
        c = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sid)[1]["code"]
        out.append(_redeem(portal, c, ip="10.68.13.97")[0])
        if i % 8 == 7:
            portal.clock.t += 61
    assert out == [200] * 15
    # POSITIVE:同一个 IP 失败 10 次就 429(闸还在,只是不数成功)
    assert [_redeem(portal, "Z" * 32, ip="10.68.13.97")[0] for _ in range(11)] == [400] * 10 + [429]


def test_redeem_failure_after_limit_still_hides_signal(portal):
    """被限流的 IP 拿真码也只得到 429,不知道码对不对;且 429 本身不再累加(不会越限越久)。"""
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    [_redeem(portal, "Z" * 32, ip="10.7.7.7") for _ in range(10)]
    assert _redeem(portal, good, ip="10.7.7.7")[0] == 429
    assert portal.app.rl_redeem_ip.count("10.7.7.7") == 10
    portal.clock.t += 61
    assert _redeem(portal, good, ip="10.7.7.7")[0] == 200, "窗口过去后同一个真码还能换(429 没把码作废)"


def test_redeem_ignores_spoofed_xff_when_not_trusted(portal_factory):
    p = portal_factory(TRUST_PROXY="0")
    codes = [_redeem(p, "W" * 32, ip="10.3.0.%d" % i)[0] for i in range(11)]
    assert codes[-1] == 429, "不信代理头时,伪造 X-Real-IP 换不出新额度"


def test_redeem_cross_origin_browser_rejected(portal):
    """redeem 免登录给脚本用;但浏览器带着外站 Origin 来调 ⇒ 拒(本站 Origin 或无 Origin 才放)。"""
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, code, origin="https://evil.example")[0] == 403
    assert _redeem(portal, code, origin=ORIGIN)[0] == 200


def test_issue_code_rate_limited(portal):
    sa = portal.sid("A")
    codes = [portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[0] for _ in range(6)]
    assert codes == [200] * 5 + [429]


# ════════════════════════════════════════════════════════════════════════
# 8. 反馈(复用 feedback_server 的 build_fields / KEY_RE)
# ════════════════════════════════════════════════════════════════════════
FB = {"title": "选 opus 报 403", "类型": "用不了/报错", "工具": "Cursor", "电脑": "Mac",
      "detail": "报错:key sk-abcdefghijklmnop1234 not allowed", "source": "ai#keys"}
FBTBL = ("SQ4vbBJhcauRKpsuZrNczNCInxb", "tblkckVodz4HHKZf")


def test_feedback_reuses_feedback_server():
    assert S.KEY_RE is S.fb.KEY_RE and S.fb.build_fields.__module__ == "feedback_server"


def test_feedback_writes_masked_and_server_identity(portal):
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", dict(FB, 提交人邮箱="evil@x.com"), cookie=sa)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    assert f["提交人"] == [{"id": USERS["A"]["union_id"]}] and f["提交人邮箱"] == "zs@auto-link.com.cn"
    assert "sk-abcdefghijklmnop1234" not in json.dumps(f, ensure_ascii=False) and "sk-***" in f["详细描述"]
    assert "张三(内部)" in f["详细描述"] and f["类型"] == "用不了/报错"


def test_feedback_external_falls_back_without_person(portal):
    sx = portal.sid("X")
    FAKE.fail_create_person = True
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", FB, cookie=sx)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    # 11-04 外部旅程:外部人的标签里加了 union_id / tenant_key(人员格写不进时只剩这行能认人)
    assert "提交人" not in f and f["提交人邮箱"] == "" and "提交人:外部王五 union_id=" + USERS["X"]["union_id"] in f["详细描述"]
    assert f["详细描述"].endswith("(外部)")


def test_feedback_validation_and_rate_limit(portal):
    sa = portal.sid("A")
    assert portal.req("POST", "/ai/api/feedback", dict(FB, 类型="乱写"), cookie=sa)[0] == 400
    assert portal.req("POST", "/ai/api/feedback", dict(FB, title=" "), cookie=sa)[0] == 400
    codes = [portal.req("POST", "/ai/api/feedback", FB, cookie=sa)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]


# ════════════════════════════════════════════════════════════════════════
# 9. 静态文件
# ════════════════════════════════════════════════════════════════════════
def test_static_index_and_assets(portal):
    s, b, _, _ = portal.req("GET", "/ai/")
    assert s == 200 and "PORTAL-INDEX" in b
    assert portal.req("GET", "/ai")[2] == "/ai/"
    assert portal.req("GET", "/ai/app.js") == (200, "console.log(1)", None, [])


@pytest.mark.parametrize("path", ["/ai/../server.py", "/ai/%2e%2e/server.py", "/ai/..%2fserver.py",
                                  "/ai/static/../../x", "/ai/.env", "/ai/nope.js", "/elsewhere"])
def test_static_traversal_and_missing(portal, path):
    assert portal.req("GET", path)[0] == 404


def test_static_dir_is_repo_static_by_default():
    assert Path(S.DEFAULTS["STATIC_DIR"]) == HERE / "static"


# ════════════════════════════════════════════════════════════════════════
# 10. 配置:SESSION_SECRET 缺失拒绝启动
# ════════════════════════════════════════════════════════════════════════
def test_session_secret_has_no_default():
    assert "SESSION_SECRET" not in S.DEFAULTS
    assert S.load_config(environ={})["SESSION_SECRET"] == ""


@pytest.mark.parametrize("secret", ["", "   ", "short-secret"])
def test_app_refuses_without_good_secret(fake, tmp_path, secret):
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, SESSION_SECRET=secret))
    # POSITIVE:同一份配置只把密钥换成合格的 ⇒ 能起
    S.App(make_cfg(fake, tmp_path))


def test_process_refuses_to_start_without_secret(tmp_path):
    env_file = tmp_path / "ai-portal.env"
    env_file.write_text("FEISHU_APP_ID=" + APP_ID + "\nFEISHU_APP_SECRET=y\n# SESSION_SECRET 故意没配\nLISTEN=127.0.0.1:0\n")
    env = {k: v for k, v in os.environ.items() if k not in ("SESSION_SECRET", "FEISHU_APP_ID", "FEISHU_APP_SECRET")}
    r = subprocess.run([sys.executable, str(HERE / "server.py"), "--env", str(env_file)], env=env,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "SESSION_SECRET" in r.stderr
    # POSITIVE:补上密钥 ⇒ 进程起来并开始监听(超时被杀 = 在跑)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env_file.write_text(env_file.read_text().replace("LISTEN=127.0.0.1:0", "LISTEN=127.0.0.1:%d" % port)
                        + "SESSION_SECRET=%s\nDB_PATH=%s\n" % (SECRET, tmp_path / "x.db"))
    proc = subprocess.Popen([sys.executable, str(HERE / "server.py"), "--env", str(env_file)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                c.request("GET", "/ai/healthz")
                assert c.getresponse().status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("配了密钥却没起来")
    finally:
        proc.terminate()
        proc.wait(5)


def test_env_file_parsing_and_process_env_wins(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("# c\nSESSION_SECRET='from-file'\nFEISHU_APP_ID=\"cli_file\"\n\nBAD LINE\n")
    cfg = S.load_config(environ={"FEISHU_APP_ID": "cli_env"}, env_file=str(f))
    assert cfg["SESSION_SECRET"] == "from-file" and cfg["FEISHU_APP_ID"] == "cli_env"


# ════════════════════════════════════════════════════════════════════════
# 11. 日志不带秘密
# ════════════════════════════════════════════════════════════════════════
def test_logs_never_contain_secrets(portal_factory, capsys):
    p = portal_factory(APPLY_DRY_RUN="0")
    capsys.readouterr()
    _, _, loc, sc = p.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc2).query))
    _, _, _, sc3 = p.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    sid = "ai_sid=" + cookie_of(sc3, "ai_sid")
    p.req("GET", "/ai/api/keys", cookie=sid)
    p.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sid)
    p.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sid)
    code = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sid)[1]["code"]
    _redeem(p, code)
    _redeem(p, code)
    p.req("POST", "/ai/api/feedback", FB, cookie=sid)
    p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sid)
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "reveal" in logs and "redeem" in logs, "阳性对照:确实在打日志,不是什么都没打"
    secrets_ = [KEY_A1, KEY_B, code, code[4:], q["code"], q["state"], cookie_of(sc3, "ai_sid").split(".")[0],
                APP_SECRET, SECRET, "sk-abcdefghijklmnop1234"] + list(FAKE.uats) + list(FAKE.tenant_tokens)
    for s in secrets_:
        assert s not in logs, s[:6]


# ════════════════════════════════════════════════════════════════════════
# 12. 和前端(static/index.html + static/dev_mock.py 约定)的接口形状
# ════════════════════════════════════════════════════════════════════════
def test_frontend_login_uses_redirect_param(portal):
    st, _, loc, sc = portal.req("GET", "/ai/auth/login?redirect=" + urllib.parse.quote("/ai/?x=1#keys"))
    _, _, loc2, _ = fake_get(loc + "&as=A")
    st3, _, loc3, _ = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))
    assert st3 == 302 and loc3 == "/ai/?x=1#keys"
    # redirect 也走同一道开放重定向校验
    st, _, loc, sc = portal.req("GET", "/ai/auth/login?redirect=https://evil.example/")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    assert portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc, "ai_oauth"))[2] == "/ai/"


def test_frontend_get_logout(portal):
    sid = portal.sid("A")
    st, _, loc, sc = portal.req("GET", "/ai/auth/logout?redirect=/ai/", cookie=sid)
    assert st == 302 and loc == "/ai/" and any(c.startswith("ai_sid=;") for c in sc)
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401
    assert portal.req("GET", "/ai/auth/logout?redirect=//evil.example", cookie=sid)[2] == "/ai/"


def test_errors_carry_message_for_frontend(portal):
    """前端 api() 只读 j.message;没有就显示泛化文案。"""
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/apply", {"account_type": "乱写"}, cookie=sa)
    assert s == 400 and d["message"] and d["message"] == d["error"]
    s, d, _, _ = portal.req("GET", "/ai/api/me")
    assert s == 401 and d["message"]


def test_frontend_feedback_shape(portal):
    sa = portal.sid("A")
    body = {"type": "用不了/报错", "tool": "Claude Code", "title": "装不上", "detail": "点了没反应",
            "report": "env: OPENAI_API_KEY=sk-leakleakleakleak9999\nos=mac", "os": "Mac", "page": "/ai/"}
    s, d, _, _ = portal.req("POST", "/ai/api/feedback", body, cookie=sa)
    assert s == 200
    f = FAKE.tables[FBTBL][d["id"]]
    assert f["类型"] == "用不了/报错" and f["工具"] == "Claude Code" and f["电脑"] == "Mac" and f["来源页面"] == "/ai/"
    assert "自检报告" in f["详细描述"] and "os=mac" in f["详细描述"]
    assert "sk-leakleak" not in json.dumps(f, ensure_ascii=False), "自检报告里的 key 也要打码"
    # 200KB 自检报告(前端上限)能收
    big = dict(body, report="x" * 200 * 1024)
    assert portal.req("POST", "/ai/api/feedback", big, cookie=sa)[0] == 200


def test_dev_mock_not_served(portal):
    """static/ 里同事放了 dev_mock.py;只出白名单后缀。"""
    (Path(portal.app.cfg["STATIC_DIR"]) / "dev_mock.py").write_text("SECRET = 1")
    assert portal.req("GET", "/ai/dev_mock.py")[0] == 404
    assert portal.req("GET", "/ai/app.js")[0] == 200                      # POSITIVE


def test_real_static_index_is_served(fake, tmp_path, clock):
    """仓库里同事真写的 static/index.html 能从 /ai/ 出来(只读,不改它)。"""
    real = HERE / "static" / "index.html"
    if not real.is_file():
        pytest.skip("static/index.html 还没有")
    FAKE.reset()
    cfg = make_cfg(fake, tmp_path)
    cfg["STATIC_DIR"] = str(HERE / "static")
    p = Portal(fake, cfg, clock)
    try:
        s, b, _, _ = p.req("GET", "/ai/")
        assert s == 200 and b == real.read_text(encoding="utf-8")
        assert p.req("GET", "/ai/dev_mock.py")[0] == 404
    finally:
        p.close()


# ════════════════════════════════════════════════════════════════════════
# 13. key 表真实数据规则(数据组实测)
# ════════════════════════════════════════════════════════════════════════
def test_status_mapping_by_prefix():
    f = S.front_status
    assert f("有效", "sk-x") == "可用" and f("有效（已重新生成）", "sk-x") == "可用" and f("已完成", "sk-x") == "可用"
    assert f("已禁用", "sk-x") == "已禁用" and f("已禁用（离职）", "sk-x") == "已禁用"
    assert f("生成失败: 网关超时", "") == "生成失败: 网关超时"
    assert f("生成中", "") == "生成中" and f("", "__provisioning__") == "生成中" and f("有效", "__provisioning__") == "生成中"
    assert f("", "sk-x") == "" and f("奇怪的值", "sk-x") == "奇怪的值"


def test_keys_status_and_single_current(portal):
    sa = portal.sid("A")
    m = {k["record_id"]: k for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]}
    assert {r: m[r]["status"] for r in m} == {
        "recA1": "可用", "recA2": "可用", "recA3": "已禁用", "recA4": "生成中",
        "recA5": "生成失败: 网关创建 key 超时 sk-…1234", "recA6": "可用"}
    assert [r for r in m if m[r]["current"]] == ["recA1"], "只有一行 current"
    # recA3 已禁用但申请时间更新、recA6 已完成、recA2 是 Claude Code ⇒ 都不是 current
    assert m["recA4"]["masked"] == "" and "__provisioning__" not in json.dumps(m)


def test_current_picks_latest_valid_cursor(portal):
    sa = portal.sid("A")
    FAKE.tables[KEYTBL]["recA7"] = row("A", "sk-AAAAnewest777newest", "有效", T0 + 8 * DAY)
    cur = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]]
    assert cur == ["recA7"]
    # 「有效」但 key 不是 sk-(比如还在占位)⇒ 不当 current
    FAKE.tables[KEYTBL]["recA7"]["API Key"] = "__provisioning__"
    cur = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]]
    assert cur == ["recA1"]


def test_customer_batch_keys_excluded(portal, monkeypatch):
    sa = portal.sid("A")
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert "recQ1" not in ids and not [i for i in ids if i.startswith("recBT")]
    # 巧虎那行申请时间最新、状态有效、Cursor ⇒ 如果没排除它会被当成 current
    assert [k for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]][0]["record_id"] == "recA1"
    # 客户 key 不能 reveal、不能拿去换安装码
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recQ1"}, cookie=sa)[0] == 404
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recBT0"}, cookie=sa)[0] == 404
    # UNGUARDED:关掉排除 ⇒ 客户 key 就会出现、并抢走 current
    monkeypatch.setattr(portal.app, "drop_batches", lambda rows, user: rows)
    d = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert "recQ1" in [k["record_id"] for k in d] and [k for k in d if k["current"]][0]["record_id"] == "recBT5"


def test_batch_threshold_boundary(portal):
    """连续 4 行不算批量(阈值 5),5 行算。"""
    sa = portal.sid("A")
    for i in range(5, 6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    del FAKE.tables[KEYTBL]["recBT4"]
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert len([i for i in ids if i.startswith("recBT")]) == 4                         # POSITIVE:4 行照常返回
    FAKE.tables[KEYTBL]["recBT4"] = row("A", "sk-BATCHbatch0004xxxx", "有效", T0 + 10 * DAY + 4000)
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert not [i for i in ids if i.startswith("recBT")]


def test_plaintext_in_other_columns_never_returned(portal):
    sa = portal.sid("A")
    raw = json.dumps(portal.req("GET", "/ai/api/keys", cookie=sa)[1], ensure_ascii=False)
    assert KEY_A1 not in raw and KEY_A2 not in raw and "#k=sk-AAAA" not in raw
    # 后端拿到的行里确实有这些列(阳性:假飞书真的把体检链接/Cursor Key 回给了后端)
    assert KEY_A1 in json.dumps(FAKE.tables[KEYTBL]["recA1"]["体检链接"])


def test_mask_deep():
    v = {"a": "x https://h/keyprobe/#k=sk-abcdefghijklmnop1234 y", "b": [{"text": "sk-zzzzzzzzzzzz9999"}], "c": 3}
    out = S.mask_deep(v)
    assert out == {"a": "x https://h/keyprobe/#k=sk-…1234 y", "b": [{"text": "sk-…9999"}], "c": 3}


def test_reveal_disabled_denied(portal):
    sa = portal.sid("A")
    for rid in ("recA3", "recA4", "recA5"):           # 已禁用 / 生成中 / 生成失败(无 sk-)
        s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": rid}, cookie=sa)
        assert s == 403 and "sk-" not in json.dumps(d), rid
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA6"}, cookie=sa)[0] == 200   # POSITIVE 已完成可 reveal


def test_reveal_disabled_unguarded(portal, monkeypatch):
    """UNGUARDED:状态映射把「已禁用」当成可用 ⇒ 已禁用的 key 就能被 reveal。证明上一条挡的是状态。"""
    sa = portal.sid("A")
    monkeypatch.setattr(S, "front_status", lambda raw, key: "可用")
    s, d, _, _ = portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA3"}, cookie=sa)
    assert s == 200 and d["key"] == "sk-AAAAold9999disabled"


def test_reveal_audit(portal):
    sa = portal.sid("A")
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa, headers={"X-Real-IP": "10.7.7.7"})
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recB1"}, cookie=sa, headers={"X-Real-IP": "10.7.7.8"})
    portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recA3"}, cookie=sa, headers={"X-Real-IP": "10.7.7.9"})
    rows, _ = portal.app.q("select union_id, name, record_id, ip, login, result, at from reveal_audit order by id")
    assert [(r[2], r[3], r[4], r[5]) for r in rows] == [
        ("recA1", "10.7.7.7", "oauth", "ok"), ("recB1", "10.7.7.8", "oauth", "denied:not-owner"),
        ("recA3", "10.7.7.9", "oauth", "denied:disabled")]
    assert all(r[0] == USERS["A"]["union_id"] and r[1] == "张三" and r[6] for r in rows)
    db = Path(portal.app.cfg["DB_PATH"]).read_bytes()
    assert KEY_A1.encode() not in db, "审计不记明文"


def test_invalid_filter_is_no_match_other_errors_fail(portal, capsys):
    """1254018 ⇒ 查无记录 + 一条日志;其它错误码 ⇒ 502,不能说成「没有 key」。"""
    bad = {"union_id": "on_truncated", "name": "x"}
    assert portal.app.my_rows(bad) == []
    assert "InvalidFilter" in capsys.readouterr().out
    # POSITIVE:同一路径,真 id ⇒ 有行(证明空结果来自 1254018 分支,不是搜索本来就坏)
    assert portal.app.my_rows({"union_id": USERS["A"]["union_id"]})
    sa = portal.sid("A")
    FAKE.search_error = 91403                       # 比如 carher-1000 那种无权限
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 502 and "keys" not in d
    assert portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[0] == 502
    FAKE.search_error = 1254018                     # UNGUARDED 对照:换成 InvalidFilter ⇒ 就是空列表 200
    s, d, _, _ = portal.req("GET", "/ai/api/keys", cookie=sa)
    assert s == 200 and d["keys"] == []


def test_wrong_app_id_refused():
    cfg = S.load_config(environ={})
    cfg.update(FEISHU_APP_ID="cli_carher1000xxxx", FEISHU_APP_SECRET="x", SESSION_SECRET=SECRET)
    assert any("carher-236" in e for e in S.check_config(cfg))
    cfg["FEISHU_APP_ID"] = APP_ID                    # POSITIVE
    assert S.check_config(cfg) == []


def test_rotate_off_without_master_key(portal):
    """没配 LITELLM_MASTER_KEY ⇒ 功能整个关闭(501),表和「提反馈」那条路保持原样。"""
    sa = portal.sid("A")
    assert portal.app.litellm.enabled is False
    s, d, _, _ = portal.req("POST", "/ai/api/keys/rotate", {"record_id": "recA1"}, cookie=sa)
    assert s == 501 and d["message"] == "需要换 key 请提反馈"
    assert row_key() == KEY_A1, "没动表"
    assert portal.req("GET", "/ai/api/me", cookie=sa)[1]["rotate_enabled"] is False


# ════════════════════════════════════════════════════════════════════════
# 14. 一次性登录链接(magic link):外部人唯一的登录方式
# ════════════════════════════════════════════════════════════════════════
@pytest.fixture
def mportal(portal_factory):
    return portal_factory(INTERNAL_SECRET=INTERNAL)


def issue(p, who="X", secret=INTERNAL, body=None):
    u = USERS[who]
    b = body if body is not None else {"union_id": u["union_id"], "open_id": u["open_id"],
                                       "tenant_key": u["tenant_key"], "name": u["name"]}
    h = {"Authorization": "Bearer " + secret} if secret is not None else {}
    return p.req("POST", "/ai/api/internal/magic-link", b, origin=None, headers=h)


def tok_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))["t"]


def consume(p, t, xrw="ai-portal", origin=ORIGIN, ip="10.5.0.1"):
    h = {"X-Real-IP": ip}
    if xrw:
        h["X-Requested-With"] = xrw
    return p.req("POST", "/ai/auth/magic", {"t": t}, origin=origin, headers=h)


def magic_sid(p, who="X"):
    t = tok_of(issue(p, who)[1]["url"])
    s, d, _, sc = consume(p, t)
    assert s == 200 and d == {"ok": True}
    return "ai_sid=" + cookie_of(sc, "ai_sid")


def test_magic_full_flow_external(mportal):
    s, d, _, _ = issue(mportal)
    assert s == 200 and d["url"].startswith("https://chat.auto-link.com.cn/ai/auth/magic?t=") and d["expires_in"] == 600
    t = tok_of(d["url"])
    import re
    assert re.fullmatch(r"[A-Za-z0-9._~-]{8,512}", t) and len(t) >= 43, "前端 MAGIC_T_RE + ≥256 bit"
    s, d, _, sc = consume(mportal, t)
    c = [x for x in sc if x.startswith("ai_sid=")][0]
    assert s == 200 and d == {"ok": True} and all(f in c for f in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/ai/"))
    sid = "ai_sid=" + cookie_of(sc, "ai_sid")
    me = mportal.req("GET", "/ai/api/me", cookie=sid)[1]
    assert me["union_id"] == USERS["X"]["union_id"] and me["external"] is True and me["login"] == "magic"
    assert me["tenant"] == USERS["X"]["tenant_key"]
    # 和 OAuth 同一种会话结构:能查自己的 key、能 reveal
    ks = mportal.req("GET", "/ai/api/keys", cookie=sid)[1]["keys"]
    assert [k["record_id"] for k in ks] == ["recX1"]
    assert mportal.req("POST", "/ai/api/keys/reveal", {"record_id": "recX1"}, cookie=sid)[1]["key"] == KEY_X
    assert not [x for x in FAKE.calls if "/authen/" in x[1]], "magic 登录不走飞书 OAuth"


def test_magic_and_oauth_same_session_shape(mportal):
    so = mportal.sid("A")
    sm = magic_sid(mportal, "A")
    mo = mportal.req("GET", "/ai/api/me", cookie=so)[1]
    mm = mportal.req("GET", "/ai/api/me", cookie=sm)[1]
    assert set(mo) == set(mm) and mo["login"] == "oauth" and mm["login"] == "magic"
    rows, _ = mportal.app.q("select user from sessions")
    users = [json.loads(r[0]) for r in rows]
    assert {u["login"] for u in users} == {"oauth", "magic"}
    assert all(set(u) == {"union_id", "open_id", "tenant_key", "name", "avatar", "email", "external", "login",
                          "app_id"} and u["app_id"] == APP_ID for u in users)


def test_magic_get_does_not_consume(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    for _ in range(3):                                   # 飞书链接预览 / 安全扫描 / 用户刷新
        s, b, _, sc = mportal.req("GET", "/ai/auth/magic?t=" + t)
        assert s == 200 and "PORTAL-INDEX" in b and not cookie_of(sc, "ai_sid")
    assert consume(mportal, t)[0] == 200                 # POSITIVE:GET 过 3 次后 POST 仍能用


def test_magic_get_page_headers(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    c = http.client.HTTPConnection("127.0.0.1", mportal.port, timeout=5)
    c.request("GET", "/ai/auth/magic?t=" + t)
    r = c.getresponse()
    r.read()
    assert r.getheader("Referrer-Policy") == "no-referrer" and r.getheader("Cache-Control") == "no-store"


def test_magic_replay_rejected(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert consume(mportal, t)[0] == 200                 # POSITIVE 第一次
    s, d, _, sc = consume(mportal, t)
    assert s == 410 and d["error"] == "used" and d["message"] and not cookie_of(sc, "ai_sid")


def test_magic_replay_unguarded(mportal, monkeypatch):
    """UNGUARDED:不检查 used、标记已用恒成功 ⇒ 同一个 token 能登两次。"""
    t = tok_of(issue(mportal)[1]["url"])
    real_q = mportal.app.q

    def q(sql, args=(), one=False):
        if sql.startswith("update magic_tokens"):
            return [], 1
        if sql.startswith("select user, expires, used from magic_tokens"):
            r = real_q(sql, args, one)
            return (r[0], r[1], None) if r else r
        return real_q(sql, args, one)
    monkeypatch.setattr(mportal.app, "q", q)
    assert consume(mportal, t)[0] == 200 and consume(mportal, t)[0] == 200


def test_magic_expired(mportal, clock):
    t = tok_of(issue(mportal)[1]["url"])
    clock.t += 601
    s, d, _, _ = consume(mportal, t)
    assert s == 410 and d["error"] == "expired"
    t2 = tok_of(issue(mportal)[1]["url"])                # POSITIVE:599 秒时可用
    clock.t += 599
    assert consume(mportal, t2)[0] == 200


def test_magic_forged_token(mportal):
    good = tok_of(issue(mportal)[1]["url"])
    flipped = good[:-1] + ("A" if good[-1] != "A" else "B")
    for bad in (flipped, "A" * 43, "", "short", None, 123, good + "x" * 30, "../" + good):
        s, d, _, sc = consume(mportal, bad, ip="10.6.%d.1" % (hash(str(bad)) % 200))
        assert s == 400 and d["error"] == "invalid" and not cookie_of(sc, "ai_sid"), bad
    assert consume(mportal, good)[0] == 200              # POSITIVE:真 token 没被上面影响


def test_magic_token_stored_hashed(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert t.encode() not in Path(mportal.app.cfg["DB_PATH"]).read_bytes()
    h = mportal.app.q("select token_hash from magic_tokens", one=True)[0]
    assert len(h) == 64 and h != S.sha(t)


def test_magic_post_needs_xrw_and_origin(mportal):
    t = tok_of(issue(mportal)[1]["url"])
    assert consume(mportal, t, xrw=None)[0] == 403
    assert consume(mportal, t, xrw="evil")[0] == 403
    assert consume(mportal, t, origin="https://evil.example")[0] == 403
    assert consume(mportal, t)[0] == 200                 # POSITIVE:上面都没消费掉它


def test_magic_consume_rate_limited(mportal):
    codes = [consume(mportal, "B" * 43, ip="10.8.8.8")[0] for _ in range(11)]
    assert codes == [400] * 10 + [429]


def test_internal_wrong_secret(mportal):
    for sec in ("wrong-" + INTERNAL, "", INTERNAL[:-1], SECRET):
        s, d, _, _ = issue(mportal, secret=sec)
        assert s == 401 and "url" not in d, sec
    s, d, _, _ = issue(mportal, secret=None)
    assert s == 401
    s, d, _, _ = mportal.req("POST", "/ai/api/internal/magic-link", {"union_id": USERS["X"]["union_id"]},
                             origin=None, headers={"Authorization": INTERNAL})    # 没有 Bearer 前缀
    assert s == 401
    assert issue(mportal)[0] == 200                      # POSITIVE


def test_internal_wrong_secret_unguarded(mportal, monkeypatch):
    """UNGUARDED:鉴权恒过 ⇒ 错密钥也能签发。证明上一条 401 来自 internal_ok。"""
    monkeypatch.setattr(mportal.app, "internal_ok", lambda h: None)
    assert issue(mportal, secret="wrong")[0] == 200


def test_internal_endpoint_closed_without_secret(portal):
    """没配 INTERNAL_SECRET ⇒ 接口关闭:任何密钥都 404(连「密钥不对」都不说)。"""
    assert "INTERNAL_SECRET" not in S.DEFAULTS and portal.app.internal_secret == ""
    for sec in (INTERNAL, "", "anything"):
        assert issue(portal, secret=sec)[0] == 404
    assert issue(portal, secret=None)[0] == 404


def test_internal_endpoint_open_with_secret_positive(portal_factory):
    """POSITIVE 对照:同一份配置只多配一个 INTERNAL_SECRET ⇒ 200。证明上一条 404 是因为没配。"""
    p = portal_factory(INTERNAL_SECRET=INTERNAL)
    assert issue(p)[0] == 200


def test_internal_secret_config_checks(fake, tmp_path):
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET="short"))
    with pytest.raises(S.ConfigError):
        S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET=SECRET))       # 和 SESSION_SECRET 一样
    S.App(make_cfg(fake, tmp_path, INTERNAL_SECRET=INTERNAL))          # POSITIVE


def test_internal_bad_body(mportal):
    for b in ({}, {"union_id": "ou_xxxx"}, {"union_id": "on_"}, {"union_id": 1}, {"union_id": USERS["X"]["union_id"],
                                                                                   "name": ["x"]}):
        assert issue(mportal, body=b)[0] == 400, b


def test_internal_not_reachable_with_session_cookie(mportal):
    """登录用户(有 cookie、本站 Origin)也不能签 magic link,只认内部密钥。"""
    sid = mportal.sid("A")
    s, _, _, _ = mportal.req("POST", "/ai/api/internal/magic-link", {"union_id": USERS["A"]["union_id"]}, cookie=sid)
    assert s == 401


def test_magic_logs_no_secrets(mportal, capsys):
    capsys.readouterr()
    t = tok_of(issue(mportal)[1]["url"])
    consume(mportal, t)
    consume(mportal, t)
    out = capsys.readouterr().out
    assert "magic 签发" in out and "magic 拒绝 used" in out          # 阳性:确实在打日志
    assert t not in out and INTERNAL not in out


# ── 11-04 拿真表(1452 行)复核后补的三条 ──────────────────────────────────

def test_batch_across_minute_boundary_is_still_one_batch(portal, monkeypatch):
    """🔴 真表:刘国现名下 10 把零跑汽车客户 key 每 ~3 秒一把,跨了自然分钟边界(8+2)。
    旧规则按「同一分钟 ≥5」切桶 ⇒ 尾巴 2 把漏过,且因为最新被选成了**他本人的当前 key**。"""
    sa = portal.sid("A")
    for i in range(6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    edge = (T0 // 60000 + 20 * 24 * 60) * 60000 + 59000          # 某分钟的第 59 秒起步
    for i in range(10):
        FAKE.tables[KEYTBL]["recLP%d" % i] = row("A", "sk-LEAPMOTORclient%04dx" % i, "有效", edge + i * 3000, org="零跑汽车")
    ks = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert not [k for k in ks if k["record_id"].startswith("recLP")], ks
    assert [k for k in ks if k["current"]][0]["record_id"] == "recA1"
    # 阳性对照:用旧的「同一自然分钟」切桶,尾巴就会漏过并抢走 current —— 证明这条测得出那个 bug
    def by_minute(rows, user):
        pm = {}
        for r in rows:
            if r["at"]:
                pm[r["at"] // 60000] = pm.get(r["at"] // 60000, 0) + 1
        return [r for r in rows if r["org"] not in portal.app.exclude_orgs and not (r["at"] and pm[r["at"] // 60000] >= 5)]
    monkeypatch.setattr(portal.app, "drop_batches", by_minute)
    ks = portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]
    assert [k for k in ks if k["current"]][0]["record_id"].startswith("recLP")


def test_undated_legacy_rows_are_not_a_batch(portal):
    """🔴 真表:早年导入的行没有申请时间(at=0)。旧规则把它们同算进「第 0 分钟」,
    凑满 5 行就整组藏掉 —— 刘国现那 5 行恰好是他本人的 Cursor / Claude / CarHer key。"""
    sa = portal.sid("A")
    for i in range(6):
        del FAKE.tables[KEYTBL]["recBT%d" % i]
    for i in range(5):
        FAKE.tables[KEYTBL]["recOLD%d" % i] = row("A", "sk-LEGACYnodate%04dxxx" % i, "有效", None)
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert len([i for i in ids if i.startswith("recOLD")]) == 5, ids


@pytest.mark.parametrize("raw", ["重新生成中", "待生成（第三方合作伙伴）", "待生成（第三方合作伙伴补录）"])
def test_real_pending_statuses_map_to_pending(portal, raw):
    """🔴 真表 16 个状态选项里有这三个「在等 key」的值。只认「生成中」开头会漏掉它们
    (「重新生成中」不以「生成中」开头)⇒ 前端当未知状态置灰、不轮询,看着像坏了。"""
    sa = portal.sid("A")
    FAKE.tables[KEYTBL]["recPEND"] = row("A", "", raw, T0 + 30 * DAY)
    k = [x for x in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if x["record_id"] == "recPEND"][0]
    assert k["status"] == "生成中" and k["masked"] == ""


def test_redeem_returns_tool_and_base_url_for_installer(portal):
    """🔴 11-04 联调发现:签发时存了 tool,换取时却没回 ⇒ 安装器(installer/install.sh)
    拿不到 tool 就 die「安装服务没说要装哪个工具」⇒ **每一次安装都在第一步失败**。
    后端测试和安装器测试各自都是绿的(各对着自己写的假对端),只有接起来才看得见。
    这里钉住安装器依赖的契约;端到端版本在 .work/ai-portal/e2e/e2e.py(真后端 + 真安装器)。"""
    sa = portal.sid("A")
    for tool in ("cursor", "codex", "claude", "all"):
        code = portal.req("POST", "/ai/api/install-code", {"tool": tool}, cookie=sa)[1]["code"]
        s, b = _redeem(portal, code)
        assert s == 200 and b["key"] == KEY_A1, (s, b)
        assert b["tool"] == tool, b                          # 安装器按它决定装哪个工具
        assert "base_url" not in b, "安装器不读它;网关地址在子安装器自己的 BASE_URL 里,别留死字段"


def test_install_code_all_one_code_three_tools(portal):
    """tool=all:一个码装三个 —— 用户只复制一行 ⇒ 绝不能签三个码 / 让安装器换三次。
    换回来的 tool 必须原样是 "all"(安装器据此依次装 Codex → Claude Code → Cursor),
    key 还是**同一把** current 的(三个工具本来就共用这一把)。"""
    sa = portal.sid("A")
    s, d, _, _ = portal.req("POST", "/ai/api/install-code", {"tool": "all"}, cookie=sa)
    assert s == 200 and d["tool"] == "all" and d["expires_in"] == 600, d
    import re
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,63}", d["code"]), "前端 CODE_RE:会拼进 shell 命令"
    # 库里存的就是 "all"(一列文本,不改表)
    assert portal.app.q("select tool from install_codes", one=True)[0] == "all"
    s, r = _redeem(portal, d["code"])
    assert s == 200 and r == {"key": KEY_A1, "account_type": "Cursor", "tool": "all"}, r
    # 还是一次性:同一个码换第二次就 400(安装器只许换一次,key 自己复用给三个工具)
    assert _redeem(portal, d["code"])[0] == 400


def test_install_code_rejects_unknown_tool_but_takes_all(portal):
    """tool 白名单 = cursor/codex/claude/all,别的一律 400(会拼进 shell 命令的东西不许放开)。"""
    sa = portal.sid("A")
    for bad in ("ALL", "all,cursor", "cursor codex", ["codex", "claude"], "", None, "rm -rf", "*"):
        s, d, _, _ = portal.req("POST", "/ai/api/install-code", {"tool": bad}, cookie=sa)
        assert s == 400, (bad, s, d)
    assert portal.req("POST", "/ai/api/install-code", {"tool": "all"}, cookie=sa)[0] == 200   # POSITIVE
    assert "all" in portal.req("GET", "/ai/api/me", cookie=sa)[1]["issuable_tools"]


def test_pilot_gate_blocks_both_login_paths(mportal):
    """灰度闸:名单非空时只有名单里的人能登录 —— OAuth 和一次性登录链接**两条路都要被管住**。
    阳性对照:名单里的人两条路都能进;清空名单 ⇒ 所有人都能进。"""
    mportal.app.cfg["PILOT_UNION_IDS"] = USERS["A"]["union_id"] + "," + USERS["X"]["union_id"]
    try:
        assert mportal.sid("A")                                   # POSITIVE:名单内 OAuth 能进
        assert magic_sid(mportal, "X")                            # POSITIVE:名单内 magic 能进
        st, _, sid, _ = mportal.login("B")
        assert not sid, "名单外的人走 OAuth 不该拿到会话"
        mportal.app.cfg["PILOT_UNION_IDS"] = USERS["A"]["union_id"]
        t = tok_of(issue(mportal, "X")[1]["url"])
        s, d, _, sc = consume(mportal, t)
        assert s == 403 and "内测" in d.get("message", "") and not cookie_of(sc, "ai_sid"), (s, d)
    finally:
        mportal.app.cfg["PILOT_UNION_IDS"] = ""
    assert mportal.sid("B")                                       # 清空 ⇒ 对所有人开放


@pytest.mark.parametrize("ghost", [{"name": "张三(拿不到 union_id)"}, {"id": ""}, {"id": None}, {}, "字符串成员"])
def test_shared_row_with_idless_member_is_nobodys(portal, ghost):
    """🔴 11-04 红队:人员格里混进一个**没有 id 的成员**时,原实现先过滤再数个数
    ⇒ `[我, {"name":…}]` 被判成「只属于我」⇒ 共用行的明文 key 被当成自己的交出去。
    缺 id 的成员也算一个人 ⇒ 这一格不是独占 ⇒ 不属于任何人。"""
    FAKE.tables[KEYTBL]["recGHOST"] = row("A", "sk-GHOSTghost0000shared", "有效", T0 + 40 * DAY)
    FAKE.tables[KEYTBL]["recGHOST"]["姓名"] = person("A") + [ghost]
    assert not S.owner_ok(FAKE.tables[KEYTBL]["recGHOST"]["姓名"], USERS["A"]["union_id"])
    sa = portal.sid("A")
    ids = [k["record_id"] for k in portal.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"]]
    assert "recGHOST" not in ids, ids
    assert portal.req("POST", "/ai/api/keys/reveal", {"record_id": "recGHOST"}, cookie=sa)[0] == 404
    # POSITIVE:同一行只留本人一个成员 ⇒ 立刻变成自己的(证明上面拒绝是因为那个幽灵成员,不是别的原因)
    FAKE.tables[KEYTBL]["recGHOST"]["姓名"] = person("A")
    assert S.owner_ok(FAKE.tables[KEYTBL]["recGHOST"]["姓名"], USERS["A"]["union_id"])


def test_keys_and_apply_are_rate_limited(portal):
    """🔴 11-04 红队 P1-1:这两个接口每次都真打飞书(全租户共享的 tenant token),
    原来一点限流都没有 ⇒ 一个人就能把飞书接口打满,连带 key poller / 反馈服务一起受影响。"""
    sa = portal.sid("A")
    portal.app.cfg["KEYS_PER_MIN"], portal.app.cfg["APPLY_PER_MIN"] = "3", "2"
    portal.app.rl_keys = S.RateLimiter(3, 60, portal.app.now)
    portal.app.rl_apply = S.RateLimiter(2, 60, portal.app.now)
    codes = [portal.req("GET", "/ai/api/keys", cookie=sa)[0] for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429], codes
    codes = [portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sa)[0] for _ in range(4)]
    assert codes.count(429) == 2, codes


def test_rate_limiter_forgets_idle_keys(portal):
    """P1-4:常驻进程里每个 IP / union_id 都留一个条目,只增不减会慢慢涨内存。"""
    t = [1000.0]
    rl = S.RateLimiter(5, 60, lambda: t[0])
    for i in range(2000):
        rl.hit("ip-%d" % i)
    assert len(rl.hits) == 2000
    t[0] += 120                      # 全部过期
    for i in range(2000):
        rl.allowed("ip-%d" % i)      # 只读一次就该把空条目清掉
    assert len(rl.hits) == 0, len(rl.hits)
    # 阳性对照:限流本身还管用(清理不能把计数清掉 —— 我第一版就这么挂的)
    assert [rl.hit("x") for _ in range(7)] == [True] * 5 + [False] * 2


def test_tightening_pilot_gate_kills_existing_sessions(portal):
    """🔴 11-04 红队 P1-5:灰度闸原来只在建会话时判 ⇒ 收紧名单后,已经登录的人
    拿着旧 cookie 还能用满 12 小时(实测还能签安装码)。现在每次请求都过闸。"""
    sa = portal.sid("A")
    assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 200          # POSITIVE:名单为空时正常
    portal.app.cfg["PILOT_UNION_IDS"] = USERS["B"]["union_id"]           # 收紧成只剩 B
    try:
        assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 401
        assert portal.req("POST", "/ai/api/install-code", {"tool": "codex"}, cookie=sa)[0] == 401
        assert portal.req("GET", "/ai/api/keys", cookie=sa)[0] == 401
    finally:
        portal.app.cfg["PILOT_UNION_IDS"] = ""
    assert portal.req("GET", "/ai/api/me", cookie=sa)[0] == 200          # 放开后旧会话照常可用


# ════════════════════════════════════════════════════════════════════════
# 16. 重置我的 key(轮换)
#
# 顺序是产品承诺的一部分:**先生成新的 → 回读 → 真发一发 → 写表 → 最后封旧的**。
# 下面每条失败路径都断言「旧 key 还能用 + 表没动」,就是在钉这条承诺。
# 全程只打假飞书 + 假 198 网关,**不碰任何真 key**。
# ════════════════════════════════════════════════════════════════════════
def test_rotate_happy_path(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 200, d
    new = d["key"]
    # ① 回给用户的是新明文,不是旧的
    assert new.startswith("sk-") and new != KEY_A1 and d["masked"] == S.mask_key(new)
    assert d["dry_run"] is False and d["record_id"] == "recA1" and d["old_blocked"] is True
    # ② 表里那一行换成了新明文;状态是表上**已有**的「有效（已重新生成）」⇒ 前端仍是「可用」
    assert row_key() == new
    f = FAKE.tables[KEYTBL]["recA1"]
    assert f["状态"] == "有效（已重新生成）" and S.front_status(f["状态"], new) == "可用"
    assert f["key_alias"] == "cursor-zhangsan", "表里历来写基名,不带 4 位后缀"
    assert f["申请时间"] == T0 + 3 * DAY, "申请时间不动(聚类规则读它)"
    assert f["Cursor Key"] == new, "副本列存的就是旧 key ⇒ 跟着换"
    # ③ 超链接体检列跟着换了(它不会自己跟 API Key 走)
    assert f["点我体检"] == {"link": "https://chat.auto-link.com.cn/keyprobe/#k=" + new, "text": "▶ 测这把 key"}
    # ④ 新 key 在网关上:配置逐字段平移过来了
    row = LITE.keys[S.sha(new)]
    old = LITE.keys.get(S.sha(KEY_A1))
    assert row["models"] == GW_MODELS and row["max_budget"] == 20.0 and row["budget_duration"] == "30d"
    assert row["team_id"] == "team-cursor-198" and row["user_id"] == "cursor-zhangsan"
    assert row["metadata"]["lark_open_id"] == "ou_xxx", "身份 metadata 跟过来"
    assert row["metadata"]["rotated_from"] == "cursor-zhangsan-ab12" and row["metadata"]["rotated_at"]
    assert row["key_alias"] == d["new_alias"] != "cursor-zhangsan-ab12"
    assert S.alias_base(row["key_alias"]) == "cursor-zhangsan", "基名不变 ⇒ 名字里的人还在"
    # ⑤ 旧 key:改名退役 + 封禁(不是删除 ⇒ 还能解封救回来)
    assert old is not None, "旧 key 不该被删掉"
    assert old["blocked"] is True and old["key_alias"].startswith("cursor-zhangsan-retired-")
    # ⑥ 顺序:generate 在 update(封旧)之前
    paths = [c[1] for c in LITE.calls]
    assert paths.index("/pro/key/generate") < paths.index("/pro/key/update")
    assert "/pro/v1/chat/completions" in paths, "换表前真发了一发"
    assert "/pro/key/delete" not in paths, "成功路径上不删任何东西"


def test_rotate_new_key_works_old_key_is_dead(rotate_portal):
    """阳性 + 阴性对照:新 key 真能调,旧 key 真被拒。只认真实调用,不认「字段写对了」。"""
    p = rotate_portal()
    new = do_rotate(p, p.sid("A"))[1]["key"]
    L = p.app.litellm
    assert L.probe(new, "gpt-5.5", 16)[0] is True                  # 阳性
    ok, why = L.probe(KEY_A1, "gpt-5.5", 16)
    assert ok is False and "401" in why, why                       # 阴性:旧的调不动了


def test_rotate_only_own_current_key(rotate_portal):
    """只能重置**自己当前那把**:前端传谁的 record_id 都不决定改谁。"""
    p = rotate_portal(ROTATE_PER_MIN="100")          # 这条要连打 9 次,别让每分钟限流先挡住
    sa = p.sid("A")
    before_b, before_a2 = row_key("recB1"), row_key("recA2")
    for rid, want in (("recB1", 409),        # 别人的行
                      ("recX1", 409),        # 外部人的行
                      ("recAB", 409),        # A+B 多人格(谁的都不算)
                      ("recQ1", 409),        # 客户批量 key(巧虎)
                      ("recBT0", 409),       # 客户批量(聚类排除)
                      ("recA2", 409),        # 自己的、但不是 current(Claude Code 那把)
                      ("recA3", 409),        # 自己的已禁用行
                      ("recNOPE", 409)):     # 不存在
        s, d, _, _ = do_rotate(p, sa, {"record_id": rid})
        assert s == want, (rid, s, d)
        assert "key" not in d and "sk-" not in json.dumps(d, ensure_ascii=False), rid
    # 一把都没动:别人的行、自己的其它行、网关上 B 的 key 全原样
    assert row_key("recB1") == before_b and row_key("recA2") == before_a2 and row_key() == KEY_A1
    assert gw_alias(KEY_B) == "cursor-lisi-cd34" and LITE.keys[S.sha(KEY_B)]["blocked"] is False
    assert [c for c in LITE.calls if c[1] in ("/pro/key/generate", "/pro/key/update", "/pro/key/delete")] == []
    # POSITIVE:同一个人不传 record_id(或传 current 那个)⇒ 成功。证明上面被拒是归属/current 判断
    assert do_rotate(p, sa, {"record_id": "recA1"})[0] == 200


def test_rotate_b_rotating_does_not_touch_a(rotate_portal):
    """B 重置自己的,A 的 key 完全不受影响(按登录身份查 current,不是按请求体)。"""
    p = rotate_portal()
    sb = p.sid("B")
    s, d, _, _ = do_rotate(p, sb, {"record_id": "recA1"})        # B 拿着 A 的 record_id 来
    assert s == 409 and "key" not in d
    assert row_key() == KEY_A1 and LITE.keys[S.sha(KEY_A1)]["blocked"] is False
    # POSITIVE:B 不传 record_id ⇒ 改的是 B 自己那行,A 仍然不动
    s, d, _, _ = do_rotate(p, sb)
    assert s == 200 and d["record_id"] == "recB1" and row_key("recB1") == d["key"]
    assert row_key() == KEY_A1 and LITE.keys[S.sha(KEY_A1)]["blocked"] is False


def test_rotate_dry_run_default_and_writes_nothing(rotate_portal):
    """dry-run 是默认:只回「将会怎么做」,**一次写都不发**(飞书和网关都查得到证据)。"""
    assert S.DEFAULTS["ROTATE_DRY_RUN"] == "1"
    p = rotate_portal(ROTATE_DRY_RUN="1")
    sa = p.sid("A")
    before = json.dumps(FAKE.tables[KEYTBL], sort_keys=True, ensure_ascii=False)
    gw_before = json.dumps(LITE.keys, sort_keys=True)
    LITE.calls.clear()
    FAKE.calls.clear()
    s, d, _, _ = do_rotate(p, sa)
    assert s == 200 and d["dry_run"] is True and "key" not in d
    assert d["old_alias"] == "cursor-zhangsan-ab12" and S.alias_base(d["new_alias"]) == "cursor-zhangsan"
    assert "models" in d["carry"] and "max_budget" in d["carry"] and d["masked"] == S.mask_key(KEY_A1)
    assert json.dumps(FAKE.tables[KEYTBL], sort_keys=True, ensure_ascii=False) == before, "表没动"
    assert json.dumps(LITE.keys, sort_keys=True) == gw_before, "网关没动"
    assert [c for c in LITE.calls if c[0] == "POST"] == [], "dry-run 不许发任何 POST"
    assert [c for c in FAKE.calls if c[0] == "PUT"] == [], "dry-run 不许写表"
    assert [c for c in LITE.calls if c[1] == "/pro/key/info"], "阳性:确实读过网关(只读)"
    # UNGUARDED:关掉 dry-run ⇒ 同一个请求就真写了。证明上面「没动」来自这个开关
    p.app.cfg["ROTATE_DRY_RUN"] = "0"
    assert do_rotate(p, sa)[1]["dry_run"] is False and row_key() != KEY_A1


def test_rotate_cooldown_blocks_second_reset(rotate_portal, clock):
    """频率限制:成功一次之后 N 分钟内不能再重置(防连点生成一堆 key)。"""
    p = rotate_portal(ROTATE_COOLDOWN="600")
    sa = p.sid("A")
    first = do_rotate(p, sa)[1]["key"]
    n = len(LITE.keys)
    s, d, _, _ = do_rotate(p, sa)
    assert s == 429 and "分钟" in d["message"] and "key" not in d
    assert len(LITE.keys) == n, "被限流时一把新 key 都不能生成"
    assert row_key() == first, "表也没再动"
    # POSITIVE:冷却过去了就能再重置(证明 429 是冷却,不是 key 坏了)
    clock.t += 601
    s2, d2, _, _ = do_rotate(p, sa)
    assert s2 == 200 and d2["key"] not in (first, KEY_A1)
    # 关掉冷却 ⇒ 立刻又能重置。证明上面的 429 来自 ROTATE_COOLDOWN
    p.app.cfg["ROTATE_COOLDOWN"] = "0"
    assert do_rotate(p, sa)[0] == 200


def test_rotate_failed_attempt_does_not_consume_cooldown(rotate_portal):
    """只有**成功**才进冷却:生成失败不该把人锁在外面 10 分钟。"""
    p = rotate_portal(ROTATE_COOLDOWN="600")
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "boom")
    assert do_rotate(p, sa)[0] == 502
    LITE.fail.clear()
    assert do_rotate(p, sa)[0] == 200, "上一次失败不占冷却额度"


def test_rotate_per_minute_rate_limit(rotate_portal, clock):
    p = rotate_portal(ROTATE_COOLDOWN="0", ROTATE_PER_MIN="3")
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "boom")     # 让每次都失败,只看限流计数
    codes = [do_rotate(p, sa)[0] for _ in range(4)]
    assert codes == [502, 502, 502, 429], codes
    clock.t += 61
    assert do_rotate(p, sa)[0] == 502, "窗口过去后恢复"


def test_rotate_concurrent_only_one_wins(rotate_portal):
    """并发只有一个成功:进行中锁(主键 union_id)。两个都成功 = 生成了两把、表只留一把。"""
    p = rotate_portal(ROTATE_COOLDOWN="0")
    sa = p.sid("A")
    gate = threading.Event()
    seen = []

    def hook(plain, row):
        seen.append(plain)
        gate.wait(5)          # 第一个请求卡在 generate 之后,让第二个请求撞上锁
    LITE.generate_hook = hook
    out = []
    t = threading.Thread(target=lambda: out.append(do_rotate(p, sa)))
    t.start()
    for _ in range(100):      # 等第一个真的进到 generate 里面
        if seen:
            break
        time.sleep(0.02)
    assert seen, "第一个请求没走到 generate"
    second = do_rotate(p, sa)
    gate.set()
    t.join(20)
    LITE.generate_hook = None
    assert second[0] == 409 and "key" not in second[1], second
    assert out and out[0][0] == 200, out
    assert len(seen) == 1, "只生成了一把新 key"
    assert row_key() == out[0][1]["key"]


def test_rotate_lock_released_after_failure(rotate_portal):
    """失败也要解锁,不然一次网关抖动就把这个人永久锁住。"""
    p = rotate_portal(ROTATE_COOLDOWN="0")
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "boom")
    assert do_rotate(p, sa)[0] == 502
    assert p.app.q("select count(*) from rotate_locks", one=True)[0] == 0
    LITE.fail.clear()
    assert do_rotate(p, sa)[0] == 200, "上一次失败没把锁留下"


def test_rotate_stale_lock_expires(rotate_portal, clock):
    """进程中途被杀留下的锁有兜底过期,不会永久锁住一个人。"""
    p = rotate_portal(ROTATE_COOLDOWN="0", ROTATE_LOCK_TTL="300")
    sa = p.sid("A")
    p.app.q("insert into rotate_locks values(?,?,?)", (USERS["A"]["union_id"], "recA1", p.app.now()))
    assert do_rotate(p, sa)[0] == 409
    clock.t += 301
    assert do_rotate(p, sa)[0] == 200


# ── 失败路径:每一条都必须落在「旧的还能用」上 ─────────────────────────────
def test_rotate_generate_failure_leaves_old_key_usable(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "upstream exploded")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "还能继续用" in d["message"] and "key" not in d
    assert "upstream exploded" not in json.dumps(d, ensure_ascii=False), "不把上游原文回给浏览器"
    assert row_key() == KEY_A1 and LITE.keys[S.sha(KEY_A1)]["blocked"] is False
    assert p.app.litellm.probe(KEY_A1, "gpt-5.5", 16)[0] is True, "旧 key 真的还能调"


def test_rotate_readback_failure_rolls_back_new_key(rotate_portal):
    """生成成功但回读不回来 ⇒ 删掉新的,回到「只有旧 key」。绝不留两把活的。"""
    p = rotate_portal()
    sa = p.sid("A")
    calls = {"n": 0}
    real = p.app.litellm.key_info

    def flaky(h):
        calls["n"] += 1
        if calls["n"] >= 2:           # 第 1 次是读旧 key,第 2 次是回读新 key
            raise S.LiteLLMError("/key/info", 500, "flaky")
        return real(h)
    p.app.litellm.key_info = flaky
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "撤掉" in d["message"] and "key" not in d
    assert row_key() == KEY_A1, "表没动"
    assert gw_alias(KEY_A1) == "cursor-zhangsan-ab12" and LITE.keys[S.sha(KEY_A1)]["blocked"] is False
    assert len(LITE.keys) == 2, "新生成那把已经删掉了(只剩 A、B 两把)"
    assert ("POST", "/pro/key/delete") in LITE.calls


def test_rotate_config_mismatch_rolls_back(rotate_portal):
    """回读到了,但预算/权限没跟过来 ⇒ 当失败处理并回滚。「建出来了」≠「闸装上了」。"""
    p = rotate_portal()
    sa = p.sid("A")
    real = p.app.litellm.generate

    def lossy(payload):
        r = real(dict(payload, max_budget=None))       # 模拟服务端把预算吞了
        LITE.keys[S.sha(r["key"])]["max_budget"] = None
        return r
    p.app.litellm.generate = lossy
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "max_budget" in d["message"] and "key" not in d
    assert row_key() == KEY_A1 and len(LITE.keys) == 2, "新的已回滚"


def test_rotate_probe_failure_rolls_back(rotate_portal):
    """🔴 建出来、字段也对,但真发一发打不通 ⇒ 不许写表。/key/info 读得回来不证明能调。"""
    p = rotate_portal()
    sa = p.sid("A")
    real = p.app.litellm.generate

    def gen(payload):
        r = real(payload)
        LITE.probe_401_for.add(S.sha(r["key"]))
        return r
    p.app.litellm.generate = gen
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "试调不通" in d["message"] and "key" not in d
    assert row_key() == KEY_A1 and len(LITE.keys) == 2
    assert p.app.litellm.probe(KEY_A1, "gpt-5.5", 16)[0] is True, "旧 key 还能用"
    # UNGUARDED:把探针关掉 ⇒ 同一个坏 key 就会被写进表(证明这条是探针在挡)
    LITE.probe_401_for.clear()
    p.app.cfg["ROTATE_PROBE"] = "0"
    p.app.cfg["ROTATE_COOLDOWN"] = "0"

    def gen2(payload):
        r = real(payload)
        LITE.probe_401_for.add(S.sha(r["key"]))
        return r
    p.app.litellm.generate = gen2
    s2, d2, _, _ = do_rotate(p, sa)
    assert s2 == 200 and row_key() == d2["key"]


def test_rotate_bitable_write_failure_rolls_back(rotate_portal):
    """🔴 最糟的那种:新 key 生成了但表没更新 = 用户拿不到新 key。必须回滚成「只有旧 key」。"""
    p = rotate_portal()
    sa = p.sid("A")
    FAKE.update_error = 1254045
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "没写进表里" in d["message"] and "key" not in d
    assert row_key() == KEY_A1, "表里还是旧明文"
    assert FAKE.tables[KEYTBL]["recA1"]["状态"] == "有效", "状态也没被改坏"
    assert gw_alias(KEY_A1) == "cursor-zhangsan-ab12" and LITE.keys[S.sha(KEY_A1)]["blocked"] is False
    assert len(LITE.keys) == 2, "新生成那把已删掉 ⇒ 不存在两把都活着"
    assert p.app.litellm.probe(KEY_A1, "gpt-5.5", 16)[0] is True
    # POSITIVE:写表恢复 ⇒ 同一个请求成功。证明 502 来自写表失败
    FAKE.update_error = None
    p.app.cfg["ROTATE_COOLDOWN"] = "0"
    assert do_rotate(p, sa)[0] == 200


def test_rotate_rollback_delete_also_fails_is_reported(rotate_portal):
    """写表失败 + 回滚删除也失败 ⇒ 两把都活着。必须明确告知,不许假装成功。"""
    p = rotate_portal()
    sa = p.sid("A")
    FAKE.update_error = 1254045
    LITE.fail["/key/delete"] = (500, "cannot delete")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "没撤掉" in d["message"] and "提一条反馈" in d["message"]
    assert row_key() == KEY_A1 and len(LITE.keys) == 3, "确实留了一把孤儿(所以要告知)"
    assert [r for r in audit_rows(p) if r[1] == "orphan"], "审计里有 orphan 这一条,运维能捞出来"


def test_rotate_block_old_failure_still_succeeds_with_warning(rotate_portal):
    """封旧 key 放在最后:这一步失败**不算整体失败** —— 用户已经拿到能用的新 key 了。"""
    p = rotate_portal()
    sa = p.sid("A")
    LITE.fail["/key/update"] = (500, "block failed")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 200 and d["key"] != KEY_A1 and d["old_blocked"] is False
    assert "旧 key 没能立刻停用" in d["warning"]
    assert row_key() == d["key"], "新 key 已经在表里 ⇒ 用户能用"
    assert LITE.keys[S.sha(KEY_A1)]["blocked"] is False, "旧的确实没封上(所以要警告)"
    assert [r for r in audit_rows(p) if r[0] == "block-old" and r[1] == "fail"]


def test_rotate_gateway_unknown_key_refuses(rotate_portal):
    """表里有明文、网关上查不到(已经被别的流程换过)⇒ 停手,不猜、不写。"""
    p = rotate_portal()
    sa = p.sid("A")
    LITE.keys.pop(S.sha(KEY_A1))
    s, d, _, _ = do_rotate(p, sa)
    assert s == 409 and "查不到" in d["message"] and "没有做任何改动" in d["message"]
    assert row_key() == KEY_A1 and len(LITE.keys) == 1


def test_rotate_gateway_read_error_is_502_not_a_guess(rotate_portal):
    """读旧 key 时网关出错 ≠「这把 key 不存在」。必须 502,不能当成「可以新建一把」。"""
    p = rotate_portal()
    sa = p.sid("A")
    LITE.fail["/key/info"] = (500, "db down")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and "没有做任何改动" in d["message"]
    assert len(LITE.keys) == 2 and row_key() == KEY_A1
    assert [c for c in LITE.calls if c[1] == "/pro/key/generate"] == []


def test_rotate_key_without_alias_refused(rotate_portal):
    """网关上这把 key 没有规范 alias ⇒ 没法改名退役、也没法给新 key 起名 ⇒ 失败关闭,不猜名字。"""
    p = rotate_portal(ROTATE_PER_MIN="100")
    sa = p.sid("A")
    for bad in (None, "", "   ", "a", "bad name", "x" * 300, "../etc/passwd"):
        LITE.keys[S.sha(KEY_A1)]["key_alias"] = bad
        s, d, _, _ = do_rotate(p, sa)
        assert s == 409 and "规范的名字" in d["message"], (bad, s, d)
        assert row_key() == KEY_A1 and len(LITE.keys) == 2, bad
    LITE.keys[S.sha(KEY_A1)]["key_alias"] = "cursor-zhangsan-ab12"     # POSITIVE
    assert do_rotate(p, sa)[0] == 200


def test_rotate_new_suffix_never_equals_old(rotate_portal, monkeypatch):
    """随机后缀恰好和旧 key 的后缀一样 ⇒ 换一个(新旧同名的话 generate 会 400,用户白失败一次)。
    ⚠️ 只防「和自己旧 key 同名」;撞上**别人**的 alias 概率是 36^-4 且基名按人区分,撞了就是 502 让他重试。"""
    p = rotate_portal()
    sa = p.sid("A")
    real = S.secrets.choice
    seq = iter("ab12")                      # 第一轮 4 个字符恰好拼出旧后缀 ab12

    def choice(xs):
        try:
            return next(seq)
        except StopIteration:
            return real(xs)
    monkeypatch.setattr(S.secrets, "choice", choice)
    s, d, _, _ = do_rotate(p, sa)
    assert s == 200 and d["new_alias"] != "cursor-zhangsan-ab12", d
    assert S.alias_base(d["new_alias"]) == "cursor-zhangsan"


# ── 鉴权 / 审计 / 不记明文 ────────────────────────────────────────────────
def test_rotate_unauthenticated_is_401(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    for bad in (None, "ai_sid=", "ai_sid=forged.sig", sa + "x", sa.split(".")[0] + ".deadbeef"):
        s, d, _, _ = do_rotate(p, bad)
        assert s == 401 and "key" not in d, bad
    assert row_key() == KEY_A1 and len(LITE.keys) == 2, "未登录的请求一次写都没发出去"
    assert do_rotate(p, sa)[0] == 200                              # POSITIVE


def test_rotate_checks_origin(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    for o in ("https://evil.example", "null", "https://chat.auto-link.com.cn.evil.example"):
        s, _, _, _ = p.req("POST", "/ai/api/keys/rotate", {}, cookie=sa, origin=o)
        assert s == 403, o
    assert p.req("POST", "/ai/api/keys/rotate", {}, cookie=sa, origin=None)[0] == 403
    assert row_key() == KEY_A1
    assert do_rotate(p, sa)[0] == 200                              # POSITIVE


def test_rotate_pilot_gate_applies(rotate_portal):
    """灰度名单外的人连会话都没有 ⇒ 重置入口自然也进不来。"""
    p = rotate_portal()
    sa = p.sid("A")
    p.app.cfg["PILOT_UNION_IDS"] = USERS["B"]["union_id"]
    try:
        assert do_rotate(p, sa)[0] == 401
        assert row_key() == KEY_A1
    finally:
        p.app.cfg["PILOT_UNION_IDS"] = ""
    assert do_rotate(p, sa)[0] == 200                              # POSITIVE


def test_rotate_bad_record_id(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    for bad in (123, "", "../x", "recA1' or 1=1", ["recA1"], {"x": 1}):
        s, d, _, _ = do_rotate(p, sa, {"record_id": bad})
        assert s == 400 and "key" not in d, bad
    assert row_key() == KEY_A1 and len(LITE.keys) == 2


def test_rotate_audit_has_every_step_and_no_plaintext(rotate_portal):
    """审计:谁、什么时候、旧 alias、新 alias、每一步的结果 —— 且**一个明文都没有**。"""
    p = rotate_portal()
    sa = p.sid("A")
    new = do_rotate(p, sa, ip="10.7.7.1")[1]["key"]
    rows = audit_rows(p)
    steps = [(r[0], r[1]) for r in rows]
    assert ("generate", "ok") in steps and ("probe", "ok") in steps
    assert ("bitable", "ok") in steps and ("block-old", "ok") in steps and ("done", "ok") in steps
    for r in rows:
        assert r[2] == "cursor-zhangsan-ab12" or r[2] == "", r
        assert r[4] == USERS["A"]["union_id"] and r[5] == "10.7.7.1" and r[6] == "张三" and r[7] == "oauth"
        assert r[9], "要有时间戳"
    assert {r[3] for r in rows if r[3]} == {[x[3] for x in rows if x[3]][0]}, "新 alias 只有一个"
    # 🔴 不记明文:库文件里既没有旧明文也没有新明文
    db = Path(p.app.cfg["DB_PATH"]).read_bytes()
    assert KEY_A1.encode() not in db and new.encode() not in db and MASTER.encode() not in db
    assert not S.SK_RE.search(json.dumps(rows, ensure_ascii=False))


def test_rotate_audit_records_failures_too(rotate_portal):
    """失败也要有审计行(否则排障时只看得到成功的那些)。"""
    p = rotate_portal(ROTATE_COOLDOWN="0")
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "boom sk-shouldnotleak1234")
    assert do_rotate(p, sa)[0] == 502
    rows = audit_rows(p)
    assert [r for r in rows if (r[0], r[1]) == ("generate", "fail")]
    assert "shouldnotleak" not in json.dumps(rows), "上游原话里夹的 key 也要打码"
    assert not S.SK_RE.search(json.dumps(rows, ensure_ascii=False)), "只许出现 sk-…1234 这种打码形状"


def test_rotate_logs_never_contain_secrets(rotate_portal, capsys):
    p = rotate_portal(ROTATE_COOLDOWN="0")
    sa = p.sid("A")
    capsys.readouterr()
    new = do_rotate(p, sa)[1]["key"]
    LITE.fail["/key/generate"] = (500, "boom")
    do_rotate(p, sa)
    out = capsys.readouterr()
    logs = out.out + out.err
    assert "rotate 完成" in logs and "rotate 生成失败" in logs, "阳性:确实在打日志"
    for s in (KEY_A1, new, MASTER, APP_SECRET, SECRET):
        assert s not in logs, s[:8]


def test_rotate_response_carries_message_for_frontend(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    LITE.fail["/key/generate"] = (500, "boom")
    s, d, _, _ = do_rotate(p, sa)
    assert s == 502 and d["message"] and d["message"] == d["error"]


def test_me_exposes_rotate_flags(rotate_portal):
    p = rotate_portal(ROTATE_DRY_RUN="1")
    me = p.req("GET", "/ai/api/me", cookie=p.sid("A"))[1]
    assert me["rotate_enabled"] is True and me["rotate_dry_run"] is True
    p2 = rotate_portal(ROTATE_DRY_RUN="0")
    assert p2.req("GET", "/ai/api/me", cookie=p2.sid("A"))[1]["rotate_dry_run"] is False


def test_rotate_master_key_never_reaches_browser_or_table(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    d = do_rotate(p, sa)[1]
    assert MASTER not in json.dumps(d, ensure_ascii=False)
    assert MASTER not in json.dumps(list(FAKE.tables.values()), ensure_ascii=False)
    assert MASTER not in json.dumps(p.req("GET", "/ai/api/keys", cookie=sa)[1], ensure_ascii=False)


def test_rotate_keys_list_masks_new_key(rotate_portal):
    """重置完刷新列表:仍然只给打码,明文只有 reveal / 重置那一次的响应里有。"""
    p = rotate_portal()
    sa = p.sid("A")
    new = do_rotate(p, sa)[1]["key"]
    d = p.req("GET", "/ai/api/keys", cookie=sa)[1]
    raw = json.dumps(d, ensure_ascii=False)
    assert new not in raw and S.mask_key(new) in raw
    m = {k["record_id"]: k for k in d["keys"]}
    assert m["recA1"]["status"] == "可用" and m["recA1"]["current"] is True
    # 重置完还能照常拿安装码、换出来的就是新 key
    code = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(p, code)[1]["key"] == new
    # reveal 出来的也是新的
    assert p.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa)[1]["key"] == new


# ── 纯函数 ───────────────────────────────────────────────────────────────
def test_alias_base_only_strips_the_pollers_suffix():
    """🔴 真表里有 `cursor-buyitian` 这种**没有后缀**的 alias。
    无脑 rsplit('-',1) 会把它剥成 `cursor` ⇒ 新 key 叫 `cursor-xxxx`,名字里的人没了。"""
    assert S.alias_base("cursor-zhangsan-ab12") == "cursor-zhangsan"
    assert S.alias_base("cursor-buyitian") == "cursor-buyitian"
    assert S.alias_base("cursor-wanghengzhang-49q3") == "cursor-wanghengzhang"
    assert S.alias_base("cursor-wangzhongwei-w1qn") == "cursor-wangzhongwei"
    # 只认 poller 那种**恰好 4 位**的后缀。手工起的 `-std`(3 位)不剥 ⇒ 新 key 叫
    # `…-std-xxxx`,名字稍长但人没丢;剥错了才会撞到别人头上,所以宁可不剥。
    assert S.alias_base("claude-code-liuguoxian-std") == "claude-code-liuguoxian-std"
    assert S.alias_base("") == "" and S.alias_base(None) == ""
    # 基名幂等:对已剥过的再剥一次不该继续吃掉东西(除非它本来就像个后缀)
    assert S.alias_base(S.alias_base("cursor-zhangsan-ab12")) == "cursor-zhangsan"
    assert S.new_alias("cursor-zhangsan-ab12", "cd34") == "cursor-zhangsan-cd34"
    assert S.retired_alias("cursor-zhangsan-ab12", "20261104T120000Z") == \
        "cursor-zhangsan-retired-20261104T120000Z"
    # 退役名还在 LiteLLM 的严格 alias 形状里(以后把那个开关打开也不会挂)
    assert S.ALIAS_OK_RE.match(S.retired_alias("cursor-zhangsan-ab12", "20261104T120000Z"))


def test_pick_probe_model_never_guesses_by_prefix():
    """🔴 探针模型只能从**这把 key 自己的 models** 里取:两个 key 族前缀是反的,
    按 alias 猜会拿到 403 —— 那是量具错,不是 key 坏(换发脚本 10-31 踩过)。"""
    assert S.pick_probe_model(["gpt-5.5", "claude-haiku-4-5"]) == "claude-haiku-4-5"
    assert S.pick_probe_model(["gpt-5.5", "anthropic.claude-haiku-4-5"]) == "anthropic.claude-haiku-4-5"
    assert S.pick_probe_model(["gpt-5.5", "kimi-k3"]) == "gpt-5.5"          # 没有偏好的就挑第一个
    assert S.pick_probe_model(["gpt-5.5", "deepseek-v4.1-flash"]) == "deepseek-v4.1-flash"
    # all-team-models 是一个词,只对 team 成员解析,master key 直接打它 400 ⇒ 探不出来,跳过
    assert S.pick_probe_model(["all-team-models"]) is None
    assert S.pick_probe_model(["all-proxy-models"]) is None
    assert S.pick_probe_model([]) is None and S.pick_probe_model(None) is None
    assert S.pick_probe_model([None, 1, ""]) is None


def test_rotate_payload_carries_and_drops_the_right_fields():
    old = {"key_alias": "cursor-zhangsan-ab12", "token": "deadbeef", "key_name": "sk-...0001",
           "models": ["gpt-5.5"], "max_budget": 20.0, "budget_duration": "30d", "team_id": "T",
           "user_id": "cursor-zhangsan", "metadata": {"lark_open_id": "ou_x", "base_alias": "cursor-zhangsan"},
           "spend": 9.5, "model_spend": {"gpt-5.5": 9.5}, "blocked": True, "aliases": {},
           "created_at": "2026-01-01", "updated_at": "2026-02-02", "budget_reset_at": "2026-03-03",
           "rotation_count": 3, "auto_rotate": True, "tpm_limit": None}
    p = S.rotate_payload(old, "cursor-zhangsan-cd34", "2026-11-04T12:00:00+0800")
    assert p["key_alias"] == "cursor-zhangsan-cd34"
    for k in ("models", "max_budget", "budget_duration", "team_id", "user_id"):
        assert p[k] == old[k], k
    for k in ("token", "key_name", "spend", "model_spend", "blocked", "created_at", "updated_at",
              "budget_reset_at", "rotation_count", "auto_rotate"):
        assert k not in p, k
    assert "aliases" not in p and "tpm_limit" not in p, "空字段不发(别被当成显式清空)"
    assert p["metadata"]["lark_open_id"] == "ou_x", "身份 metadata 整份跟过来"
    assert p["metadata"]["base_alias"] == "cursor-zhangsan"
    assert p["metadata"]["rotated_from"] == "cursor-zhangsan-ab12"
    assert p["metadata"]["rotated_at"] == "2026-11-04T12:00:00+0800"
    assert old["metadata"] == {"lark_open_id": "ou_x", "base_alias": "cursor-zhangsan"}, "不改入参"


def test_carry_mismatch_only_reports_real_drift():
    old = {"models": ["a"], "max_budget": 20.0, "budget_duration": "30d", "team_id": "T", "user_id": "u"}
    assert S.carry_mismatch(old, dict(old)) == []
    assert S.carry_mismatch(old, dict(old, max_budget=None)) == ["max_budget"]
    assert S.carry_mismatch(old, dict(old, models=["a", "b"])) == ["models"]
    assert S.carry_mismatch(old, dict(old, team_id="OTHER")) == ["team_id"]
    # 老的为空、新的被服务端补上 ⇒ 不算漏平移(key_type 恒 default、organization_id 按 team 补齐)
    assert S.carry_mismatch(dict(old, team_id=None), dict(old, team_id="T")) == []
    assert S.carry_mismatch({}, {}) == []


def test_litellm_disabled_without_master_key(fake, tmp_path):
    cfg = make_cfg(fake, tmp_path)
    assert S.LiteLLMClient(cfg).enabled is False
    assert S.LiteLLMClient(dict(cfg, LITELLM_MASTER_KEY=MASTER)).enabled is True
    assert S.LiteLLMClient(dict(cfg, LITELLM_MASTER_KEY="   ")).enabled is False


def test_litellm_config_checks(fake, tmp_path):
    def errs(**over):
        return S.check_config(make_cfg(fake, tmp_path, **over))
    assert errs() == []
    assert any("LITELLM_MASTER_KEY" in e for e in errs(LITELLM_MASTER_KEY=SECRET)), "不能和会话密钥同值"
    assert any("ROTATE_DRY_RUN=0" in e for e in errs(ROTATE_DRY_RUN="0")), "真写却没配网关 key ⇒ 拒绝启动"
    assert errs(ROTATE_DRY_RUN="0", LITELLM_MASTER_KEY=MASTER) == []          # POSITIVE
    assert any("https" in e for e in errs(LITELLM_BASE="http://cc.auto-link.com.cn/pro")), "master key 不走明文"
    assert errs(LITELLM_BASE="http://127.0.0.1:4000") == [], "回环放过(给测试/198 本机)"
    for bad in ("", "x", "-1", "1.5"):
        assert any("ROTATE_COOLDOWN" in e for e in errs(ROTATE_COOLDOWN=bad)), bad


def test_rotate_field_allowlist_never_touches_identity():
    """重置不改归属:姓名 / 账户类型 / 组织名称 / open_id / 邮箱前缀 一律不在白名单里。"""
    for f in ("姓名", "账户类型", "组织名称", "open_id", "邮箱前缀", "是否在职", "部门名称", "每日预算"):
        assert f not in S.ROTATE_FIELD_ALLOW, f
    assert S.ROTATE_STATUS in REAL_STATUS_OPTIONS, "状态必须是表里已有的选项"


def test_rotate_does_not_touch_identity_columns(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    before = {k: json.dumps(v, ensure_ascii=False, sort_keys=True)
              for k, v in FAKE.tables[KEYTBL]["recA1"].items()}
    do_rotate(p, sa)
    after = FAKE.tables[KEYTBL]["recA1"]
    changed = {k for k in after if before.get(k) != json.dumps(after[k], ensure_ascii=False, sort_keys=True)}
    assert changed <= S.ROTATE_FIELD_ALLOW, changed
    assert changed == {"API Key", "状态", "key_alias", "点我体检", "Cursor Key"}, changed


def test_rotate_leaves_other_rows_alone(rotate_portal):
    p = rotate_portal()
    sa = p.sid("A")
    before = {rid: json.dumps(f, ensure_ascii=False, sort_keys=True)
              for rid, f in FAKE.tables[KEYTBL].items() if rid != "recA1"}
    do_rotate(p, sa)
    for rid, snap in before.items():
        assert json.dumps(FAKE.tables[KEYTBL][rid], ensure_ascii=False, sort_keys=True) == snap, rid


def test_rotate_refuses_blocked_key_no_self_unblock(rotate_portal):
    """🔴 被封的 key 绝不能自助重置 —— 否则就是**自助解封**:新 key 照抄字段但不抄 blocked。
    真实数据(11-04 量):网关上已封的 key 有 15 把在飞书表里状态**仍是「有效」**
    (异常账户处置只在网关上封 key、不动表)⇒ 门户只看表就会把它挑成 current,只看表拦不住。"""
    p = rotate_portal()
    sa = p.sid("A")
    LITE.keys[S.sha(KEY_A1)]["blocked"] = True                 # 网关上封了,表里照旧「有效」
    n_before = len(LITE.keys)
    s, d, _, _ = do_rotate(p, sa)
    assert s == 403 and "停用" in d.get("message", ""), (s, d)
    assert len(LITE.keys) == n_before, "被拒时一把新 key 都不能生成"
    assert row_key() == KEY_A1 and LITE.keys[S.sha(KEY_A1)]["blocked"] is True
    # 阳性对照:同一个人解封后就能重置 ⇒ 上面的 403 是因为 blocked,不是别的原因
    LITE.keys[S.sha(KEY_A1)]["blocked"] = False
    s, d, _, _ = do_rotate(p, sa)
    assert s == 200 and d["key"] != KEY_A1, (s, d)


def test_external_apply_goes_to_review_not_auto_issue(portal):
    """收紧开关 APPLY_ALLOW_EXTERNAL=0 时:外部人点申请只登记待审核、**不写 key 表**(写了 poller 就会发)。
    默认是 1(用户 11-04 定:外部人登录飞书后就能申请)⇒ 这条测的是「需要收紧时这个开关真管用」。"""
    portal.app.cfg["APPLY_DRY_RUN"] = "0"
    portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "0"
    try:
        sx = portal.sid("X")
        for r in list(FAKE.tables[KEYTBL]):
            if r == "recX1": del FAKE.tables[KEYTBL][r]            # 让 X 名下没有 key
        n = len(FAKE.tables[KEYTBL])
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)[:2]
        assert s == 200 and d.get("review") is True and "审核" in d.get("message", ""), d
        assert len(FAKE.tables[KEYTBL]) == n, "外部人申请不能写 key 表"
        # 阳性对照:同一个开关打开 ⇒ 外部人申请真写表
        portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "1"
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)[:2]
        assert s == 200 and not d.get("review") and len(FAKE.tables[KEYTBL]) == n + 1, d
    finally:
        portal.app.cfg["APPLY_DRY_RUN"] = "1"; portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "1"


def test_internal_apply_unaffected_by_external_gate(portal):
    portal.app.cfg["APPLY_DRY_RUN"] = "0"
    try:
        sn = portal.sid("N")                                       # 内部新人,表里没有行
        n = len(FAKE.tables[KEYTBL])
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[:2]
        assert s == 200 and not d.get("review") and len(FAKE.tables[KEYTBL]) == n + 1, d
    finally:
        portal.app.cfg["APPLY_DRY_RUN"] = "1"


def test_external_can_apply_by_default_but_must_be_logged_in(portal):
    """用户 11-04 定:外部人登录飞书后就能申请。默认开;门槛是**必须登录**。"""
    assert portal.app.cfg["APPLY_ALLOW_EXTERNAL"] == "1"
    portal.app.cfg["APPLY_DRY_RUN"] = "0"
    try:
        for r in list(FAKE.tables[KEYTBL]):
            if r == "recX1": del FAKE.tables[KEYTBL][r]
        n = len(FAKE.tables[KEYTBL])
        # 没登录 ⇒ 401,什么都不写
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"})[:2]
        assert s == 401 and len(FAKE.tables[KEYTBL]) == n, (s, d)
        # 登录后 ⇒ 真写表(poller 接着发 key)
        sx = portal.sid("X")
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)[:2]
        assert s == 200 and not d.get("review") and len(FAKE.tables[KEYTBL]) == n + 1, d
    finally:
        portal.app.cfg["APPLY_DRY_RUN"] = "1"


# ════════════════════════════════════════════════════════════════════════
# 17. 11-04 放开后加固(P1-3 redeem 审计 / P2-2 CSP / P2-5 方法与错误页 / P2-6 会话 / P1-1 my_rows 缓存)
# ════════════════════════════════════════════════════════════════════════
def redeem_audit_rows(p):
    rows, _ = p.app.q("select union_id, tool, record_id, key_sha, key_masked, code_id, ip, ua, result, at"
                      " from redeem_audit order by id")
    return rows


def test_redeem_audit_records_who_what_where_without_plaintext(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "all"}, cookie=sa)[1]["code"]
    s, d, _, _ = portal.req("POST", "/ai/api/install-code/redeem", {"code": code}, origin=None,
                            headers={"X-Real-IP": "203.0.113.7", "User-Agent": "curl/8.7.1"})
    assert s == 200 and d["key"] == KEY_A1
    rows = redeem_audit_rows(portal)
    assert len(rows) == 1
    uid, tool, rid, ksha, kmask, code_id, ip, ua, result, at = rows[0]
    assert (uid, tool, rid, ip, ua, result) == (USERS["A"]["union_id"], "all", "recA1", "203.0.113.7", "curl/8.7.1", "ok")
    assert ksha == S.sha(KEY_A1) and kmask == S.mask_key(KEY_A1) and at == portal.clock()
    # code_id 能 join 回签发记录(谁签的、什么时候)
    signed = portal.app.q("select union_id, tool from install_codes where substr(code_hash,1,16)=?", (code_id,), one=True)
    assert signed == (USERS["A"]["union_id"], "all")
    # 🔴 明文 key 和安装码都不落库(整库字节里搜)
    db = Path(portal.app.cfg["DB_PATH"]).read_bytes()
    raw = code.replace("-", "")
    assert KEY_A1.encode() not in db and raw.encode() not in db and code.encode() not in db


def test_redeem_audit_answers_where_did_this_key_go(portal):
    """出事时的那条查询:给一把泄露的 key(手上只有明文或它的 sha256)⇒ 查出装到了哪些 IP。"""
    sa = portal.sid("A")
    for ip in ("198.51.100.1", "198.51.100.2"):
        c = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
        assert _redeem(portal, c, ip=ip)[0] == 200
    sb = portal.sid("B")
    c = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sb)[1]["code"]
    assert _redeem(portal, c, ip="198.51.100.9")[0] == 200
    got, _ = portal.app.q("select ip from redeem_audit where key_sha=? and result='ok' order by at", (S.sha(KEY_A1),))
    assert [r[0] for r in got] == ["198.51.100.1", "198.51.100.2"]


def test_redeem_audit_records_failures_but_not_rate_limited(portal):
    sa = portal.sid("A")
    good = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    _redeem(portal, "nope", ip="10.8.0.1")
    _redeem(portal, "Z" * 32, ip="10.8.0.1")
    assert _redeem(portal, good, ip="10.8.0.2")[0] == 200
    assert _redeem(portal, good, ip="10.8.0.3")[0] == 400           # 用过的码再换
    res = [(r[8], r[6], r[0]) for r in redeem_audit_rows(portal)]
    assert res == [("denied:bad-format", "10.8.0.1", None), ("denied:invalid", "10.8.0.1", None),
                   ("ok", "10.8.0.2", USERS["A"]["union_id"]), ("denied:invalid", "10.8.0.3", None)]
    # 被限流的那些不进审计表(撞库时它最多,只进日志)
    n = len(redeem_audit_rows(portal))
    for _ in range(12):
        _redeem(portal, "Z" * 32, ip="10.8.0.9")
    assert len(redeem_audit_rows(portal)) == n + 10


def test_redeem_audit_nokey_and_feishu_error(portal):
    sa = portal.sid("A")
    code = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    FAKE.search_error = 91403
    try:
        assert _redeem(portal, code, ip="10.8.1.1")[0] == 502
    finally:
        FAKE.search_error = None
    FAKE.tables[KEYTBL]["recA1"]["状态"] = "已禁用"
    for k in [k for k, v in FAKE.tables[KEYTBL].items() if v["状态"].startswith("有效") or v["状态"] == "已完成"]:
        FAKE.tables[KEYTBL][k]["状态"] = "已禁用"
    assert _redeem(portal, code, ip="10.8.1.1")[0] == 404
    res = [r[8] for r in redeem_audit_rows(portal)]
    assert res == ["error:feishu-91403", "denied:nokey"], res
    assert all(r[3] is None for r in redeem_audit_rows(portal)), "没交出 key 的行不记 key_sha"


def test_redeem_audit_unguarded(portal, monkeypatch):
    """反向对照:把审计关掉 ⇒ 上面那条「这把 key 去了哪」的查询查不到任何东西。"""
    monkeypatch.setattr(portal.app, "audit_redeem", lambda *a, **k: None)
    sa = portal.sid("A")
    c = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(portal, c, ip="198.51.100.1")[0] == 200
    assert portal.app.q("select count(*) from redeem_audit where key_sha=?", (S.sha(KEY_A1),), one=True)[0] == 0


def test_redeem_audit_ua_is_masked_and_bounded(portal):
    sa = portal.sid("A")
    c = portal.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    portal.req("POST", "/ai/api/install-code/redeem", {"code": c}, origin=None,
               headers={"X-Real-IP": "10.9.9.9", "User-Agent": "x " + KEY_B + " " + "y" * 500})
    ua = redeem_audit_rows(portal)[0][7]
    assert KEY_B not in ua and len(ua) <= 200


# ── P2-2 CSP ──────────────────────────────────────────────────────────────
REAL_INDEX = HERE / "static" / "index.html"


def real_portal(fake, tmp_path, clock):
    FAKE.reset()
    FAKE.tables[KEYTBL] = seed()
    cfg = make_cfg(fake, tmp_path)
    cfg["STATIC_DIR"] = str(HERE / "static")
    return Portal(fake, cfg, clock)


def headers_of(p, method, path):
    c = http.client.HTTPConnection("127.0.0.1", p.port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    b = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, b


def parse_csp(v):
    out = {}
    for part in v.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


def test_page_csp_is_strict_and_matches_inline_blocks(fake, tmp_path, clock):
    import base64, hashlib, re
    p = real_portal(fake, tmp_path, clock)
    try:
        for path in ("/ai/", "/ai/auth/magic?t=" + "a" * 43):
            s, h, b = headers_of(p, "GET", path)
            assert s == 200
            csp = parse_csp(h["content-security-policy"])
            for d, want in (("default-src", ["'none'"]), ("frame-ancestors", ["'none'"]), ("base-uri", ["'none'"]),
                            ("form-action", ["'self'"]), ("connect-src", ["'self'"])):
                assert csp[d] == want, (d, csp.get(d))
            for d in ("script-src", "style-src"):
                assert csp[d] and all(x.startswith("'sha256-") for x in csp[d]), (d, csp[d])
                assert "'unsafe-inline'" not in csp[d] and "'unsafe-eval'" not in csp[d]
            assert "font-src" not in csp, "页面不该再加载任何字体"
            # 每一个内联块的 hash 都在里面(浏览器按标签之间的原始字节算)
            for tag, d in (("script", "script-src"), ("style", "style-src")):
                blocks = re.findall(rb"<%s>(.*?)</%s>" % (tag.encode(), tag.encode()), b, re.S)
                assert blocks
                want = {"'sha256-%s'" % base64.b64encode(hashlib.sha256(x).digest()).decode() for x in blocks}
                assert want == set(csp[d]), tag
            assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
            assert "camera=()" in h["permissions-policy"] and "clipboard-write=(self)" in h["permissions-policy"]
            assert h["referrer-policy"] in ("same-origin", "no-referrer")
            assert h["cross-origin-opener-policy"] == "same-origin"
    finally:
        p.close()


def test_page_csp_hash_tracks_file_content(portal):
    """反向对照:页面内容一变,hash 跟着变(serve 时现算)⇒ 改了 JS 不会因为 hash 过期把整页打死。
    同时证明 CSP 真的「只放这一段」:别的脚本内容算出来的 hash 不在里面。"""
    import base64, hashlib
    idx = Path(portal.app.cfg["STATIC_DIR"]) / "index.html"
    idx.write_text("<!doctype html><script>var a=1</script><style>p{}</style>", encoding="utf-8")
    csp1 = parse_csp(headers_of(portal, "GET", "/ai/")[1]["content-security-policy"])
    idx.write_text("<!doctype html><script>var a=2</script><style>p{}</style>", encoding="utf-8")
    csp2 = parse_csp(headers_of(portal, "GET", "/ai/")[1]["content-security-policy"])
    h = lambda s: "'sha256-%s'" % base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()
    assert csp1["script-src"] == [h("var a=1")] and csp2["script-src"] == [h("var a=2")]
    assert h("alert(document.cookie)") not in csp2["script-src"]
    assert csp1["style-src"] == csp2["style-src"] == [h("p{}")]


def test_non_page_responses_have_lockdown_csp(portal):
    for m, path in (("GET", "/ai/api/me"), ("GET", "/ai/healthz"), ("GET", "/ai/nope"), ("GET", "/ai")):
        h = headers_of(portal, m, path)[1]
        csp = parse_csp(h["content-security-policy"])
        assert csp["default-src"] == ["'none'"] and csp["frame-ancestors"] == ["'none'"], path


def test_index_has_no_external_resources_or_inline_attrs():
    """CSP 用 hash 放行的只有 <script>/<style> 块:内联属性(style="" / onclick="")、外链脚本/样式/字体
    都会被拦 ⇒ 这里钉住页面里没有它们(否则浏览器里功能静默坏掉)。"""
    import re
    t = REAL_INDEX.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in t and "fonts.gstatic.com" not in t
    assert not re.search(r"<(link|script|img|iframe)\b[^>]*\b(href|src)=\"https?://", t), "不许外链资源"
    assert not re.search(r"<link\b", t), "不许 <link>(样式表 / preconnect 都算外链)"
    markup = re.sub(r"<script>.*?</script>|<style>.*?</style>", "", t, flags=re.S)   # 只看标签,不看 JS 字符串
    assert not re.search(r"<[a-z][^>]*\sstyle=\"", markup), "内联 style 属性会被 CSP 拦"
    assert not re.search(r"<[a-z][^>]*\son[a-z]+=", markup), "内联事件属性会被 CSP 拦"
    assert "<style>" in t and "<script>" in t      # POSITIVE:剥块的正则确实剥到了东西
    assert t.count("<script>") == t.count("</script>") and "<script " not in t
    assert t.count("<style>") == t.count("</style>") and "<style " not in t


# ── P2-5 方法 / 错误页 ────────────────────────────────────────────────────
def raw_http(p, data):
    s = socket.create_connection(("127.0.0.1", p.port), 5)
    s.sendall(data)
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return buf.decode("utf-8", "replace")


def test_head_is_get_without_body_only_on_safe_paths(portal):
    s, h, b = headers_of(portal, "HEAD", "/ai/")
    assert s == 200 and b == b"" and int(h["content-length"]) > 0 and "content-security-policy" in h
    s, h, b = headers_of(portal, "HEAD", "/ai/healthz")
    assert s == 200 and b == b""
    # 有副作用 / 要会话的路径不给 HEAD
    for path in ("/ai/auth/login", "/ai/auth/logout", "/ai/api/me", "/ai/api/keys"):
        s, h, b = headers_of(portal, "HEAD", path)
        assert s == 405 and b == b"" and "POST" in h["allow"], path
    # HEAD /auth/login 不该建 state 行
    assert portal.app.q("select count(*) from oauth_states", one=True)[0] == 0


def test_head_logout_does_not_kill_session(portal):
    sid = portal.sid("A")
    c = http.client.HTTPConnection("127.0.0.1", portal.port, timeout=5)
    c.request("HEAD", "/ai/auth/logout", headers={"Cookie": sid})
    c.getresponse().read()
    c.close()
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200


def test_options_has_no_cors(portal):
    c = http.client.HTTPConnection("127.0.0.1", portal.port, timeout=5)
    c.request("OPTIONS", "/ai/api/keys/reveal", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
    r = c.getresponse()
    r.read()
    hs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    assert r.status == 204 and "POST" in hs["allow"]
    assert not [k for k in hs if k.startswith("access-control-")]


@pytest.mark.parametrize("line", [b"PUT /ai/api/keys HTTP/1.1", b"DELETE /ai/api/keys HTTP/1.1",
                                  b"<script>alert(1)</script> /ai/ HTTP/1.1",
                                  b'GET /ai/"><img src=x onerror=alert(1)> HTTP/1.1', b"GARBAGE",
                                  b"GET /ai/ HTTP/9.9"])
def test_bad_requests_get_uniform_json_no_python_banner(portal, line):
    r = raw_http(portal, line + b"\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
    head, _, body = r.partition("\r\n\r\n")
    assert "Python" not in head and "BaseHTTP" not in head, head
    assert "Server: ai-portal" in head
    j = json.loads(body)
    assert j["message"] and "<" not in body and "alert" not in body, body
    assert "content-security-policy" in head.lower()


def test_unknown_method_is_405_with_allow(portal):
    r = raw_http(portal, b"PATCH /ai/api/keys HTTP/1.1\r\nHost: x\r\n\r\n")
    assert r.startswith("HTTP/1.1 405") or r.startswith("HTTP/1.0 405"), r[:40]
    assert "Allow: GET, HEAD, POST, OPTIONS" in r


def test_server_header_has_no_version(portal):
    h = headers_of(portal, "GET", "/ai/healthz")[1]
    assert h["server"] == "ai-portal"


# ── P2-6 会话 TTL / 踢人 ──────────────────────────────────────────────────
def test_session_ttl_is_4h(portal, clock):
    assert S.DEFAULTS["SESSION_TTL"] == "14400"
    st, loc, sid, _ = portal.login("A")
    _, _, _, sc = portal.req("GET", "/ai/auth/login")       # 只为拿一份 cookie 串看属性
    clock.t += 4 * 3600 - 5
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 200    # POSITIVE:4h 内能用
    clock.t += 10
    assert portal.req("GET", "/ai/api/me", cookie=sid)[0] == 401


def test_session_cookie_max_age_matches_ttl(portal):
    _, _, loc, _ = portal.req("GET", "/ai/auth/login")
    st, loc, sid, _ = portal.login("A")
    # login() 吃掉了 Set-Cookie;重新走一遍拿原文
    path = "/ai/auth/login"
    s, _, loc, sc = portal.req("GET", path)
    stc = cookie_of(sc, "ai_oauth")
    _, _, loc2, _ = fake_get(loc + "&as=A")
    _, _, _, sc3 = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + stc)
    c = [x for x in sc3 if x.startswith("ai_sid=")][0]
    assert "Max-Age=14400" in c


def test_kill_sessions_kicks_every_device_and_pending_codes(mportal):
    p = mportal
    s1, s2 = p.sid("A"), p.sid("A")
    s3 = magic_sid(p, "A")                                   # 第三台:一次性登录链接
    sb = p.sid("B")
    code = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=s1)[1]["code"]
    pending_magic = tok_of(issue(p, "A")[1]["url"])          # 还没点的登录链接
    r = p.app.kill_user(USERS["A"]["union_id"])
    assert r == {"sessions": 3, "install_codes": 1, "magic_tokens": 1}
    for s in (s1, s2, s3):
        assert p.req("GET", "/ai/api/me", cookie=s)[0] == 401
    assert _redeem(p, code, ip="10.6.6.6")[0] == 400, "踢人之前签的安装码不能再换出明文"
    assert consume(p, pending_magic, ip="10.6.6.7")[0] == 400, "踢人之前签的登录链接不能再用"
    # POSITIVE:别人不受影响
    assert p.req("GET", "/ai/api/me", cookie=sb)[0] == 200
    # 踢完他还能重新登录(踢人 ≠ 封号;封号要在表/网关上停 key)
    assert p.req("GET", "/ai/api/me", cookie=p.sid("A"))[0] == 200


def test_kill_sessions_unguarded_logout_only_kills_one(portal):
    """反向对照:没有踢人入口时,只能退出当前这一条,其他设备上的 cookie 照样能用(红队 4.3)。"""
    s1, s2 = portal.sid("A"), portal.sid("A")
    portal.req("POST", "/ai/auth/logout", {}, cookie=s1)
    assert portal.req("GET", "/ai/api/me", cookie=s2)[0] == 200


def test_kill_sessions_rejects_bad_id(portal):
    for bad in ("", "on_", "%", "on_a' or 1=1 --", None):
        with pytest.raises(ValueError):
            portal.app.kill_user(bad)


def test_kill_sessions_cli(fake, tmp_path, clock):
    """真进程跑 `server.py --env … --kill-sessions on_xxx`:踢完退出,不起服务;输出不带完整 union_id。"""
    FAKE.reset()
    FAKE.tables[KEYTBL] = seed()
    cfg = make_cfg(fake, tmp_path)
    p = Portal(fake, cfg, clock)
    try:
        sid, sb = p.sid("A"), p.sid("B")
        env = tmp_path / "k.env"
        env.write_text("\n".join("%s=%s" % (k, cfg[k]) for k in (
            "FEISHU_APP_ID", "FEISHU_APP_SECRET", "SESSION_SECRET", "DB_PATH", "FEISHU_OPEN_BASE")), encoding="utf-8")
        r = subprocess.run([sys.executable, str(HERE / "server.py"), "--env", str(env),
                            "--kill-sessions", USERS["A"]["union_id"]],
                           capture_output=True, text=True, timeout=30,
                           env={k: v for k, v in os.environ.items() if k not in S.DEFAULTS and k not in S.REQUIRED})
        assert r.returncode == 0, r.stderr
        assert "已踢" in r.stdout and "会话 1 条" in r.stdout and USERS["A"]["union_id"] not in r.stdout, r.stdout
        assert p.req("GET", "/ai/api/me", cookie=sid)[0] == 401
        assert p.req("GET", "/ai/api/me", cookie=sb)[0] == 200
        bad = subprocess.run([sys.executable, str(HERE / "server.py"), "--env", str(env), "--kill-sessions", "nope"],
                             capture_output=True, text=True, timeout=30,
                             env={k: v for k, v in os.environ.items() if k not in S.DEFAULTS and k not in S.REQUIRED})
        assert bad.returncode == 1
    finally:
        p.close()


# ── P1-1 补充:my_rows 缓存 + 写操作失效 ──────────────────────────────────
def searches():
    return len([c for c in FAKE.calls if c[1].endswith("/records/search")])


def test_my_rows_cache_cuts_feishu_calls(portal_factory, clock):
    p = portal_factory(MY_ROWS_TTL="15")
    sa = p.sid("A")
    FAKE.calls.clear()
    assert p.req("GET", "/ai/api/keys", cookie=sa)[0] == 200
    one = searches()                        # 一次真查 = 假飞书分页的页数
    assert one >= 1
    for _ in range(4):
        assert p.req("GET", "/ai/api/keys", cookie=sa)[0] == 200
    assert searches() == one, "TTL 内的 4 次全部吃缓存"
    clock.t += 16
    p.req("GET", "/ai/api/keys", cookie=sa)
    assert searches() == 2 * one, "过了 TTL 要重新查"
    # 按人隔离:B 的请求不吃 A 的缓存,也拿不到 A 的行
    n = searches()
    kb = p.req("GET", "/ai/api/keys", cookie=p.sid("B"))[1]["keys"]
    assert searches() > n and [k["record_id"] for k in kb] == ["recB1"]


def test_my_rows_cache_unguarded_ttl0(portal_factory):
    """反向对照:TTL=0 ⇒ 每次都打飞书(证明上一条的 1 次来自缓存)。"""
    p = portal_factory(MY_ROWS_TTL="0")
    sa = p.sid("A")
    FAKE.calls.clear()
    p.req("GET", "/ai/api/keys", cookie=sa)
    one = searches()
    for _ in range(4):
        p.req("GET", "/ai/api/keys", cookie=sa)
    assert searches() == 5 * one


def test_rotate_invalidates_cache_so_page_shows_new_key(rotate_portal):
    p = rotate_portal(MY_ROWS_TTL="30")
    sa = p.sid("A")
    before = [k for k in p.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]][0]
    assert before["masked"] == S.mask_key(KEY_A1)
    s, d = do_rotate(p, sa)[:2]
    assert s == 200 and d["key"] != KEY_A1
    after = [k for k in p.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]][0]
    assert after["masked"] == S.mask_key(d["key"]), "重置后页面必须马上看到新 key(缓存已失效)"
    # 签码 → 换出来的也是新 key
    c = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    assert _redeem(p, c, ip="10.4.4.5")[1]["key"] == d["key"]


def test_rotate_cache_unguarded_shows_stale_key(rotate_portal, monkeypatch):
    """反向对照:不失效缓存 ⇒ 重置后页面还显示旧 key。证明上一条测得出缺失效的 bug。"""
    p = rotate_portal(MY_ROWS_TTL="30")
    monkeypatch.setattr(p.app, "drop_rows_cache", lambda uid: None)
    sa = p.sid("A")
    p.req("GET", "/ai/api/keys", cookie=sa)
    d = do_rotate(p, sa)[1]
    after = [k for k in p.req("GET", "/ai/api/keys", cookie=sa)[1]["keys"] if k["current"]][0]
    assert after["masked"] == S.mask_key(KEY_A1) != S.mask_key(d["key"])


def test_reveal_and_redeem_bypass_cache_after_admin_disables(portal_factory):
    """管理员在表里停用 key:列表最多 TTL 秒后跟上,但**交明文**的两条路(reveal / redeem)立刻拒。"""
    p = portal_factory(MY_ROWS_TTL="60")
    sa = p.sid("A")
    p.req("GET", "/ai/api/keys", cookie=sa)                          # 填缓存
    code = p.req("POST", "/ai/api/install-code", {"tool": "cursor"}, cookie=sa)[1]["code"]
    FAKE.tables[KEYTBL]["recA1"]["状态"] = "已禁用"
    s, _ = p.req("POST", "/ai/api/keys/reveal", {"record_id": "recA1"}, cookie=sa)[:2]
    assert s == 403
    s, d = _redeem(p, code, ip="10.4.4.6")
    assert s == 200 and d["key"] != KEY_A1, "停用后 redeem 换出来的不能是那把被停的"


def test_apply_invalidates_cache(portal_factory):
    p = portal_factory(MY_ROWS_TTL="60", APPLY_DRY_RUN="0")
    sn = p.sid("N")
    assert p.req("GET", "/ai/api/keys", cookie=sn)[1]["keys"] == []
    assert p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sn)[0] == 200
    ks = p.req("GET", "/ai/api/keys", cookie=sn)[1]["keys"]
    assert len(ks) == 1 and ks[0]["record_id"].startswith("recNEW"), "申请后页面要马上看到「生成中」那一行"


def test_cache_returns_copies(portal_factory):
    """缓存里的行不能被调用方改脏(keys() 会往外打码,rotate 会读 extra_keys)。"""
    p = portal_factory(MY_ROWS_TTL="60")
    u = {"union_id": USERS["A"]["union_id"]}
    r1 = p.app.my_rows(u)
    r1[0]["key"] = "tampered"
    assert p.app.my_rows(u)[0]["key"] != "tampered"


# ════════════════════════════════════════════════════════════════════════
# 外部人旅程(11-04 外部用户体验走查,.work/ai-portal/external/)
# ════════════════════════════════════════════════════════════════════════
def _fb_rows():
    return list(FAKE.tables.get(FBTBL, {}).values())


def test_external_review_request_actually_lands_in_feedback_table(portal):
    """🔴 原来写 type="其他"(不在「类型」选项里)⇒ build_fields 抛错被吞 ⇒ 待审核申请一条都进不了反馈表。"""
    portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "0"
    try:
        sx = portal.sid("X")
        s, d = portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)[:2]
        assert s == 200 and d.get("review") is True, d
        rows = [f for f in _fb_rows() if "申请 key" in f.get("一句话说明", "")]
        assert len(rows) == 1, _fb_rows()
        f = rows[0]
        assert f["类型"] in S.fb.CHOICES["类型"] and f["工具"] in S.fb.CHOICES["工具"]
        # 管理员拿到这一行就能处理:有 union_id、有处理步骤
        assert USERS["X"]["union_id"] in f["详细描述"] and "key 表新建一行" in f["详细描述"]
        # 24 小时内重复点 ⇒ 不刷屏
        portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)
        assert len([f for f in _fb_rows() if "申请 key" in f.get("一句话说明", "")]) == 1
    finally:
        portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "1"


def test_review_request_type_is_a_real_choice_unguarded():
    """放开对照:旧值 "其他" 确实过不了 build_fields(证明上一条测的是那个修复,不是桩宽松)。"""
    with pytest.raises(ValueError):
        S.fb.build_fields(S.App.feedback_form({"type": "其他", "tool": "其他", "title": "x"}), "")
    S.fb.build_fields(S.App.feedback_form({"type": "其他建议", "tool": "其他", "title": "x"}), "")


def test_me_reports_pending_review_so_reload_shows_it(portal):
    portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "0"
    try:
        sx = portal.sid("X")
        assert portal.req("GET", "/ai/api/me", cookie=sx)[1]["apply_review_at"] == 0
        portal.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sx)
        me = portal.req("GET", "/ai/api/me", cookie=sx)[1]
        assert me["apply_review_at"] > 0 and me["apply_external_review"] is True
        # 内部人永远是 0(他们不走审核)
        assert portal.req("GET", "/ai/api/me", cookie=portal.sid("A"))[1]["apply_review_at"] == 0
        assert [r[0] for r in portal.app.list_apply_requests()] == [USERS["X"]["union_id"]]
    finally:
        portal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "1"


def test_external_feedback_tag_identifies_the_person(portal):
    """机器人签链接不传名字 ⇒ 外部人 name 常为空;人员格还可能写不进 ⇒ 描述里必须带 union_id。"""
    FAKE.fail_create_person = True
    sx = portal.sid("X")
    s, d = portal.req("POST", "/ai/api/feedback", {"title": "被公司电脑拦了", "类型": "用不了/报错", "工具": "Cursor"},
                      cookie=sx)[:2]
    assert s == 200, d
    f = _fb_rows()[-1]
    assert "提交人" not in f and USERS["X"]["union_id"] in f["详细描述"] and "(外部)" in f["详细描述"]
    # 内部人的描述里不加 id(他们有人员格 + 邮箱)
    FAKE.fail_create_person = False
    portal.req("POST", "/ai/api/feedback", {"title": "t", "类型": "其他建议", "工具": "其他"}, cookie=portal.sid("A"))
    assert USERS["A"]["union_id"] not in _fb_rows()[-1]["详细描述"]


def test_oauth_20010_sends_external_user_back_to_bot_path(portal, monkeypatch):
    """外部公司点了内部登录按钮 ⇒ 飞书 20010 ⇒ 回首页 login_error=external,而不是一页「飞书登录失败,请重试」。"""
    def boom(*a, **k):
        raise S.FeishuError("oauth/token", 20010, "user has no app permission")
    monkeypatch.setattr(portal.app.client, "exchange_code", boom)
    _, _, loc0, sc0 = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc0 + "&as=X")
    st, _, loc, sc = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc0, "ai_oauth"))
    assert st == 302 and loc == "/ai/?login_error=external" and not cookie_of(sc, "ai_sid"), (st, loc)
    # 对照:别的飞书错误码仍是原来的 502 页
    def boom2(*a, **k):
        raise S.FeishuError("oauth/token", 20003, "invalid_grant")
    monkeypatch.setattr(portal.app.client, "exchange_code", boom2)
    _, _, loc0, sc0 = portal.req("GET", "/ai/auth/login")
    _, _, loc2, _ = fake_get(loc0 + "&as=X")
    st, _, loc, _ = portal.req("GET", loc2[len(ORIGIN):], cookie="ai_oauth=" + cookie_of(sc0, "ai_oauth"))
    assert st == 502 and not loc


def test_frontend_external_copy_has_no_dead_internal_links():
    """外部人能看到的页面上,不能出现外部人打不开的内部资源(飞书表格分享链接、内部群、内部人名)。"""
    h = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    for bad in ("feishu.cn/share/base", "feishu.cn/wiki", "feishu.cn/docx", "符金冬", "刘国现"):
        assert bad not in h, bad
    # 有外部人专属提示,且 login_error=external 有对应文案
    assert 'class="sub ext-only"' in h and "external:" in h and "is-ext" in h
    # CSP 只放行 hash 过的内联块 ⇒ 不许 style= 属性 / on*= 内联事件
    #   (只查标记部分;内联的二维码库 JS 字符串里有 '<table style=',那是它自己的降级分支,不是页面标记)
    import re as _re
    markup = _re.sub(r"<script\b.*?</script>", "", h, flags=_re.S)
    assert not _re.search(r'<[^>]+\sstyle="', markup) and not _re.search(r'<[^>]+\son[a-z]+="', markup)


# ════════════════════════════════════════════════════════════════════════
# 18. 外部人按 tenant_key 认公司(11-04「个人版不让申请」)
#     个人版和企业外部人的事件字段一模一样 ⇒ 唯一判据 = tenant_key 在不在合作公司映射里。
#     每条拒绝都配阳性对照:同一个门户、同一个开关,只换 tenant_key(或只放回映射)⇒ 必须放行。
# ════════════════════════════════════════════════════════════════════════
def _ext_user(p, tenant_key, who="X"):
    """机器人签 magic link 时带上事件里的 tenant_key ⇒ 这就是线上外部人拿到会话的唯一路径。"""
    u = dict(USERS[who])
    body = {"union_id": u["union_id"], "open_id": u["open_id"], "tenant_key": tenant_key, "name": u["name"]}
    t = tok_of(issue(p, who, body=body)[1]["url"])
    s, d, _, sc = consume(p, t)
    assert s == 200, d
    return "ai_sid=" + cookie_of(sc, "ai_sid")


def _clear_x_rows():
    for r in list(FAKE.tables[KEYTBL]):
        if r == "recX1":
            del FAKE.tables[KEYTBL][r]


@pytest.fixture
def oportal(portal_factory):
    p = portal_factory(INTERNAL_SECRET=INTERNAL, APPLY_DRY_RUN="0")
    _clear_x_rows()
    return p


def _apply(p, sid):
    return p.req("POST", "/ai/api/apply", {"account_type": "Cursor"}, cookie=sid)[:2]


def test_known_company_allowed_and_org_written_per_person(oportal):
    """已知企业(北汽,manual 来源)⇒ 放行,组织名称=映射里的 org;不是 APPLY_ORG_NAME 写死的值。"""
    oportal.app.cfg["APPLY_ORG_NAME"] = "无锡车联"          # 内部人的写死值不许漏给外部人
    n = len(FAKE.tables[KEYTBL])
    s, d = _apply(oportal, _ext_user(oportal, BAIC_TENANT))
    assert s == 200 and d["record_id"].startswith("recNEW"), d
    assert len(FAKE.tables[KEYTBL]) == n + 1
    assert FAKE.tables[KEYTBL][d["record_id"]] == {"姓名": [{"id": USERS["X"]["union_id"]}], "账户类型": ["Cursor"],
                                                   "组织名称": "北汽集团"}
    # 另一个外部人在上海车联的租户 ⇒ 写的是上海车联(按人取值,不是全站一个值)
    # (换人而不是同一个人再申请一次:同一人 24h 内有申请锁)
    s, d = _apply(oportal, _ext_user(oportal, "2a0b_other_tenant", who="N"))
    assert s == 200 and FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "上海车联", d


def test_personal_edition_denied_with_positive_control(oportal):
    n = len(FAKE.tables[KEYTBL])
    FAKE.calls.clear()
    s, d = _apply(oportal, _ext_user(oportal, PERSONAL_TENANT))
    assert s == 403 and d["error"] == S.APPLY_UNKNOWN_ORG_MSG, (s, d)
    assert len(FAKE.tables[KEYTBL]) == n and not [c for c in FAKE.calls if c[1].endswith("/records")]
    # 个人号连「待审核登记」也进不去(不刷反馈表)
    oportal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "0"
    s, d = _apply(oportal, _ext_user(oportal, PERSONAL_TENANT))
    assert s == 403 and not oportal.app.list_apply_requests(), (s, d)
    oportal.app.cfg["APPLY_ALLOW_EXTERNAL"] = "1"
    # POSITIVE:同一个人只换 tenant_key 为已知企业 ⇒ 放行
    s, d = _apply(oportal, _ext_user(oportal, BAIC_TENANT))
    assert s == 200 and len(FAKE.tables[KEYTBL]) == n + 1, (s, d)


def test_unknown_tenant_denied_with_positive_control(oportal, tmp_path):
    new_co = "1f00000000new575e"
    s, d = _apply(oportal, _ext_user(oportal, new_co))
    assert s == 403 and d["error"] == S.APPLY_UNKNOWN_ORG_MSG, (s, d)
    # 空 tenant_key(机器人漏传)也是拒
    s, d = _apply(oportal, _ext_user(oportal, ""))
    assert s == 403, (s, d)
    # POSITIVE:把这家新公司加进映射(不重启,热加载)⇒ 同一个会话直接放行
    sid = _ext_user(oportal, new_co)
    m = dict(TENANT_MAP, **{new_co: {"name": "美团", "org": "美团", "source": "manual", "verified_at": "2026-11-04"}})
    path = Path(oportal.app.cfg["TENANT_MAP_PATH"])
    path.write_text(json.dumps(m, ensure_ascii=False) + "\n", encoding="utf-8")   # 尺寸变了 ⇒ 一定重读
    s, d = _apply(oportal, sid)
    assert s == 200 and FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "美团", (s, d)


def test_known_company_without_table_option_denied(oportal):
    """芜湖车联:关联组织认得出,但 key 表「组织名称」没有它的选项 ⇒ 拒(写新值会造脏选项)。"""
    s, d = _apply(oportal, _ext_user(oportal, WUHU_TENANT))
    assert s == 403 and "芜湖车联天下智能科技有限公司" in d["error"] and "还没开通" in d["error"], (s, d)
    # org 写了、但不在 _meta.org_options 快照里 ⇒ 也拒
    m = json.loads(json.dumps(TENANT_MAP))
    m[WUHU_TENANT]["org"] = "芜湖车联"
    path = Path(oportal.app.cfg["TENANT_MAP_PATH"])
    path.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    s, d = _apply(oportal, _ext_user(oportal, WUHU_TENANT))
    assert s == 403, (s, d)
    # POSITIVE:选项快照里加上 ⇒ 放行,写的就是这个选项
    m["_meta"]["org_options"].append("芜湖车联")
    path.write_text(json.dumps(m, ensure_ascii=False) + "  ", encoding="utf-8")
    s, d = _apply(oportal, _ext_user(oportal, WUHU_TENANT))
    assert s == 200 and FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "芜湖车联", (s, d)


@pytest.mark.parametrize("breakage", ["missing", "garbage", "no_meta", "unset"])
def test_map_missing_fails_closed_for_external_only(portal_factory, tmp_path, breakage):
    over = {"TENANT_MAP_PATH": ""} if breakage == "unset" else {}
    p = portal_factory(INTERNAL_SECRET=INTERNAL, APPLY_DRY_RUN="0", **over)
    _clear_x_rows()
    path = Path(p.app.cfg["TENANT_MAP_PATH"]) if breakage != "unset" else None
    if breakage == "missing":
        path.unlink()
    elif breakage == "garbage":
        path.write_text("{not json", encoding="utf-8")
    elif breakage == "no_meta":
        path.write_text(json.dumps({k: v for k, v in TENANT_MAP.items() if k != "_meta"}), encoding="utf-8")
    sx = _ext_user(p, BAIC_TENANT)
    n = len(FAKE.tables[KEYTBL])
    s, d = _apply(p, sx)
    assert s == 403 and len(FAKE.tables[KEYTBL]) == n, (breakage, s, d)
    me = p.req("GET", "/ai/api/me", cookie=sx)[1]
    assert me["org_name"] == "" and me["org_ok"] is False
    # 内部人不受影响(新人 N 照常写表)
    s, d = _apply(p, p.sid("N"))
    assert s == 200 and d["record_id"].startswith("recNEW") and len(FAKE.tables[KEYTBL]) == n + 1, (s, d)
    # POSITIVE:把映射放回来 ⇒ 同一个外部会话放行
    good = tmp_path / "good_map.json"
    good.write_text(json.dumps(TENANT_MAP, ensure_ascii=False), encoding="utf-8")
    p.app.cfg["TENANT_MAP_PATH"] = str(good)
    s, d = _apply(p, sx)
    assert s == 200 and FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "北汽集团", (s, d)


def test_internal_unaffected_by_tenant_map(portal_factory):
    """内部同事(tenant_key=本租户)不读映射:映射里没有本租户、甚至映射整个缺失,照常申请,组织名称按 APPLY_ORG_NAME。"""
    p = portal_factory(APPLY_DRY_RUN="0", APPLY_ORG_NAME="无锡车联", TENANT_MAP_PATH="/nonexistent/tenant_map.json")
    assert OUR_TENANT not in TENANT_MAP
    s, d = _apply(p, p.sid("N"))
    assert s == 200 and FAKE.tables[KEYTBL][d["record_id"]]["组织名称"] == "无锡车联", (s, d)
    me = p.req("GET", "/ai/api/me", cookie=p.sid("A"))[1]
    assert me["external"] is False and me["org_name"] == ""
    # 对照:同一个门户里外部人确实被映射缺失挡住(证明上面放行不是因为闸门没生效)
    sx = _ext_user_factory(p, BAIC_TENANT)
    assert _apply(p, sx)[0] == 403


def _ext_user_factory(p, tenant_key):
    """给没配 INTERNAL_SECRET 的门户造外部会话:直接走 session_shape + new_session(和 magic 消费同一条路)。"""
    u = USERS["X"]
    sid = p.app.new_session(p.app.session_shape({"union_id": u["union_id"], "open_id": u["open_id"],
                                                 "tenant_key": tenant_key, "name": u["name"]}, "magic"))
    return "ai_sid=" + sid


def test_tenant_key_in_map_cannot_be_spoofed_from_client(oportal):
    """公司只认会话里的 tenant_key(机器人签发时从飞书事件取)。请求体里塞 tenant_key / 组织名称 不起作用。"""
    sid = _ext_user(oportal, PERSONAL_TENANT)
    s, d = oportal.req("POST", "/ai/api/apply", {"account_type": "Cursor", "tenant_key": BAIC_TENANT,
                                                  "组织名称": "北汽集团"}, cookie=sid)[:2]
    assert s == 403, (s, d)


def test_me_shows_company_name_for_external(oportal):
    me = oportal.req("GET", "/ai/api/me", cookie=_ext_user(oportal, BAIC_TENANT))[1]
    assert me["external"] is True and me["org_name"] == "北汽集团" and me["org_ok"] is True
    me = oportal.req("GET", "/ai/api/me", cookie=_ext_user(oportal, WUHU_TENANT))[1]
    assert me["org_name"] == "芜湖车联天下智能科技有限公司" and me["org_ok"] is False
    me = oportal.req("GET", "/ai/api/me", cookie=_ext_user(oportal, PERSONAL_TENANT))[1]
    assert me["org_name"] == "" and me["org_ok"] is False


def test_frontend_shows_company_next_to_name():
    h = (HERE / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="who-org"' in h and "me.org_name" in h
    # 走 textContent,不拼 HTML(公司名来自映射文件,也当不可信文本)
    assert '$("who-org").textContent' in h and "who-org\").innerHTML" not in h
