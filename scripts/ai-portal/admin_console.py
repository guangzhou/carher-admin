"""账户库管理后台:https://chat.auto-link.com.cn/ai/admin/ (门户后端转发账户库 API,账户库不开公网口)

由 server.py 加载(同 feedback_server 的加载方式),只用标准库。门户的 HttpError / RateLimiter / 打码函数由
server.py 注入,不反向 import(server.py 在测试里按文件路径加载,模块名不固定)。

谁能进 —— 三条**全在服务端**判,页面和每个 /api/admin/* 都过同一个 gate():
  ① 会话来自飞书网页 OAuth(login == "oauth")。magic link 会话一律不行:外部人只能走 magic。
  ② 本公司租户:tenant_key == OUR_TENANT_KEY 且 external 为假(两者是同一事实的两种写法,都查,失败关闭)。
  ③ union_id 在 ADMIN_UNION_IDS 白名单里(逗号分隔;空 = 谁都进不来)。
CSRF:每个 /api/admin/* 都要 X-Requested-With: ai-admin(自定义头 ⇒ 跨站必须预检,而门户不回任何 CORS 头);
  有 Origin 就必须是本站;有 Sec-Fetch-Site 就必须是 same-origin。POST 另外还过门户原有的 same_origin(required)。
账户库 token:ACCOUNT_STORE_TOKEN_FILE 指向 198 上 root 0600 的文件(scope read / reveal:any / audit:read)。
  文件不在 / 权限宽于 0600 ⇒ 后台关闭(503),**门户其余功能照常**(不为后台把整个门户拒绝启动)。
明文:只有 POST /api/admin/reveal 会出现;必须填原因;门户本地 admin_audit 先记一行再去账户库取,
  账户库那边自己再记一行(actor = adminweb:admin-web:<姓名>(<union_id>))。列表 / 导出 / 审计接口一律不含明文。
写操作(改状态 / 封禁 / 换发)故意不做:走 skill litellm-198-key-usage-audit 的现成 SOP,避免两套逻辑。
"""
import concurrent.futures, csv, io, json, os, re, stat, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    "ADMIN_UNION_IDS": "on_0577e767b8995740ebbf83f06d9f8bd1",     # 刘国现
    "ADMIN_HTML": str(HERE / "admin" / "admin.html"),   # 不放 static/:那里的文件不过闸门
    "ACCOUNT_STORE_URL": "",            # 例 http://10.43.x.x(account-store Service 的 ClusterIP;deploy 现读)
    "ACCOUNT_STORE_TOKEN_FILE": "",     # 例 /etc/account-store/clients/adminweb.token(root 0600)
    "ACCOUNT_STORE_TIMEOUT": "10",
    "ADMIN_LIST_PER_MIN": "60",         # 列表 / 搜索 / 统计 / 审计 共用,每个管理员每分钟
    "ADMIN_REVEAL_PER_MIN": "10",
    "ADMIN_EXPORT_PER_MIN": "3",
    "ADMIN_GW_CHECK": "1",              # 列表里查网关封禁状态(要门户配了 LITELLM_MASTER_KEY);0 = 不查
    "ADMIN_GW_TIMEOUT": "5",
}
XRW = "ai-admin"
STATUSES = ("active", "disabled", "completed", "failed", "unknown")
TYPES = ("cursor", "claude_code", "carher")
KINDS = ("api_key", "cursor_key", "claude_code_key")
PAGE_SIZES = (20, 50, 100)
EXPORT_MAX = 20000
SOP = {"skill": ".claude/skills/litellm-198-key-usage-audit/SKILL.md",
       "text": "封禁 / 解封 / 重新生成 / 删旧 key 走 skill litellm-198-key-usage-audit 的 SOP 和脚本"
               "(按 open_id 封号、reissue-batch.sh)。后台只读,不做写操作,避免两套逻辑。"}
_UID_RE = re.compile(r"^on_[A-Za-z0-9]{8,64}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SK_ANY = re.compile(r"sk-")       # 导出里不许出现的字面串(打码形态 sk-…ABCD 也算)


def check_config(cfg):
    errs = []
    for x in (cfg.get("ADMIN_UNION_IDS") or "").split(","):
        if x.strip() and not _UID_RE.match(x.strip()):
            errs.append("ADMIN_UNION_IDS 里 %r 不是 on_xxx" % x.strip()[:20])
    url = (cfg.get("ACCOUNT_STORE_URL") or "").strip()
    if url and not re.match(r"^https?://[A-Za-z0-9.\-:\[\]]+/?$", url):
        errs.append("ACCOUNT_STORE_URL 要形如 http://<ClusterIP>(不带路径)")
    for k in ("ACCOUNT_STORE_TIMEOUT", "ADMIN_LIST_PER_MIN", "ADMIN_REVEAL_PER_MIN", "ADMIN_EXPORT_PER_MIN",
              "ADMIN_GW_TIMEOUT"):
        v = str(cfg.get(k) or DEFAULTS[k]).strip()
        if not v.isdigit() or int(v) < 1:
            errs.append("%s 要是正整数(现在是 %r)" % (k, v))
    return errs


def read_token_file(path):
    """root 0600 的 token 文件 → token;不合格返回 (None, 原因)。原因只写进日志,不回给浏览器。"""
    if not path:
        return None, "没配 ACCOUNT_STORE_TOKEN_FILE"
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            return None, "token 文件权限过宽(%s),要 0600/0400" % stat.filemode(st.st_mode)
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None, "token 文件不归运行用户所有"
        tok = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        return None, "token 文件读不了(%s)" % type(e).__name__
    if len(tok) < 20 or _CTRL.search(tok):
        return None, "token 文件内容不对"
    return tok, ""


class StoreError(Exception):
    def __init__(self, status, msg):
        super().__init__("%s %s" % (status, msg))
        self.status, self.msg = status, msg


class StoreClient:
    """账户库 HTTP 客户端。token 只在 Authorization 头里;不进日志、不进响应。"""

    def __init__(self, base, token, timeout):
        self.base, self.token, self.timeout = base.rstrip("/"), token, timeout

    def call(self, method, path, params=None, body=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = str((json.loads(e.read() or b"{}") or {}).get("error") or "")[:200]
            except ValueError:
                msg = ""
            raise StoreError(e.code, msg)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise StoreError(-1, type(e).__name__)


class Admin:
    def __init__(self, app, HttpError, RateLimiter, mask_deep, page_csp, log, short):
        self.app, self.cfg = app, app.cfg
        self.page_csp = page_csp
        self.HttpError, self.mask_deep, self.log, self.short = HttpError, mask_deep, log, short
        c = {k: str(self.cfg.get(k) or DEFAULTS[k]) for k in DEFAULTS}
        self.c = c
        # 键在 cfg 里就按它(显式置空 = 谁都进不来);不在(别的调用方没走 load_config)才用默认
        raw = self.cfg["ADMIN_UNION_IDS"] if "ADMIN_UNION_IDS" in self.cfg else DEFAULTS["ADMIN_UNION_IDS"]
        self.admins = {x.strip() for x in (raw or "").split(",") if x.strip()}
        self.rl_list = RateLimiter(c["ADMIN_LIST_PER_MIN"], 60, app.now)
        self.rl_reveal = RateLimiter(c["ADMIN_REVEAL_PER_MIN"], 60, app.now)
        self.rl_export = RateLimiter(c["ADMIN_EXPORT_PER_MIN"], 60, app.now)
        self.store, self.store_why = None, ""
        url = (self.cfg.get("ACCOUNT_STORE_URL") or "").strip()
        tok, why = read_token_file((self.cfg.get("ACCOUNT_STORE_TOKEN_FILE") or "").strip())
        if not url:
            self.store_why = "没配 ACCOUNT_STORE_URL"
        elif tok is None:
            self.store_why = why
        else:
            self.store = StoreClient(url, tok, int(c["ACCOUNT_STORE_TIMEOUT"]))
        if self.store is None:
            log("管理后台:账户库未接入,后台接口回 503 ——", self.store_why)
        app.q("create table if not exists admin_audit(id integer primary key autoincrement, at real, union_id text,"
              " name text, ip text, action text, account_id integer, kind text, reason text, result text)")
        self._gw_cache, self._gw_lock = {}, threading.Lock()

    # ── 闸门 ────────────────────────────────────────────────────────────
    def deny_reason(self, u):
        """None = 放行;否则是拒绝原因(只进日志/本地审计,浏览器只看到统一的 403 文案)。"""
        if u.get("login") != "oauth":
            return "not-oauth"
        if u.get("external") is not False or u.get("tenant_key") != self.cfg["OUR_TENANT_KEY"]:
            return "not-our-tenant"
        if u.get("app_id") and u.get("app_id") != self.cfg["FEISHU_APP_ID"]:
            return "other-app"
        if u.get("union_id") not in self.admins:
            return "not-admin"
        return None

    def gate(self, h):
        u = h.user()                    # 没会话 ⇒ 401
        why = self.deny_reason(u)
        if why:
            self.log("管理后台拒绝", why, self.short(u.get("union_id") or ""))
            self.audit(u, h.client_ip(), "deny", result=why)
            raise self.HttpError(403, "只有管理员能进后台(要用本公司飞书账号、点「用飞书登录」进来)")
        return u

    def csrf(self, h):
        if h.headers.get("X-Requested-With") != XRW:
            raise self.HttpError(403, "来源不对")
        o = h.headers.get("Origin")
        if o is not None and o != self.app.origin:
            raise self.HttpError(403, "来源不对")
        sfs = h.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs != "same-origin":
            raise self.HttpError(403, "来源不对")

    def need_store(self):
        if self.store is None:
            raise self.HttpError(503, "后台还没接上账户库(运维:查门户日志里「账户库未接入」那行)")
        return self.store

    def audit(self, u, ip, action, account_id=None, kind=None, reason=None, result=""):
        self.app.q("insert into admin_audit(at, union_id, name, ip, action, account_id, kind, reason, result)"
                   " values(?,?,?,?,?,?,?,?,?)",
                   (self.app.now(), u.get("union_id") or "", u.get("name") or "", ip, action, account_id, kind,
                    reason, result))

    def _store(self, method, path, params=None, body=None):
        try:
            return self.need_store().call(method, path, params, body)
        except StoreError as e:
            self.log("账户库出错", method, path.split("?")[0], e.status)
            if e.status in (401, 403):
                raise self.HttpError(502, "门户连账户库的 token 不对或权限不够(运维看日志)")
            if e.status in (400, 404, 409, 429):
                raise self.HttpError(e.status, e.msg or "账户库拒绝了这个请求")
            raise self.HttpError(502, "账户库暂时连不上,稍后再试")

    # ── 路由 ────────────────────────────────────────────────────────────
    def handle_get(self, h, p, q):
        a = self.app
        if p == "/admin":
            return h.redirect(a.base + "/admin/")
        if p == "/admin/":
            return self.page(h)
        if not p.startswith("/api/admin/"):
            raise self.HttpError(404, "not found")
        self.csrf(h)
        u = self.gate(h)
        one = lambda k: (q.get(k) or [""])[0]         # noqa: E731
        if p == "/api/admin/me":
            return h.send_json(200, {"name": u.get("name") or "", "union_id": u["union_id"],
                                     "store_ok": self.store is not None, "sop": SOP,
                                     "gw_check": self.gw_enabled, "page_sizes": list(PAGE_SIZES)})
        if p == "/api/admin/export.csv":
            return self.export(h, u, one)
        if not self.rl_list.hit(u["union_id"]):
            raise self.HttpError(429, "查得太快了,过一会儿再试")
        if p == "/api/admin/accounts":
            return h.send_json(200, self.accounts(one))
        if p == "/api/admin/stats":
            return h.send_json(200, self.stats())
        if p == "/api/admin/audit":
            return h.send_json(200, self.audit_list(one))
        raise self.HttpError(404, "not found")

    def handle_post(self, h, p):
        self.csrf(h)
        u = self.gate(h)
        if p == "/api/admin/reveal":
            return h.send_json(200, self.reveal(u, h.body_json(), h.client_ip()))
        raise self.HttpError(404, "not found")

    # ── 页面 ────────────────────────────────────────────────────────────
    def page(self, h):
        a = self.app
        u = a.session_user(h.headers.get("Cookie"))
        if not u:
            return h.redirect(a.base + "/auth/login?" + urllib.parse.urlencode({"redirect": a.base + "/admin/"}))
        why = self.deny_reason(u)
        if why:
            self.log("管理后台页面拒绝", why, self.short(u.get("union_id") or ""))
            self.audit(u, h.client_ip(), "deny-page", result=why)
            return h.send_text(403, "只有管理员能进后台。要用本公司飞书账号、点「用飞书登录」进来"
                                    "(机器人发的登录链接进不了后台)。")
        b = Path(self.c["ADMIN_HTML"]).read_bytes()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(b)))
        h.send_header("Cache-Control", "no-store")
        h._common_headers(referrer="no-referrer", csp=self.page_csp(b))
        h.end_headers()
        h._write(b)

    # ── 查询 ────────────────────────────────────────────────────────────
    def filters(self, one):
        f = {}
        for k in ("q", "org", "dept"):
            v = _CTRL.sub("", one(k)).strip()
            if len(v) > 100:
                raise self.HttpError(400, "%s 太长" % k)
            if v:
                f[k] = v
        st, ty = one("status"), one("type")
        if st and st not in STATUSES:
            raise self.HttpError(400, "status 不对")
        if ty and ty not in TYPES:
            raise self.HttpError(400, "type 不对")
        if st:
            f["status"] = st
        if ty:
            f["type"] = ty
        return f

    def accounts(self, one):
        f = self.filters(one)
        try:
            page = max(int(one("page") or 1), 1)
            size = int(one("size") or 50)
        except ValueError:
            raise self.HttpError(400, "page / size 要是数字")
        if size not in PAGE_SIZES:
            raise self.HttpError(400, "size 只能是 %s" % "/".join(map(str, PAGE_SIZES)))
        offset = (page - 1) * size
        if offset > 100000:
            raise self.HttpError(400, "页码太大")
        d = self._store("GET", "/v1/accounts", {**f, "limit": size, "offset": offset, "order": "applied_desc"})
        items = d.get("items") or []
        if "total" not in d:      # 账户库还是老版本:不认 q/dept/offset ⇒ 宁可报错也不给错的结果
            raise self.HttpError(502, "账户库版本太旧(不支持搜索 / 分页),先升级账户库")
        rows = [self.view(r) for r in items]
        self.fill_gateway(rows, items)
        return self.mask_deep({"items": rows, "total": d["total"], "page": page, "size": size})

    @staticmethod
    def _depts(r):
        try:
            v = json.loads(r.get("dept_names") or "[]")
            return [str(x) for x in v] if isinstance(v, list) else [str(v)]
        except ValueError:
            return [str(r.get("dept_names"))]

    def view(self, r):
        sec = {s.get("kind"): s for s in (r.get("secrets") or [])}
        main = sec.get("api_key") or next(iter(sec.values()), {})
        return {"id": r.get("id"), "name": r.get("owner_name") or "", "union_id": r.get("owner_union_id") or "",
                "email_prefix": r.get("email_prefix") or "", "org": r.get("org_name") or "",
                "depts": self._depts(r), "type": r.get("account_type") or "", "status": r.get("status") or "",
                "status_raw": r.get("status_raw") or "", "alias": r.get("key_alias") or "",
                "masked": main.get("hint") or "", "secret_state": main.get("state") or "",
                "kinds": [k for k, s in sec.items() if s.get("state") == "encrypted"],
                "applied_at": r.get("applied_at") or "", "gw": "unchecked"}

    # 网关封禁状态:/key/info?key=<sha256>(LiteLLMClient 注释:按 alias 查在 198 上是 200+全 null)
    @property
    def gw_enabled(self):
        return self.c["ADMIN_GW_CHECK"] == "1" and bool(getattr(self.app.litellm, "enabled", False))

    def _gw_one(self, sha):
        t = self.app.now()
        with self._gw_lock:
            hit = self._gw_cache.get(sha)
            if hit and t - hit[0] < 120:
                return hit[1]
        try:
            st, d = self.app.litellm._http("GET", "/key/info?key=" + urllib.parse.quote(sha, safe=""),
                                           timeout=int(self.c["ADMIN_GW_TIMEOUT"]))
        except Exception:                       # noqa: BLE001 —— LiteLLMError / 超时:当「没查到」
            return "error"
        info = (d or {}).get("info") if st == 200 else None
        if st == 404 or (st == 200 and not info):
            v = "missing"
        elif st != 200:
            v = "error"
        else:
            v = "blocked" if info.get("blocked") else "ok"
        with self._gw_lock:
            if len(self._gw_cache) > 5000:
                self._gw_cache.clear()
            self._gw_cache[sha] = (t, v)
        return v

    def fill_gateway(self, rows, items):
        if not self.gw_enabled:
            return
        jobs = {}
        for row, it in zip(rows, items):
            sec = {s.get("kind"): s for s in (it.get("secrets") or [])}
            sha = (sec.get("api_key") or {}).get("sha256")
            if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
                jobs[row["id"]] = sha
            else:
                row["gw"] = "nokey"
        if not jobs:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            fut = {ex.submit(self._gw_one, sha): rid for rid, sha in jobs.items()}
            done, _ = concurrent.futures.wait(fut, timeout=int(self.c["ADMIN_GW_TIMEOUT"]) + 3)
            res = {fut[f]: f.result() for f in done}
        for row in rows:
            if row["id"] in jobs:
                row["gw"] = res.get(row["id"], "error")

    def stats(self):
        d = self._store("GET", "/v1/stats")
        keep = ("accounts", "persons", "by_status", "by_type", "by_org", "secrets", "applied_7d", "applied_by_day_7d",
                "audit_rows")
        return self.mask_deep({k: d.get(k) for k in keep})

    def audit_list(self, one):
        params = {"limit": 50}
        scope = one("scope") or "reveal"
        if scope == "reveal":
            params["action_prefix"] = "reveal"
        elif scope != "all":
            raise self.HttpError(400, "scope 只能是 reveal / all")
        for k in ("before_id", "account_id"):
            v = one(k)
            if v:
                if not v.isdigit():
                    raise self.HttpError(400, "%s 要是数字" % k)
                params[k] = v
        d = self._store("GET", "/v1/audit", params)
        keep = ("id", "ts", "actor", "action", "account_id", "kind", "reason", "owner_name", "key_alias")
        return self.mask_deep({"items": [{k: it.get(k) for k in keep} for it in d.get("items") or []],
                               "next_before_id": d.get("next_before_id")})

    # ── 明文 ────────────────────────────────────────────────────────────
    def reveal(self, u, body, ip):
        aid, kind = body.get("account_id"), body.get("kind") or "api_key"
        reason = body.get("reason")
        if not isinstance(aid, int) or isinstance(aid, bool) or aid < 1:
            raise self.HttpError(400, "account_id 不对")
        if kind not in KINDS:
            raise self.HttpError(400, "kind 不对")
        reason = _CTRL.sub(" ", reason).strip() if isinstance(reason, str) else ""
        if len(reason) < 2:
            self.audit(u, ip, "reveal", aid, kind, reason, "denied:no-reason")
            raise self.HttpError(400, "必须填写原因(会写进审计)")
        if len(reason) > 200:
            raise self.HttpError(400, "原因最多 200 字")
        if not self.rl_reveal.hit(u["union_id"]):
            self.audit(u, ip, "reveal", aid, kind, reason, "denied:rate")
            raise self.HttpError(429, "看得太频繁了,过一会儿再试")
        self.need_store()
        # 先落本地审计(attempt),再去账户库取;账户库那边自己也先写审计再解密
        self.audit(u, ip, "reveal", aid, kind, reason, "attempt")
        actor = "admin-web:%s(%s)" % ((u.get("name") or "")[:40], u["union_id"])
        try:
            d = self._store("POST", "/v1/accounts/%d/reveal" % aid, body={"kind": kind, "reason": reason, "actor": actor})
        except Exception as e:
            self.audit(u, ip, "reveal", aid, kind, reason, "fail:%s" % getattr(e, "status", type(e).__name__))
            raise
        key = d.get("key")
        if not isinstance(key, str) or not key:
            self.audit(u, ip, "reveal", aid, kind, reason, "fail:empty")
            raise self.HttpError(502, "账户库没有回 key")
        self.audit(u, ip, "reveal", aid, kind, reason, "ok")
        self.log("管理后台 reveal", self.short(u["union_id"]), "account", aid, kind)
        return {"account_id": aid, "kind": kind, "hint": d.get("hint") or "", "key": key, "remask_after": 30}

    # ── 导出 ────────────────────────────────────────────────────────────
    COLS = (("id", "账户ID"), ("name", "姓名"), ("union_id", "union_id"), ("email_prefix", "邮箱前缀"), ("org", "组织"),
            ("depts", "部门"), ("type", "类型"), ("status", "状态"), ("status_raw", "状态原文"), ("alias", "key_alias"),
            ("masked", "key(末4位)"), ("secret_state", "库内形态"), ("applied_at", "申请时间(UTC)"))

    @staticmethod
    def _cell(v):
        s = "、".join(v) if isinstance(v, list) else ("" if v is None else str(v))
        s = _SK_ANY.sub("", s)                          # 打码形态 sk-…ABCD 也去掉前缀:导出里不出现 sk-
        if s[:1] in ("=", "+", "-", "@", "\t", "\r"):   # CSV 公式注入
            s = "'" + s
        return s

    def export(self, h, u, one):
        if not self.rl_export.hit(u["union_id"]):
            raise self.HttpError(429, "导出太频繁了,过一会儿再试")
        f = self.filters(one)
        rows, offset = [], 0
        while offset < EXPORT_MAX:
            d = self._store("GET", "/v1/accounts", {**f, "limit": 1000, "offset": offset, "order": "applied_desc"})
            if "total" not in d:
                raise self.HttpError(502, "账户库版本太旧(不支持分页),先升级账户库")
            items = d.get("items") or []
            rows += [self.view(r) for r in items]
            offset += len(items)
            if not items or offset >= d["total"]:
                break
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([c for _, c in self.COLS])
        for r in rows:
            r = self.mask_deep(r)
            w.writerow([self._cell(r.get(k)) for k, _ in self.COLS])
        b = ("﻿" + buf.getvalue()).encode("utf-8")        # BOM:Excel 打开中文不乱码
        if _SK_ANY.search(b.decode("utf-8")):                  # 最后一道:有 sk- 就不发(失败关闭)
            self.audit(u, h.client_ip(), "export", result="blocked:sk")
            raise self.HttpError(500, "导出内容没过打码检查,已拦下")
        self.audit(u, h.client_ip(), "export", result="ok:%d" % len(rows))
        self.log("管理后台导出", self.short(u["union_id"]), len(rows), "行")
        h.send_response(200)
        h.send_header("Content-Type", "text/csv; charset=utf-8")
        h.send_header("Content-Disposition", "attachment; filename=\"accounts-%s.csv\"" %
                      time.strftime("%Y%m%d-%H%M", time.localtime(self.app.now())))
        h.send_header("Content-Length", str(len(b)))
        h.send_header("Cache-Control", "no-store")
        h._common_headers()
        h.end_headers()
        h._write(b)
