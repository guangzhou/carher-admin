"""feedback_server.py 的离线回归:假 Open WebUI + 假飞书,验身份只认 cookie、写表字段、限流、同源、打码、通知。

跑法:python3 -m pytest scripts/feedback -q
"""
import http.server, importlib.util, json, sqlite3, threading, time, urllib.request
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TOKEN_OK = "tok-good"
OPEN_ID = "ou_test0000000000000000000000000"
CALLS = []          # 假飞书收到的请求
RECORDS = {}        # 假表


class FakeOWUI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") == "Bearer " + TOKEN_OK:
            b = json.dumps({"id": "u1", "name": "张三", "email": "zs@auto-link.com.cn"}).encode()
            self.send_response(200)
        else:
            b = b'{"detail":"Not authenticated"}'
            self.send_response(401)
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    d = tmp_path_factory.mktemp("fb")
    db = d / "webui.db"
    c = sqlite3.connect(db)
    c.execute("create table user(id text, oauth text)")
    c.execute("insert into user values('u1', ?)", (json.dumps({"feishu": {"sub": OPEN_ID}}),))
    c.commit()
    c.close()
    ow = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOWUI)
    threading.Thread(target=ow.serve_forever, daemon=True).start()

    spec = importlib.util.spec_from_file_location("fbs", HERE / "feedback_server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.CFG.update(OWUI_URL="http://127.0.0.1:%d" % ow.server_port, OWUI_DB=str(db),
                 FEISHU_APP_ID="x", FEISHU_APP_SECRET="y")

    def fake_fs(method, path, body=None, **kw):
        CALLS.append((method, path, body))
        if path.startswith("/drive/v1/medias/upload_all"):
            return {"code": 0, "data": {"file_token": "boxtok%d" % len(CALLS)}}
        if method == "POST" and path.endswith("/records?user_id_type=open_id"):
            rid = "rec%03d" % (len(RECORDS) + 1)
            RECORDS[rid] = dict(body["fields"], 编号="FB-%04d" % (len(RECORDS) + 1))
            return {"code": 0, "data": {"record": {"record_id": rid, "fields": {"编号": RECORDS[rid]["编号"]}}}}
        if "/records/search" in path:
            conds = body["filter"]["conditions"]
            items = []
            for rid, f in RECORDS.items():
                ok = True
                for cnd in conds:
                    v = f.get(cnd["field_name"])
                    if cnd["field_name"] == "提交人":
                        ok &= bool(v) and v[0]["id"] in cnd["value"]
                    elif cnd["field_name"] == "已通知提交人":
                        ok &= not v
                    else:
                        ok &= v in cnd["value"]
                if ok:
                    items.append({"record_id": rid, "fields": f})
            return {"code": 0, "data": {"items": items}}
        if path.startswith("/im/v1/messages"):
            return {"code": 0}
        if method == "PUT":
            RECORDS[path.rsplit("/", 1)[1]].update(body["fields"])
            return {"code": 0}
        return {"code": 0}

    m.fs = fake_fs
    s = m.Server(("127.0.0.1", 0), m.H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield m, "http://127.0.0.1:%d/feedback/api" % s.server_address[1]
    s.shutdown()
    ow.shutdown()


def call(base, path, body=None, cookie="token=" + TOKEN_OK, origin="https://chat.auto-link.com.cn"):
    h = {"Content-Type": "application/json"}
    if cookie:
        h["Cookie"] = cookie
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


GOOD = {"title": "选 opus 报 403", "类型": "用不了/报错", "工具": "Cursor", "电脑": "Windows", "紧急程度": "影响工作",
        "detail": "报错:key sk-abcdefghijklmnop1234 not allowed", "source": "oneclick#cursor"}


def test_not_logged_in_is_401(srv):
    _, base = srv
    assert call(base, "/me", cookie=None)[0] == 401
    assert call(base, "/me", cookie="token=forged")[0] == 401
    assert call(base, "/submit", GOOD, cookie="token=forged")[0] == 401


def test_me_returns_identity_from_owui(srv):
    _, base = srv
    s, d = call(base, "/me")
    assert s == 200 and d["name"] == "张三" and d["feishu"] is True and "类型" in d["choices"]


def test_submit_writes_identity_from_server_not_client(srv):
    _, base = srv
    body = dict(GOOD, 提交人邮箱="evil@x.com", name="冒充")
    s, d = call(base, "/submit", body)
    assert s == 200 and d["no"].startswith("FB-")
    f = RECORDS["rec001"]
    assert f["提交人"] == [{"id": OPEN_ID}] and f["提交人邮箱"] == "zs@auto-link.com.cn"
    assert f["状态"] == "新提交" and f["类型"] == "用不了/报错" and f["电脑"] == "Windows"
    assert "sk-abcdefghijklmnop1234" not in f["详细描述"] and "sk-***" in f["详细描述"]


def test_bad_choice_and_empty_title_rejected(srv):
    _, base = srv
    assert call(base, "/submit", dict(GOOD, 类型="乱写"))[0] == 400
    assert call(base, "/submit", dict(GOOD, title="  "))[0] == 400


def test_cross_origin_rejected(srv):
    _, base = srv
    assert call(base, "/submit", GOOD, origin="https://evil.example")[0] == 403


def test_image_upload_attached(srv):
    import base64
    _, base = srv
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    s, d = call(base, "/submit", dict(GOOD, images=[{"name": "a b.png", "data": png}]))
    assert s == 200
    f = RECORDS[d["id"]]
    assert f["截图"] and f["截图"][0]["file_token"].startswith("boxtok")


def test_rate_limit_three_per_minute(srv):
    m, base = srv
    m.RATE.clear()
    codes = [call(base, "/submit", GOOD)[0] for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    m.RATE.clear()


def test_mine_only_returns_own(srv):
    _, base = srv
    RECORDS["recX"] = {"一句话说明": "别人的", "提交人": [{"id": "ou_other"}], "编号": "FB-9999", "状态": "新提交"}
    s, d = call(base, "/mine")
    titles = [x["title"] for x in d["items"]]
    assert s == 200 and "别人的" not in titles and "选 opus 报 403" in titles


def test_notify_resolved_once(srv):
    m, _ = srv
    RECORDS["rec001"].update(状态="已解决", 处理说明="已给你的 key 开通 opus")
    CALLS.clear()
    m.CFG["NOTIFY_INTERVAL"] = "0"
    t = threading.Thread(target=m.notify_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    sent = [c for c in CALLS if c[1].startswith("/im/v1/messages")]
    assert any(OPEN_ID == c[2]["receive_id"] and "已给你的 key 开通 opus" in json.loads(c[2]["content"])["text"] for c in sent)
    assert RECORDS["rec001"]["已通知提交人"] is True
    n = len([c for c in CALLS if c[1].startswith("/im/v1/messages") and c[2]["receive_id"] == OPEN_ID])
    time.sleep(0.3)
    n2 = len([c for c in CALLS if c[1].startswith("/im/v1/messages") and c[2]["receive_id"] == OPEN_ID])
    assert n == n2 == 1, "同一条只通知一次"
