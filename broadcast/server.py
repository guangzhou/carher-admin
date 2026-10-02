#!/usr/bin/env python3
"""
feishu-broadcast — 从飞书多维表里筛人，用自建机器人群发消息的内网页面。

WHAT IT TOUCHES
---------------
  读：一张多维表（bot 必须是这张表的协作者，至少「可阅读」）
  写：只调 im/v1/messages 发消息。不改表，不碰 carher-admin / litellm 的任何东西。
  本地：DATA_DIR/jobs/<id>.json 发送记录（hostPath，滚 pod 不丢）。

WHY IT LOOKS LIKE THIS（2026-10-01 实测）
----------------------------------------
1. bot 读不到行时 records/search 返回 `code 0 + total 0`，不报错。两个病同形：
   bot 不是协作者 / bot 是「可阅读」协作者但表开了高级权限、行被角色规则滤光。
   所以「读到 0 行」在页面上按故障展示并分开提示（diagnose_empty），绝不读成「表是空的」。
2. 一人多行（Cursor 一行、Claude Code 一行）：10-01 实测 1391 行 = 661 人。按 open_id 去重。
3. 192 人的人员字段没有 email ⇒ 不能按邮箱发。用 `user_id_type=open_id` 读人员字段，
   拿到的就是本 app 视角的 open_id。表里那列文本 `open_id` 是别的 app 的，不用
   （塞进来是 `99992361 open_id cross app`）。
4. 能打开这页 = 能以机器人身份给几百人发消息 ⇒ 必须登录，群发要手输人数确认，
   服务端按同一套筛选重算名单，人数对不上就拒发。
5. 失败按原因分桶（不在可用范围 / 跨 app / 限流 / 网络 / 其它），不合并成一个百分比。
6. 每个收件人的 uuid = sha256(root_job_id:open_id)：重试复用同一个 uuid，
   飞书 1 小时内按 uuid 去重 ⇒ 「网络错但其实送达了」的人重试也不会收到两条。

Run locally:
  FEISHU_APP_ID=... FEISHU_APP_SECRET=... BROADCAST_PASSWORD=... \\
  BROADCAST_DATA_DIR=/tmp/fb PORT=9000 python3 server.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPEN_BASE = os.environ.get("FEISHU_OPEN_BASE", "https://open.feishu.cn")
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
PASSWORD = os.environ.get("BROADCAST_PASSWORD", "")
BASE_TOKEN = os.environ.get("BROADCAST_BASE_TOKEN", "DlT9bsrwMad12VsogEpcK9Ptncc")
TABLE_ID = os.environ.get("BROADCAST_TABLE_ID", "tblJT2s6Y6xjYj5A")
VIEW_ID = os.environ.get("BROADCAST_VIEW_ID", "vewISEKPg7")
PERSON_FIELD = os.environ.get("BROADCAST_PERSON_FIELD", "姓名")
FILTER_FIELDS = [f.strip() for f in os.environ.get(
    "BROADCAST_FILTER_FIELDS", "是否在职,组织名称,部门名称,账户类型,先锋,实验,状态"
).split(",") if f.strip()]
DEFAULT_FILTERS = {"是否在职": ["在职"]}   # 默认不发给离职的人
TEST_OPEN_ID = os.environ.get("BROADCAST_TEST_OPEN_ID", "")  # 「先发给我看看」的收件人
DATA_DIR = os.environ.get("BROADCAST_DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
RATE = float(os.environ.get("BROADCAST_RATE", "5"))          # 条/秒；飞书单 app 上限 50 QPS
MAX_RECIPIENTS = int(os.environ.get("BROADCAST_MAX_RECIPIENTS", "2000"))
TABLE_TTL = int(os.environ.get("BROADCAST_TABLE_TTL", "300"))
SESSION_TTL = 12 * 3600
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 600
MAX_BODY_CHARS = 20000
COOKIE = "fb_session"
EMPTY = "<空>"

NET_ERROR = -2
TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}
# 分桶只是提示；页面上永远同时给出飞书原始 code/msg，分错了一眼能看出来。
AVAIL_CODES = {230013}          # Bot has NO availability to this user（不在可用范围）
CROSS_APP_CODES = {99992361,    # open_id cross app
                   99992351}    # not a valid {open_id} or not exists（2026-10-01 实测）
RATE_CODES = {99991400, 230020}
BUCKET_LABELS = {
    "not_in_scope": "不在机器人可用范围",
    "cross_app": "open_id 无效或不属于本 app",
    "rate_limited": "被限流（已自动重试）",
    "network": "网络错误（已自动重试）",
    "other": "其它",
}
# bot 读到 0 行时飞书一律 code 0，不报错。2026-10-01 实测有两个病同形，提示必须分开说：
#   ① bot 不是协作者 —— 连字段都读不到
#   ② bot 是协作者（可阅读）但表开了「高级权限」—— 表/字段都能读，行被角色的行规则滤光
#      （那张表的「角色3」= 只看自己提交的行；bot 没提交过任何行 ⇒ 0 行）
EMPTY_HINT_NOT_COLLAB = (
    "bot 读到 0 行，而且连字段都读不到 ⇒ bot 还不是这张表的协作者。"
    "请在多维表右上角「…→ 添加文档应用」里把这个机器人加进来，再点刷新。"
)
EMPTY_HINT_ADVANCED = (
    "bot 读到 0 行，但能读到表和字段，且这张表开了「高级权限」⇒ 行被角色的行规则滤掉了"
    "（「可阅读」协作者看哪些行由角色决定，只有「可管理」不受限）。"
    "二选一：把 bot 升成「可管理」；或在高级权限里给 bot 单建一个「所有行可读、只开需要的字段」的角色。"
)
EMPTY_HINT_UNKNOWN = (
    "bot 读到 0 行，能读到字段且没开高级权限。可能视图本身有筛选、或表确实是空的——"
    "请用自己的账号打开同一视图对照行数。"
)


def diagnose_empty(fields_readable: bool, is_advanced: bool | None) -> str:
    """0 行时判是哪一层挡住的。纯函数，单测钉住。"""
    if not fields_readable:
        return EMPTY_HINT_NOT_COLLAB
    if is_advanced:
        return EMPTY_HINT_ADVANCED
    return EMPTY_HINT_UNKNOWN
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'none'")


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, http_status: int = 0):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.http_status = http_status


def _http(method: str, path: str, *, token: str | None = None,
          body: dict | None = None, timeout: float = 15.0) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OPEN_BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    status = 200
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # 飞书业务错误也走 4xx，body 里才有 code/msg
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeishuError(NET_ERROR, f"network: {exc}") from exc
    try:
        payload = json.loads(raw.decode())
    except ValueError:
        raise FeishuError(-1, f"HTTP {status}: {raw[:200]!r}", status) from None
    if payload.get("code", 0) != 0:
        raise FeishuError(payload.get("code", -1), str(payload.get("msg", "")), status)
    return payload


class TokenCache:
    def __init__(self) -> None:
        self._tok = ""
        self._exp = 0.0
        self._lock = threading.Lock()

    def get(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._tok or time.time() >= self._exp:
                p = _http("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                          body={"app_id": APP_ID, "app_secret": APP_SECRET})
                self._tok = p["tenant_access_token"]
                self._exp = time.time() + max(p.get("expire", 7200) - 120, 60)
            return self._tok


TOKENS = TokenCache()


def api(method: str, path: str, body: dict | None = None) -> dict:
    try:
        return _http(method, path, token=TOKENS.get(), body=body)
    except FeishuError as exc:
        if exc.code not in TOKEN_INVALID_CODES:
            raise
        return _http(method, path, token=TOKENS.get(force=True), body=body)


# ------------------------------------------------------------ table → people
# 以下是纯函数，单测直接打，不碰网络。

def cell_values(v) -> list[str]:
    """把一格多维表的值摊平成字符串列表。空格子 => [EMPTY]，让「空」本身也能被筛。"""
    if v is None or v == "" or v == []:
        return [EMPTY]
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                s = x.get("name") or x.get("text") or x.get("link") or ""
            else:
                s = str(x)
            if s:
                out.append(s)
        return out or [EMPTY]
    if isinstance(v, dict):
        return [v.get("name") or v.get("text") or json.dumps(v, ensure_ascii=False)]
    return [str(v)]


def build_people(records: list[dict], person_field: str,
                 filter_fields: list[str]) -> tuple[list[dict], dict]:
    """多行 → 去重的人。同一人多行时，每个筛选字段取各行值的并集。

    并集的含义：「账户类型 = Cursor」选中的是「有一行是 Cursor」的人，
    而不是「所有行都是 Cursor」的人——这是群发时符合直觉的那个语义。
    """
    people: dict[str, dict] = {}
    stats = {"rows": len(records), "rows_without_person": 0}
    for rec in records:
        fields = rec.get("fields") or {}
        persons = fields.get(person_field) or []
        if not isinstance(persons, list) or not persons:
            stats["rows_without_person"] += 1
            continue
        for p in persons:
            oid = (p or {}).get("id")
            if not oid:
                continue
            ent = people.get(oid)
            if ent is None:
                ent = people[oid] = {
                    "open_id": oid,
                    "name": p.get("name") or p.get("en_name") or oid,
                    "email": p.get("email") or "",
                    "rows": 0,
                    "attrs": {f: set() for f in filter_fields},
                }
            ent["rows"] += 1
            for f in filter_fields:
                ent["attrs"][f].update(cell_values(fields.get(f)))
    out = []
    for ent in people.values():
        ent["attrs"] = {k: sorted(v) for k, v in ent["attrs"].items()}
        out.append(ent)
    out.sort(key=lambda e: e["name"])
    stats["people"] = len(out)
    return out, stats


def filter_options(people: list[dict], filter_fields: list[str]) -> dict:
    """每个筛选字段的取值 → 人数（不是行数）。"""
    opts: dict[str, dict[str, int]] = {f: {} for f in filter_fields}
    for p in people:
        for f in filter_fields:
            for v in p["attrs"].get(f, []):
                opts[f][v] = opts[f].get(v, 0) + 1
    return {f: dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) for f, c in opts.items()}


def select(people: list[dict], filters: dict, include: list[str] | None = None,
           exclude: list[str] | None = None) -> list[dict]:
    """字段之间 AND，同一字段多个值之间 OR；空列表 = 这个字段不限。

    include 里的人无视筛选强制加入，exclude 最后一刀剔除（exclude 优先）。
    """
    active = {f: set(vs) for f, vs in (filters or {}).items() if vs}
    inc, exc = set(include or []), set(exclude or [])
    out = []
    for p in people:
        oid = p["open_id"]
        if oid in exc:
            continue
        hit = all(set(p["attrs"].get(f, [EMPTY])) & vs for f, vs in active.items())
        if hit or oid in inc:
            out.append(p)
    return out


def render(template: str, person: dict) -> str:
    """只认 {name} 一个占位符。不用 str.format：消息正文里出现的 { } 不该让发送炸掉。"""
    return template.replace("{name}", person.get("name", ""))


def build_content(msg_type: str, body: str, title: str = "") -> tuple[str, str]:
    """返回 (飞书 msg_type, content JSON 串)。markdown 包成 card 2.0——飞书没有裸 markdown 消息。"""
    if msg_type == "text":
        return "text", json.dumps({"text": body}, ensure_ascii=False)
    if msg_type == "markdown":
        card: dict = {"schema": "2.0",
                      "body": {"elements": [{"tag": "markdown", "content": body}]}}
        if title:
            card["header"] = {"title": {"tag": "plain_text", "content": title},
                              "template": "blue"}
        return "interactive", json.dumps(card, ensure_ascii=False)
    raise ValueError(f"unknown msg_type {msg_type!r}")


def recipient_uuid(root_id: str, open_id: str) -> str:
    """飞书 uuid 上限 50 字符；同一次群发（含它的所有重试）对同一人恒定。"""
    return hashlib.sha256(f"{root_id}:{open_id}".encode()).hexdigest()[:40]


def bucket_of(code: int) -> str:
    if code in AVAIL_CODES:
        return "not_in_scope"
    if code in CROSS_APP_CODES:
        return "cross_app"
    if code in RATE_CODES:
        return "rate_limited"
    if code == NET_ERROR:
        return "network"
    return "other"


def retryable(code: int, http_status: int) -> bool:
    return code == NET_ERROR or code in RATE_CODES or http_status == 429 or http_status >= 500


# ------------------------------------------------------------ live table

class TableCache:
    def __init__(self) -> None:
        self.people: list[dict] = []
        self.stats: dict = {}
        self.fetched_at = 0.0
        self.error = ""
        self._lock = threading.Lock()

    def fetch_records(self) -> list[dict]:
        rows, page_token = [], ""
        fields = [PERSON_FIELD] + FILTER_FIELDS
        for _ in range(100):  # 500×100 = 5 万行硬上限，防止 has_more 永真时死循环
            q = {"page_size": "500", "user_id_type": "open_id"}
            if page_token:
                q["page_token"] = page_token
            path = (f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}"
                    f"/records/search?{urllib.parse.urlencode(q)}")
            data = api("POST", path, {"view_id": VIEW_ID, "field_names": fields})["data"]
            rows += data.get("items") or []
            if not data.get("has_more"):
                return rows
            page_token = data.get("page_token") or ""
            if not page_token:
                break
        raise FeishuError(-1, f"分页没有正常结束（已读 {len(rows)} 行），拒绝用不完整的名单")

    def probe_empty(self) -> str:
        """读到 0 行后，再探两层，给出是哪个病的提示。"""
        try:
            api("GET", f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/fields?page_size=1")
            fields_ok = True
        except FeishuError:
            fields_ok = False
        try:
            meta = api("GET", f"/open-apis/bitable/v1/apps/{BASE_TOKEN}")["data"]["app"]
            advanced = bool(meta.get("is_advanced"))
        except (FeishuError, KeyError, TypeError):
            advanced = None
        return diagnose_empty(fields_ok, advanced)

    def get(self, force: bool = False) -> tuple[list[dict], dict]:
        with self._lock:
            if force or not self.fetched_at or time.time() - self.fetched_at > TABLE_TTL:
                try:
                    records = self.fetch_records()
                    self.people, self.stats = build_people(records, PERSON_FIELD, FILTER_FIELDS)
                    self.error = "" if records else self.probe_empty()
                except FeishuError as exc:
                    self.people, self.stats = [], {}
                    self.error = f"读表失败：{exc}"
                self.fetched_at = time.time()
            return self.people, dict(self.stats, error=self.error, fetched_at=self.fetched_at)


TABLE = TableCache()


# ------------------------------------------------------------ jobs

class Throttle:
    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


THROTTLE = Throttle(RATE)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
WORK: "queue.Queue[str]" = queue.Queue()


def jobs_dir() -> str:
    d = os.path.join(DATA_DIR, "jobs")
    os.makedirs(d, exist_ok=True)
    return d


def save_job(job: dict) -> None:
    path = os.path.join(jobs_dir(), f"{job['id']}.json")
    tmp = path + ".tmp"
    with JOBS_LOCK:
        blob = json.dumps(job, ensure_ascii=False)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(blob)
    os.replace(tmp, path)


def load_jobs() -> None:
    try:
        names = os.listdir(jobs_dir())
    except OSError:
        return
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(jobs_dir(), n), encoding="utf-8") as fh:
                job = json.load(fh)
        except (OSError, ValueError):
            continue
        if job.get("status") in ("queued", "running"):
            # 进程死在发送途中。不自动续发：哪些人其实已经收到，只有飞书知道。
            # 标成 interrupted，让人看一眼再决定点「重发未完成的」（uuid 会兜住 1 小时内的重复）。
            job["status"] = "interrupted"
            for r in job["recipients"]:
                if r["status"] in ("pending", "sending"):
                    r["status"] = "unknown"
        JOBS[job["id"]] = job


def summarize(job: dict) -> dict:
    counts = {"pending": 0, "sending": 0, "sent": 0, "failed": 0, "unknown": 0}
    buckets: dict[str, int] = {}
    for r in job["recipients"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] == "failed":
            b = r.get("bucket", "other")
            buckets[b] = buckets.get(b, 0) + 1
    return {k: job[k] for k in ("id", "root_id", "status", "created_at", "finished_at",
                                 "msg_type", "title", "body", "filters", "test", "note")
            if k in job} | {"total": len(job["recipients"]), "counts": counts,
                            "buckets": {b: [BUCKET_LABELS.get(b, b), n] for b, n in buckets.items()}}


def new_job(recipients: list[dict], msg_type: str, title: str, body: str,
            filters: dict, test: bool, root_id: str = "", note: str = "") -> dict:
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    job = {
        "id": jid, "root_id": root_id or jid, "status": "queued",
        "created_at": time.time(), "finished_at": None,
        "msg_type": msg_type, "title": title, "body": body,
        "filters": filters, "test": test, "note": note,
        "recipients": [{"open_id": p["open_id"], "name": p["name"], "status": "pending",
                        "code": None, "msg": "", "message_id": "", "attempts": 0}
                       for p in recipients],
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    save_job(job)
    WORK.put(jid)
    return job


def send_one(job: dict, r: dict) -> None:
    msg_type, content = build_content(job["msg_type"], render(job["body"], r),
                                      render(job.get("title", ""), r))
    body = {"receive_id": r["open_id"], "msg_type": msg_type, "content": content,
            "uuid": recipient_uuid(job["root_id"], r["open_id"])}
    for attempt in range(1, 4):
        THROTTLE.wait()
        r["attempts"] = attempt
        try:
            resp = api("POST", "/open-apis/im/v1/messages?receive_id_type=open_id", body)
        except FeishuError as exc:
            r.update(code=exc.code, msg=exc.msg)
            if retryable(exc.code, exc.http_status) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            r.update(status="failed", bucket=bucket_of(exc.code))
            return
        r.update(status="sent", code=0, msg="", bucket="",
                 message_id=(resp.get("data") or {}).get("message_id", ""))
        return


def worker() -> None:
    while True:
        jid = WORK.get()
        job = JOBS.get(jid)
        if not job:
            continue
        job["status"] = "running"
        save_job(job)
        last_save = time.time()
        for r in job["recipients"]:
            if r["status"] != "pending":
                continue
            r["status"] = "sending"
            try:
                send_one(job, r)
            except Exception as exc:  # noqa: BLE001 — 一个人的意外不能让整批停在半路
                r.update(status="failed", code=-1, msg=f"internal: {exc}", bucket="other")
            if time.time() - last_save > 2:
                save_job(job)
                last_save = time.time()
        job["status"] = "done"
        job["finished_at"] = time.time()
        save_job(job)


# ------------------------------------------------------------ auth

class Auth:
    """单口令登录。锁定按「全局」而不是按 IP：NodePort 后面看到的源 IP 常是节点 IP，
    按 IP 锁等于没锁。代价是有人乱试会把你也锁 10 分钟——可接受，比被试出来好。"""

    def __init__(self, password: str) -> None:
        self.password = password
        self.sessions: dict[str, float] = {}
        self.fails: list[float] = []
        self.locked_until = 0.0
        self._lock = threading.Lock()

    def login(self, pw: str, now: float | None = None) -> tuple[str | None, str]:
        now = time.time() if now is None else now
        with self._lock:
            if now < self.locked_until:
                return None, f"尝试次数过多，{int(self.locked_until - now)} 秒后再试"
            if self.password and hmac.compare_digest(pw.encode(), self.password.encode()):
                self.fails.clear()
                sid = secrets.token_urlsafe(32)
                self.sessions[sid] = now + SESSION_TTL
                return sid, ""
            self.fails = [t for t in self.fails if now - t < LOGIN_LOCK_SECONDS] + [now]
            if len(self.fails) >= LOGIN_MAX_FAILS:
                self.locked_until = now + LOGIN_LOCK_SECONDS
                self.fails.clear()
                return None, f"口令错误次数过多，锁定 {LOGIN_LOCK_SECONDS // 60} 分钟"
            return None, "口令错误"

    def check(self, sid: str | None, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            exp = self.sessions.get(sid or "")
            if not exp:
                return False
            if now > exp:
                self.sessions.pop(sid, None)
                return False
            return True

    def logout(self, sid: str | None) -> None:
        with self._lock:
            self.sessions.pop(sid or "", None)


AUTH = Auth(PASSWORD)


# ------------------------------------------------------------ http

def _parse_selection(body: dict) -> tuple[dict, list, list]:
    filters = body.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters 必须是对象")
    filters = {str(k): [str(x) for x in (v or [])] for k, v in filters.items()
               if k in FILTER_FIELDS}
    inc = [str(x) for x in body.get("include") or []]
    exc = [str(x) for x in body.get("exclude") or []]
    return filters, inc, exc


def _parse_message(body: dict) -> tuple[str, str, str]:
    msg_type = body.get("msg_type") or "text"
    if msg_type not in ("text", "markdown"):
        raise ValueError("msg_type 只能是 text 或 markdown")
    text = str(body.get("body") or "")
    title = str(body.get("title") or "")[:100]
    if not text.strip():
        raise ValueError("消息内容不能为空")
    if len(text) > MAX_BODY_CHARS:
        raise ValueError(f"消息超过 {MAX_BODY_CHARS} 字")
    return msg_type, title, text


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-broadcast/1"

    def log_message(self, fmt, *args):  # 不打 query/body，access log 里不留消息正文
        print(f"{self.address_string()} {self.command} {self.path.split('?')[0]} "
              f"{args[1] if len(args) > 1 else ''}", flush=True)

    # -- plumbing
    def _sid(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _send(self, code: int, payload, ctype="application/json; charset=utf-8",
              headers: dict | None = None) -> None:
        blob = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(blob)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 256 * 1024:
            raise ValueError("请求体过大")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _same_origin(self) -> bool:
        # 所有写操作都是 JSON POST + SameSite=Strict cookie；再校验一次 Origin 兜 CSRF。
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == (self.headers.get("Host") or "")

    # -- routes
    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, {"ok": True})
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if not AUTH.check(self._sid()):
            return self._send(401, {"error": "未登录"})
        if path == "/api/people":
            force = "refresh=1" in (urllib.parse.urlparse(self.path).query or "")
            people, stats = TABLE.get(force=force)
            return self._send(200, {
                "stats": stats, "filter_fields": FILTER_FIELDS, "defaults": DEFAULT_FILTERS,
                "options": filter_options(people, FILTER_FIELDS),
                "people": [{"open_id": p["open_id"], "name": p["name"], "email": p["email"],
                            "attrs": p["attrs"]} for p in people],
                "test_ready": bool(TEST_OPEN_ID),
            })
        if path == "/api/jobs":
            with JOBS_LOCK:
                items = sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)
                return self._send(200, {"jobs": [summarize(j) for j in items[:50]]})
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._send(404, {"error": "没有这条记录"})
            with JOBS_LOCK:
                return self._send(200, {"job": summarize(job), "recipients": job["recipients"]})
        return self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if not self._same_origin():
            return self._send(403, {"error": "跨站请求被拒绝"})
        try:
            body = self._json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})

        if path == "/api/login":
            sid, err = AUTH.login(str(body.get("password") or ""))
            if not sid:
                return self._send(401, {"error": err})
            return self._send(200, {"ok": True}, headers={"Set-Cookie": (
                f"{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")})
        if path == "/api/logout":
            AUTH.logout(self._sid())
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
        if not AUTH.check(self._sid()):
            return self._send(401, {"error": "未登录"})

        try:
            if path == "/api/preview":
                return self._preview(body)
            if path == "/api/test":
                return self._test(body)
            if path == "/api/send":
                return self._broadcast(body)
            if path.startswith("/api/jobs/") and path.endswith("/retry"):
                return self._retry(path.split("/")[3])
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(404, {"error": "not found"})

    def _preview(self, body: dict) -> None:
        filters, inc, exc = _parse_selection(body)
        people, stats = TABLE.get()
        chosen = select(people, filters, inc, exc)
        return self._send(200, {"count": len(chosen), "error": stats.get("error", ""),
                                "recipients": [{"open_id": p["open_id"], "name": p["name"]}
                                               for p in chosen]})

    def _test(self, body: dict) -> None:
        msg_type, title, text = _parse_message(body)
        if not TEST_OPEN_ID:
            raise ValueError("服务端没配 BROADCAST_TEST_OPEN_ID，不知道「我」是谁")
        me = {"open_id": TEST_OPEN_ID, "name": str(body.get("test_name") or "测试")}
        job = new_job([me], msg_type, title, text, {}, test=True)
        return self._send(200, {"job": summarize(job)})

    def _broadcast(self, body: dict) -> None:
        msg_type, title, text = _parse_message(body)
        filters, inc, exc = _parse_selection(body)
        people, stats = TABLE.get()
        if stats.get("error"):
            raise ValueError(stats["error"])
        # 服务端按同一套条件重算——不信前端送来的名单。表在预览和发送之间变了，人数就对不上。
        chosen = select(people, filters, inc, exc)
        try:
            confirm = int(body.get("confirm_count"))
        except (TypeError, ValueError):
            raise ValueError("请输入收件人数确认") from None
        if confirm != len(chosen):
            raise ValueError(f"确认人数 {confirm} 与当前名单 {len(chosen)} 人不符"
                             f"（表可能刚变过），请重新预览")
        if not chosen:
            raise ValueError("名单为空")
        if len(chosen) > MAX_RECIPIENTS:
            raise ValueError(f"一次最多 {MAX_RECIPIENTS} 人")
        job = new_job(chosen, msg_type, title, text, {"filters": filters, "include": inc,
                                                      "exclude": exc}, test=False)
        return self._send(200, {"job": summarize(job)})

    def _retry(self, jid: str) -> None:
        old = JOBS.get(jid)
        if not old:
            return self._send(404, {"error": "没有这条记录"})
        if old["status"] in ("queued", "running"):
            raise ValueError("这条还在发，等它跑完")
        todo = [{"open_id": r["open_id"], "name": r["name"]} for r in old["recipients"]
                if r["status"] in ("failed", "unknown")]
        if not todo:
            raise ValueError("没有失败或状态未知的收件人")
        # root_id 沿用原批次 ⇒ uuid 不变 ⇒ 飞书侧 1 小时内对已送达的人去重
        job = new_job(todo, old["msg_type"], old.get("title", ""), old["body"],
                      old.get("filters", {}), old.get("test", False),
                      root_id=old["root_id"], note=f"重发 {jid} 中未成功的 {len(todo)} 人")
        return self._send(200, {"job": summarize(job)})


with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
          "rb") as _fh:
    PAGE = _fh.read()


def main() -> None:
    missing = [k for k, v in (("FEISHU_APP_ID", APP_ID), ("FEISHU_APP_SECRET", APP_SECRET),
                              ("BROADCAST_PASSWORD", PASSWORD)) if not v]
    if missing:
        raise SystemExit(f"缺少环境变量：{', '.join(missing)}")
    if len(PASSWORD) < 10:
        raise SystemExit("BROADCAST_PASSWORD 至少 10 位")
    load_jobs()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"feishu-broadcast on :{PORT} base={BASE_TOKEN}/{TABLE_ID}/{VIEW_ID} rate={RATE}/s",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
