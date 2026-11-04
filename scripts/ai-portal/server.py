#!/usr/bin/env python3
"""AI 自助门户后端:https://chat.auto-link.com.cn/ai/*(nginx 原样反代,不剥 /ai 前缀)

身份:飞书网页 OAuth(不依赖 Open WebUI)。主键 = user_info 里的 union_id
(同一开发商下跨应用、跨租户都一致)。跨租户用户查通讯录必 41050 ⇒ 只用登录拿到的 user_info,
本服务**从不调 /contact/**。

key:飞书多维表格 key 表,bot 的 tenant_access_token + user_id_type=union_id 按「姓名」精确过滤,
回来的每一行**服务端再验一遍归属**(人员格恰好一个人且 = 当前 union_id),不信过滤、不信前端传的 id。

只用 Python 标准库(风格同 scripts/feedback/feedback_server.py):http.server + sqlite3,systemd 跑。
配置:env 文件(--env 或 AI_PORTAL_ENV)+ 进程环境变量(进程环境优先)。SESSION_SECRET 没配就拒绝启动。
日志里不打印 key 明文 / token / code / state。
"""
import argparse, hashlib, hmac, html, http.server, importlib.util, json, mimetypes, os, re, secrets
import socketserver, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_feedback_module():
    """复用 scripts/feedback/feedback_server.py 的 KEY_RE / build_fields / text_of / CHOICES,别重抄。"""
    p = HERE.parent / "feedback" / "feedback_server.py"
    spec = importlib.util.spec_from_file_location("feedback_server", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


fb = _load_feedback_module()
KEY_RE = fb.KEY_RE
text_of = fb.text_of

# 🔴 SESSION_SECRET 故意不在这里:没有默认值。见 .work/ext-user-onboarding/05-redteam.md(硬编码默认密钥)。
DEFAULTS = {
    "LISTEN": "127.0.0.1:8793",
    "PUBLIC_ORIGIN": "https://chat.auto-link.com.cn",
    "BASE_PATH": "/ai",
    "STATIC_DIR": str(HERE / "static"),
    "DB_PATH": str(HERE / "ai_portal.db"),
    "FEISHU_APP_ID": "", "FEISHU_APP_SECRET": "",
    # 后端身份只能是 carher-236(app B);carher-1000 只有 view 权限,搜 key 表 total=0(数据组实测)
    "REQUIRE_APP_ID": "cli_aa8505afb0f89cb2",
    "PILOT_UNION_IDS": "",     # 灰度:非空=只有这些 union_id(逗号分隔)能登录;清空=对所有人开放
    "KEYS_PER_MIN": "30",      # 每人每分钟查 key 次数(前端 20 秒轮一次 ⇒ 正常只用到 3)
    "APPLY_PER_MIN": "3",      # 每人每分钟申请次数
    # OAuth:飞书网页 OAuth 只认本租户(外部公司/个人版报 20010)⇒ 只给内部人用;外部人走 magic link
    "FEISHU_OPEN_BASE": "https://open.feishu.cn/open-apis",
    "FEISHU_AUTHORIZE_URL": "https://accounts.feishu.cn/open-apis/authen/v1/authorize",
    "FEISHU_TOKEN_PATH": "/authen/v2/oauth/token",
    "FEISHU_USERINFO_PATH": "/authen/v1/user_info",
    "FEISHU_SCOPE": "",
    "OUR_TENANT_KEY": "1088406f1c155740",
    # key 表
    "KEY_BASE_TOKEN": "DlT9bsrwMad12VsogEpcK9Ptncc", "KEY_TABLE_ID": "tblJT2s6Y6xjYj5A",
    "KEY_PERSON_FIELD": "姓名", "KEY_FIELD": "API Key",
    "KEY_TYPE_FIELD": "账户类型", "KEY_STATUS_FIELD": "状态",
    "KEY_TIME_FIELD": "申请时间", "KEY_ORG_FIELD": "组织名称",
    # 客户批量 key 排除:组织名称精确命中 ⇒ 不返回。⚠️ 名单是数据组举的例子,要按表里真实选项补全
    "EXCLUDE_ORGS": "巧虎,美团,北汽研究院",
    "BATCH_MIN_ROWS": "5",      # 一串连续生成(相邻 ≤BATCH_GAP_MS)≥N 行 ⇒ 视为客户批量,整串不返回
    "BATCH_GAP_MS": "60000",    # 真表里批量是每 ~3 秒一把(最长一次 27 秒);本人手动申请两次相隔以天计
    # 申请:字段形状已定({"姓名":[{"id":on_}],"账户类型":["Cursor"]} + 可选 组织名称);默认 dry-run 不写表
    "APPLY_DRY_RUN": "1",
    "APPLY_ORG_NAME": "",       # 非空则写进「组织名称」;⚠️ 必须是表里已有的选项,本服务不校验
    "APPLY_LOCK_TTL": "86400",
    # 重置自己的 key(轮换)。没配 LITELLM_MASTER_KEY ⇒ 整个功能关闭(接口 501,前端退回「提反馈」)
    "LITELLM_MASTER_KEY": "",
    "LITELLM_BASE": "https://cc.auto-link.com.cn/pro",   # 同 poller:198 product,nginx 前缀 /pro
    "LITELLM_TIMEOUT": "60",
    "ROTATE_DRY_RUN": "1",          # 同 APPLY_DRY_RUN 的风格:只有显式 "0" 才真写
    "ROTATE_COOLDOWN": "600",       # 两次**成功**重置之间的最小间隔(秒);0 = 不限
    "ROTATE_LOCK_TTL": "300",       # 进行中锁的兜底过期:进程中途被杀时不至于永久锁住这个人
    "ROTATE_PER_MIN": "3",          # 每人每分钟的尝试次数(含失败和 dry-run)
    "ROTATE_PROBE": "1",            # 换表之前用新 key 真发一次推理;"0" 关掉(回读到字段 ≠ 能调)
    "ROTATE_PROBE_MAX_TOKENS": "16",
    "INSTALL_CODE_TTL": "600",
    "MAGIC_TTL": "600",
    "REDEEM_PER_IP_PER_MIN": "10",
    "REDEEM_GLOBAL_PER_MIN": "120",
    # 反馈:默认写 feedback_server 同一张「反馈」表
    "FEEDBACK_BASE_TOKEN": fb.CFG["BASE_TOKEN"], "FEEDBACK_TABLE_ID": fb.CFG["TABLE_ID"],
    "FEEDBACK_PER_MIN": "3",
    "SESSION_TTL": "43200",
    "STATE_TTL": "600",
    "TRUST_PROXY": "0",     # 1 = 对端是 127.0.0.1 时信 X-Real-IP(nginx 必须 proxy_set_header 覆盖它)
}
REQUIRED = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "SESSION_SECRET")
OPTIONAL_SECRETS = ("INTERNAL_SECRET",)    # 没配 ⇒ /api/internal/* 整个关闭(404)
APPLY_TYPE = "Cursor"                      # poller 不管写什么都落成 Cursor
TOOLS = ("cursor", "codex", "claude")      # 三个工具共用同一把 current 的 Cursor key
APPLY_FIELD_ALLOW = {"姓名", "账户类型", "组织名称"}   # API Key/open_id/邮箱前缀/状态/key_alias 一律不许写
NO_MATCH_CODES = {1254018}                 # InvalidFilter:假 id/截断 id/类型不匹配 ⇒ 当「查无记录」
MAX_BODY = 1024 * 1024     # 反馈带自检报告(前端上限 200KB,JSON 转义后会变大)


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def short(uid):
    """日志里只留 id 前缀(id 不是凭据,但没必要整串)。"""
    return (uid or "")[:10] + "…"


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ── 配置 ────────────────────────────────────────────────────────────────
class ConfigError(Exception):
    pass


def parse_env_file(path):
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out


def load_config(environ=None, env_file=None):
    environ = os.environ if environ is None else environ
    filev = parse_env_file(env_file) if env_file else {}
    keys = set(DEFAULTS) | set(REQUIRED) | set(OPTIONAL_SECRETS)
    cfg = {}
    for k in keys:
        cfg[k] = environ.get(k, filev.get(k, DEFAULTS.get(k, "")))
    return cfg


def check_config(cfg):
    errs = []
    for k in REQUIRED:
        if not (cfg.get(k) or "").strip():
            errs.append("缺 %s(没有默认值,必须在 env 文件里配)" % k)
    s = cfg.get("SESSION_SECRET") or ""
    if s and len(s) < 32:
        errs.append("SESSION_SECRET 太短(至少 32 字符,建议 python3 -c 'import secrets;print(secrets.token_urlsafe(48))')")
    req = (cfg.get("REQUIRE_APP_ID") or "").strip()
    if req and (cfg.get("FEISHU_APP_ID") or "").strip() and cfg["FEISHU_APP_ID"].strip() != req:
        errs.append("FEISHU_APP_ID 必须是 %s(carher-236);别的应用读 key 表会是 0 行" % req)
    isec = cfg.get("INTERNAL_SECRET") or ""
    if isec and len(isec) < 32:
        errs.append("INTERNAL_SECRET 太短(至少 32 字符)")
    if isec and isec == s:
        errs.append("INTERNAL_SECRET 不能和 SESSION_SECRET 相同")
    if not (cfg.get("BASE_PATH") or "").startswith("/") or cfg["BASE_PATH"].endswith("/"):
        errs.append("BASE_PATH 要形如 /ai")
    return errs


# ── 飞书 client(可注入;测试里指向假飞书)──────────────────────────────
class FeishuError(Exception):
    def __init__(self, where, code, msg):
        super().__init__("%s: code=%s msg=%s" % (where, code, msg))
        self.where, self.code, self.msg = where, code, msg


class FeishuClient:
    # tenant_access_token 失效/过期的错误码(⚠️ 按经验列的,线上见到新码再补)
    EXPIRED = {99991661, 99991663, 99991668, 99991677}

    def __init__(self, cfg, timeout=15):
        self.cfg = cfg
        self.base = cfg["FEISHU_OPEN_BASE"].rstrip("/")
        self.timeout = timeout
        self._tok, self._exp = "", 0.0
        self._lock = threading.Lock()

    def _http(self, method, url, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except ValueError:
                return {"code": e.code, "msg": "HTTP %d" % e.code}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FeishuError(urllib.parse.urlsplit(url).path, -1, type(e).__name__)

    # 用户侧 OAuth
    def authorize_url(self, state, redirect_uri):
        q = {"client_id": self.cfg["FEISHU_APP_ID"], "response_type": "code",
             "redirect_uri": redirect_uri, "state": state}
        if self.cfg.get("FEISHU_SCOPE"):
            q["scope"] = self.cfg["FEISHU_SCOPE"]
        return self.cfg["FEISHU_AUTHORIZE_URL"] + "?" + urllib.parse.urlencode(q)

    def exchange_code(self, code, redirect_uri):
        r = self._http("POST", self.base + self.cfg["FEISHU_TOKEN_PATH"], {
            "grant_type": "authorization_code", "client_id": self.cfg["FEISHU_APP_ID"],
            "client_secret": self.cfg["FEISHU_APP_SECRET"], "code": code, "redirect_uri": redirect_uri})
        d = r.get("data") if isinstance(r.get("data"), dict) else r
        if r.get("code") not in (0, None) or not d.get("access_token"):
            raise FeishuError("oauth/token", r.get("code"), r.get("msg") or r.get("error"))
        return d["access_token"]

    def user_info(self, user_access_token):
        r = self._http("GET", self.base + self.cfg["FEISHU_USERINFO_PATH"], token=user_access_token)
        # 实测 OWUI 看到的是平铺;飞书文档是 {code,data:{}} ⇒ 两种都认
        d = r.get("data") if isinstance(r.get("data"), dict) else r
        if r.get("code") not in (0, None) or not d.get("union_id"):
            raise FeishuError("user_info", r.get("code"), r.get("msg"))
        return d

    # bot 侧
    def tenant_token(self, force=False):
        with self._lock:
            if not force and self._tok and time.time() < self._exp - 120:
                return self._tok
            r = self._http("POST", self.base + "/auth/v3/tenant_access_token/internal",
                           {"app_id": self.cfg["FEISHU_APP_ID"], "app_secret": self.cfg["FEISHU_APP_SECRET"]})
            if not r.get("tenant_access_token"):
                raise FeishuError("tenant_access_token", r.get("code"), r.get("msg"))
            self._tok, self._exp = r["tenant_access_token"], time.time() + int(r.get("expire", 3600))
            return self._tok

    def bot(self, method, path, body=None):
        r = self._http(method, self.base + path, body, token=self.tenant_token())
        if r.get("code") in self.EXPIRED:      # token 被提前作废/过期 ⇒ 强刷一次再试
            r = self._http(method, self.base + path, body, token=self.tenant_token(force=True))
        if r.get("code") != 0:
            raise FeishuError(path.split("?")[0], r.get("code"), r.get("msg"))
        return r.get("data") or {}

    def search_by_person(self, app_token, table_id, field, union_id, max_pages=20):
        items, page_token = [], ""
        for _ in range(max_pages):
            q = {"user_id_type": "union_id", "page_size": "100"}
            if page_token:
                q["page_token"] = page_token
            try:
                d = self.bot("POST", "/bitable/v1/apps/%s/tables/%s/records/search?%s" % (
                    app_token, table_id, urllib.parse.urlencode(q)), {
                    "filter": {"conjunction": "and", "conditions": [
                        {"field_name": field, "operator": "is", "value": [union_id]}]},
                    "automatic_fields": False})
            except FeishuError as e:
                # 1254018 InvalidFilter = 假 id/截断 id/类型不匹配 ⇒ 查无记录;其它码照常抛(≠「没有 key」)
                if e.code in NO_MATCH_CODES:
                    log("key 表搜索 InvalidFilter,按查无记录处理", short(union_id), e.code)
                    return []
                raise
            items += d.get("items") or []
            if not d.get("has_more") or not d.get("page_token"):
                break
            page_token = d["page_token"]
        return items

    def create_record(self, app_token, table_id, fields):
        d = self.bot("POST", "/bitable/v1/apps/%s/tables/%s/records?user_id_type=union_id" % (app_token, table_id),
                     {"fields": fields})
        return d.get("record") or {}


# ── 纯函数(单测直接打)────────────────────────────────────────────────
def mask_key(k):
    """sk-…后4位。太短的不露尾巴(露 4 位就等于露一半)。"""
    k = (k or "").strip()
    if not k:
        return ""
    head = "sk-…" if k.startswith("sk-") else "…"
    return head + k[-4:] if len(k) >= 12 else head


SK_RE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")     # 比 feedback 的 KEY_RE 更宽一点:短 key 也打


def mask_deep(v):
    """整行任意字段里的 sk- 明文都打码(体检链接 #k=<明文>、Cursor Key、Claude Code Key…)。"""
    if isinstance(v, str):
        return SK_RE.sub(lambda m: mask_key(m.group(0)), v)
    if isinstance(v, list):
        return [mask_deep(x) for x in v]
    if isinstance(v, dict):
        return {k: mask_deep(x) for k, x in v.items()}
    return v


def front_status(raw, key):
    """表里状态原值 → 前端词表(按前缀,数据组实测口径)。

    🔴 11-04 对表里**真实的 16 个选项**复核时补了三个:「重新生成中」「待生成（第三方合作伙伴）」
    「待生成（第三方合作伙伴补录）」。原来只认「生成中」开头 ⇒ 这三个原样透给前端,
    前端按"未知状态"置灰,而用户其实是在等 key ⇒ 页面不轮询、也不提示「留在页面等」,看着像坏了。
    (「重新生成中」不以「生成中」开头,前缀判断恰好漏掉它。)"""
    s = (raw or "").strip()
    if (s.startswith("生成中") or s.startswith("重新生成中") or s.startswith("待生成")
            or key == "__provisioning__"):
        return "生成中"
    if s.startswith("有效") or s.startswith("已完成"):
        return "可用"
    if s.startswith("已禁用"):
        return "已禁用"
    return s        # 「生成失败: <原因>」原样带原因;其它原样


def to_ms(v):
    """申请时间:飞书 datetime/created_time 是毫秒时间戳;也容错数字串。"""
    if isinstance(v, (int, float)):
        return int(v)
    t = text_of(v).strip()
    return int(float(t)) if re.fullmatch(r"\d+(\.\d+)?", t) else 0


def person_ids(cell):
    if not isinstance(cell, list):
        return []
    return [x.get("id") for x in cell if isinstance(x, dict) and x.get("id")]


def owner_ok(cell, union_id):
    """归属判据:人员格恰好一个成员,且就是他。多人格不算谁的(避免「和别人共用一行」被当成自己的)。

    🔴 11-04 红队:原来先把**没有 id 的成员过滤掉**再数个数 ⇒ 判的其实是「有 id 的人只有一个」。
    于是 `[我, {"name":"张三"}]`(拿不到 union_id 的成员,跨租户的人就可能是这形状)会被判成
    「只属于我」⇒ 两个人共用的那一行连明文一起给了我。
    ⇒ **先数这一格有几个成员,再比 id**。缺 id 的成员也算一个人,有它就不是独占。
    (现量:真表 1452 行里人员格全是单成员、无缺 id —— 所以这条现在不可触发;
     但「飞书对拿不到 union_id 的成员回什么形状」没有实测过,不能赌它永远不出现。)"""
    if not isinstance(cell, list) or len(cell) != 1 or not union_id:
        return False
    m = cell[0]
    return isinstance(m, dict) and m.get("id") == union_id


def as_list(cell):
    if isinstance(cell, list):
        return [text_of(x) if not isinstance(x, str) else x for x in cell]
    t = text_of(cell)
    return [t] if t else []


def safe_next(nxt, base="/ai"):
    """登录回跳只许本站 base 下的相对路径;其余一律回 base/。"""
    default = base + "/"
    if not isinstance(nxt, str) or not nxt or len(nxt) > 512:
        return default
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in nxt):
        return default
    p = urllib.parse.urlsplit(nxt)
    if p.scheme or p.netloc or not nxt.startswith(base + "/") or nxt.startswith("//"):
        return default
    if ".." in urllib.parse.unquote(p.path).split("/"):
        return default
    return nxt


def parse_cookies(header):
    out = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k and k not in out:
            out[k] = v
    return out


class RateLimiter:
    def __init__(self, limit, window, now):
        self.limit, self.window, self.now = int(limit), window, now
        self.hits, self.lock = {}, threading.Lock()

    def _window(self, key):
        t = self.now()
        h = [x for x in self.hits.get(key, []) if t - x < self.window]
        self.hits[key] = h
        return t, h

    def _forget_if_idle(self, key):
        """窗口内一次都没有了就把 key 删掉:常驻进程里每个 IP / union_id 都留一个条目,
        只增不减会慢慢涨内存(11-04 红队 P1-4)。
        🔴 不能在 _window 里直接 pop —— hit() 拿到的是局部 list,pop 之后再 append 就记不进库里,
        所有限流会**全部失效**(我第一版就这么写的,8 条限流测试当场变红)。所以只在确认不再 append 时清。"""
        if not self.hits.get(key):
            self.hits.pop(key, None)

    def hit(self, key):
        """记一次;超限返回 False。(redeem 用:每次尝试都算,不管对错)"""
        with self.lock:
            t, h = self._window(key)
            ok = len(h) < self.limit
            if ok:
                h.append(t)
            else:
                self._forget_if_idle(key)
            return ok

    def allowed(self, key):
        with self.lock:
            n = len(self._window(key)[1])
            self._forget_if_idle(key)
            return n < self.limit

    def record(self, key):
        """只记成功的(feedback 用,同 feedback_server:填错表单不扣次数)。"""
        with self.lock:
            t, h = self._window(key)
            h.append(t)


# ── 应用 ────────────────────────────────────────────────────────────────
SCHEMA = """
create table if not exists sessions(sid_hash text primary key, user text not null, created real, expires real);
create table if not exists oauth_states(state_hash text primary key, next text, created real, expires real, used real);
create table if not exists install_codes(code_hash text primary key, union_id text not null, tool text,
    account_type text, created real, expires real, used real);
create table if not exists apply_locks(union_id text, account_type text, created real, primary key(union_id, account_type));
create table if not exists magic_tokens(token_hash text primary key, user text not null, created real, expires real,
    used real);
create table if not exists reveal_audit(id integer primary key autoincrement, at real, union_id text, name text,
    record_id text, ip text, login text, result text);
"""
REC_ID = re.compile(r"^rec[A-Za-z0-9]{1,40}$")
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"    # base32:不分大小写、不含 0/1/8/9 易混字符
CODE_LEN = 32                                         # 32 × 5 bit = 160 bit
SID_COOKIE, STATE_COOKIE = "ai_sid", "ai_oauth"
STATIC_EXT = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
              ".woff", ".woff2", ".txt", ".map"}


class HttpError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


class MagicError(HttpError):
    def __init__(self, status, code, msg):
        super().__init__(status, msg)
        self.code = code


class App:
    def __init__(self, cfg, client=None, now=time.time):
        errs = check_config(cfg)
        if errs:
            raise ConfigError(";".join(errs))
        self.cfg, self.now = cfg, now
        self.base = cfg["BASE_PATH"]
        self.origin = cfg["PUBLIC_ORIGIN"].rstrip("/")
        self.client = client or FeishuClient(cfg)
        self.redirect_uri = self.origin + self.base + "/auth/callback"
        secret = cfg["SESSION_SECRET"].encode()
        # 不同用途派生不同 key,一个用途的签名不能拿去冒充另一个
        self._k_sess = hmac.new(secret, b"ai-portal/session", hashlib.sha256).digest()
        self._k_code = hmac.new(secret, b"ai-portal/install-code", hashlib.sha256).digest()
        self._k_state = hmac.new(secret, b"ai-portal/oauth-state", hashlib.sha256).digest()
        self._k_magic = hmac.new(secret, b"ai-portal/magic-link", hashlib.sha256).digest()
        self.internal_secret = (cfg.get("INTERNAL_SECRET") or "").strip()
        self.db = sqlite3.connect(cfg["DB_PATH"], check_same_thread=False, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.dblock = threading.Lock()
        self.tools = TOOLS
        self.exclude_orgs = {t.strip() for t in cfg["EXCLUDE_ORGS"].split(",") if t.strip()}
        self.batch_min = int(cfg["BATCH_MIN_ROWS"])
        self.batch_gap_ms = int(cfg["BATCH_GAP_MS"])
        self.rl_magic_ip = RateLimiter(cfg["REDEEM_PER_IP_PER_MIN"], 60, now)
        self.rl_redeem_ip = RateLimiter(cfg["REDEEM_PER_IP_PER_MIN"], 60, now)
        self.rl_redeem_all = RateLimiter(cfg["REDEEM_GLOBAL_PER_MIN"], 60, now)
        self.rl_issue = RateLimiter(5, 60, now)
        # 11-04 红队 P1-1:/api/keys 和 /api/apply 原来一点限流都没有,而它们每次都真打飞书接口
        # (用的是全租户共享的 tenant token)⇒ 打满会连带 key poller、反馈服务一起受影响;
        # 而且前端本身每 20 秒轮询一次。按人限流,比上限宽松得多、够正常使用。
        self.rl_keys = RateLimiter(cfg["KEYS_PER_MIN"], 60, now)
        self.rl_apply = RateLimiter(cfg["APPLY_PER_MIN"], 60, now)
        self.rl_feedback = RateLimiter(cfg["FEEDBACK_PER_MIN"], 60, now)

    def q(self, sql, args=(), one=False):
        with self.dblock:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall()
            return (rows[0] if rows else None) if one else (rows, cur.rowcount)

    def _h(self, key, v):
        return hmac.new(key, v.encode(), hashlib.sha256).hexdigest()

    # 会话:cookie = sid.HMAC(sid);库里只存 HMAC(sid) ⇒ 库泄露也拿不到能用的 cookie
    def pilot_ok(self, user):
        """灰度名单(逗号分隔 union_id);空=对所有人开放。建会话和每次带会话的请求都过这一道。"""
        pilot = {x.strip() for x in (self.cfg.get("PILOT_UNION_IDS") or "").split(",") if x.strip()}
        return not pilot or (user.get("union_id") in pilot)

    def new_session(self, user):
        # 灰度闸:OAuth 和一次性登录链接两条路**都只经过这里**建会话 ⇒ 闸放在这一处就同时管住两条路。
        # 不在名单里的人照样能走完飞书授权,只是拿不到会话 —— 网页未上线前别人进不来,也不会建出孤儿会话。
        if not self.pilot_ok(user):
            log("灰度闸拒绝登录", short(user.get("union_id") or ""))
            raise HttpError(403, "这个页面还在内测,暂时只对少数人开放。请先继续用 https://chat.auto-link.com.cn/oneclick/")
        sid = secrets.token_urlsafe(32)
        t = self.now()
        self.q("insert into sessions values(?,?,?,?)",
               (self._h(self._k_sess, sid), json.dumps(user, ensure_ascii=False), t, t + int(self.cfg["SESSION_TTL"])))
        return sid + "." + self._h(self._k_sess, "sig|" + sid)

    def session_user(self, cookie_header):
        v = parse_cookies(cookie_header).get(SID_COOKIE, "")
        sid, _, sig = v.partition(".")
        if not sid or not sig or not hmac.compare_digest(sig, self._h(self._k_sess, "sig|" + sid)):
            return None
        row = self.q("select user, expires from sessions where sid_hash=?", (self._h(self._k_sess, sid),), one=True)
        if not row or row[1] < self.now():
            return None
        u = json.loads(row[0])
        # 11-04 红队 P1-5:灰度闸原来只在**建会话**时判 ⇒ 把名单收紧后,之前已经登录的人
        # 还能拿着旧 cookie 用满 12 小时(实测还能签安装码)。每次请求都过一遍闸,收紧即生效。
        if not self.pilot_ok(u):
            return None
        return u

    def drop_session(self, cookie_header):
        sid = parse_cookies(cookie_header).get(SID_COOKIE, "").partition(".")[0]
        if sid:
            self.q("delete from sessions where sid_hash=?", (self._h(self._k_sess, sid),))

    # OAuth
    def login_start(self, nxt):
        state = secrets.token_urlsafe(32)
        t = self.now()
        # 顺手清过期行(state/会话/安装码),免得表无限长
        self.q("delete from oauth_states where expires<?", (t - 3600,))
        self.q("delete from sessions where expires<?", (t,))
        self.q("delete from install_codes where expires<?", (t - 86400,))
        self.q("insert into oauth_states values(?,?,?,?,null)",
               (self._h(self._k_state, state), safe_next(nxt, self.base), t, t + int(self.cfg["STATE_TTL"])))
        return state, self.client.authorize_url(state, self.redirect_uri)

    def login_finish(self, code, state, cookie_header):
        """state 两道校验:①和本浏览器的 ai_oauth cookie 一致(防登录 CSRF/会话注入)②服务端有、没过期、没用过。"""
        browser = parse_cookies(cookie_header).get(STATE_COOKIE, "")
        if not state or not browser or not hmac.compare_digest(state, browser):
            raise HttpError(400, "登录状态不匹配,请从登录按钮重新进入")
        sh = self._h(self._k_state, state)
        _, n = self.q("update oauth_states set used=? where state_hash=? and used is null and expires>?",
                      (self.now(), sh, self.now()))
        if n != 1:
            raise HttpError(400, "登录链接已失效,请重新登录")
        nxt = self.q("select next from oauth_states where state_hash=?", (sh,), one=True)[0]
        if not code:
            raise HttpError(400, "飞书没有返回授权码(可能点了拒绝)")
        try:
            uat = self.client.exchange_code(code, self.redirect_uri)
            info = self.client.user_info(uat)
        except FeishuError as e:
            log("登录失败", e.where, e.code)
            raise HttpError(502, "飞书登录失败,请重试")
        user = {
            "union_id": info["union_id"], "open_id": info.get("open_id") or "",
            "tenant_key": info.get("tenant_key") or "", "name": info.get("name") or "",
            "avatar": info.get("avatar_url") or info.get("avatar_thumb") or "",
            "email": info.get("email") or info.get("enterprise_email") or "",
        }
        user = self.session_shape(user, "oauth")
        log("登录", "oauth", short(user["union_id"]), "外部" if user["external"] else "内部")
        return self.new_session(user), nxt

    def session_shape(self, u, login):
        """OAuth 和 magic 落成同一种会话结构;记上登录方式和 app_id。"""
        out = {k: str(u.get(k) or "") for k in ("union_id", "open_id", "tenant_key", "name", "avatar", "email")}
        # tenant_key 不是我们的 ⇒ 外部人。**空也算外部**(失败关闭):magic link 签发时如果没传
        # tenant_key,宁可把内部人标成"外部合作方"(只影响页面上那个标签),也不要把外部人标成内部。
        # ⇒ 机器人签发时务必把事件里的 tenant_key 一起传过来。
        out["external"] = out["tenant_key"] != self.cfg["OUR_TENANT_KEY"]
        out["login"] = login
        out["app_id"] = self.cfg["FEISHU_APP_ID"]
        return out

    # 一次性登录链接(外部人唯一入口:飞书网页 OAuth 只认本租户,外部报 20010)
    def internal_ok(self, auth_header):
        """机器人侧调用的内部接口。没配 INTERNAL_SECRET ⇒ 整个关闭(调用方见 404)。"""
        if not self.internal_secret:
            raise HttpError(404, "not found")
        got = (auth_header or "")
        got = got[7:] if got.startswith("Bearer ") else ""
        if not got or not hmac.compare_digest(got.encode(), self.internal_secret.encode()):
            raise HttpError(401, "内部密钥不对")

    def issue_magic(self, body):
        uid = body.get("union_id")
        if not isinstance(uid, str) or not re.fullmatch(r"on_[A-Za-z0-9]{8,64}", uid):
            raise HttpError(400, "union_id 不对")
        for k in ("open_id", "tenant_key", "name"):
            if body.get(k) is not None and not isinstance(body.get(k), str):
                raise HttpError(400, "%s 要是字符串" % k)
        user = self.session_shape({"union_id": uid, "open_id": (body.get("open_id") or "")[:80],
                                   "tenant_key": (body.get("tenant_key") or "")[:80],
                                   "name": (body.get("name") or "")[:80]}, "magic")
        tok = secrets.token_urlsafe(32)          # 256 bit;字符集落在前端 MAGIC_T_RE 里
        t, ttl = self.now(), int(self.cfg["MAGIC_TTL"])
        self.q("delete from magic_tokens where expires<?", (t - 86400,))
        self.q("insert into magic_tokens values(?,?,?,?,null)",
               (self._h(self._k_magic, tok), json.dumps(user, ensure_ascii=False), t, t + ttl))
        log("magic 签发", short(uid), "外部" if user["external"] else "内部")
        return {"url": self.origin + self.base + "/auth/magic?" + urllib.parse.urlencode({"t": tok}),
                "expires_in": ttl}

    def consume_magic(self, body, ip):
        """只有 POST 消费。错误码 expired/used/invalid 给前端查表(LOGIN_ERR)。"""
        if not self.rl_magic_ip.hit(ip):
            raise MagicError(429, "invalid", "尝试太频繁,稍后再试")
        tok = body.get("t")
        if not isinstance(tok, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", tok):
            raise MagicError(400, "invalid", "链接不对")
        th = self._h(self._k_magic, tok)
        row = self.q("select user, expires, used from magic_tokens where token_hash=?", (th,), one=True)
        if not row:
            log("magic 拒绝 invalid", ip)
            raise MagicError(400, "invalid", "链接不对")
        if row[2] is not None:
            log("magic 拒绝 used", ip)
            raise MagicError(410, "used", "链接已经用过了")
        if row[1] <= self.now():
            log("magic 拒绝 expired", ip)
            raise MagicError(410, "expired", "链接过期了")
        _, n = self.q("update magic_tokens set used=? where token_hash=? and used is null and expires>?",
                      (self.now(), th, self.now()))
        if n != 1:                                  # 并发:只有一个赢
            raise MagicError(410, "used", "链接已经用过了")
        user = json.loads(row[0])
        log("登录", "magic", short(user["union_id"]), "外部" if user["external"] else "内部")
        return self.new_session(user)

    # key 表
    def my_rows(self, user):
        """本人的行(已排除客户批量 key),每行带前端状态和 current 标记。所有行字段已按 sk- 整行打码进 masked_fields。"""
        c = self.cfg
        items = self.client.search_by_person(c["KEY_BASE_TOKEN"], c["KEY_TABLE_ID"], c["KEY_PERSON_FIELD"],
                                             user["union_id"])
        rows = []
        for it in items:
            f = it.get("fields") or {}
            if not owner_ok(f.get(c["KEY_PERSON_FIELD"]), user["union_id"]):
                continue        # 过滤器漏过来的别人的行:服务端再验一遍归属
            key = text_of(f.get(c["KEY_FIELD"])).strip()
            raw_status = text_of(f.get(c["KEY_STATUS_FIELD"])).strip()
            rows.append({"record_id": it.get("record_id") or "", "key": key, "raw_status": raw_status,
                         "status": front_status(raw_status, key), "types": as_list(f.get(c["KEY_TYPE_FIELD"])),
                         "org": text_of(f.get(c["KEY_ORG_FIELD"])).strip(), "at": to_ms(f.get(c["KEY_TIME_FIELD"]))})
        rows = self.drop_batches(rows, user)
        cand = [r for r in rows if r["raw_status"].startswith("有效") and "Cursor" in r["types"]
                and r["key"].startswith("sk-")]
        cur = max(cand, key=lambda r: (r["at"], r["record_id"])) if cand else None
        for r in rows:
            r["current"] = r is cur
        return rows

    def drop_batches(self, rows, user):
        """客户批量 key 不算本人的:①组织名称命中 EXCLUDE_ORGS ②一串**连续**生成的行(相邻间隔 ≤BATCH_GAP_MS)
        合计 ≥BATCH_MIN_ROWS 行。

        🔴 11-04 拿真表复核时推翻了原来的「同一自然分钟 ≥N 行」:
        - 自然分钟有边界。刘国现名下 10 把零跑汽车客户 key 每 3 秒一把,**跨了分钟边界**被切成 8+2,
          尾巴那 2 把(cursor-leapmotor-9/-10)每桶都 <5 ⇒ 漏过过滤,而且因为最新,
          **被选成了"刘国现的当前 key"** —— 页面上给他看的是客户的 key。按间隔聚类就是一串 10 行。
        - 时间为 0 / 空的行(早年导入,没有申请时间)以前同落「第 0 分钟」,
          凑满 5 行就被当成批量整组藏掉(刘国现那 5 行恰好是他本人的 Cursor/Claude/CarHer key)
          ⇒ 没时间的行不参与聚类。"""
        timed = sorted((r for r in rows if r["at"]), key=lambda r: r["at"])
        batch_ids, run = set(), []
        for r in timed + [None]:                      # 末尾哨兵:把最后一串结算掉
            if r is not None and run and r["at"] - run[-1]["at"] <= self.batch_gap_ms:
                run.append(r)
                continue
            if len(run) >= self.batch_min:
                batch_ids.update(id(x) for x in run)
            run = [r] if r is not None else []
        keep = []
        for r in rows:
            if r["org"] in self.exclude_orgs or id(r) in batch_ids:
                continue
            keep.append(r)
        if len(keep) != len(rows):
            log("排除客户批量 key", short(user["union_id"]), len(rows) - len(keep), "行")
        return keep

    def keys(self, user):
        """形状对齐前端(static/index.html loadKeys):{keys:[{record_id, account_type, masked, status, current}]}。
        status 可能是 poller 写的「生成失败: <原因>」原文 ⇒ 整行再过一遍 sk- 打码。"""
        if not self.rl_keys.hit(user["union_id"]):
            raise HttpError(429, "刷得太快了,过一会儿再看")
        out = [{"record_id": r["record_id"], "account_type": "、".join(r["types"]), "account_types": r["types"],
                "status": r["status"], "current": r["current"],
                "masked": "" if r["status"] == "生成中" else mask_key(r["key"])} for r in self.my_rows(user)]
        return mask_deep({"keys": out})

    def audit_reveal(self, user, rid, ip, result):
        self.q("insert into reveal_audit(at, union_id, name, record_id, ip, login, result) values(?,?,?,?,?,?,?)",
               (self.now(), user["union_id"], user.get("name") or "", rid, ip, user.get("login") or "", result))

    def reveal(self, user, body, ip=""):
        rid = body.get("record_id")
        if not isinstance(rid, str) or not REC_ID.match(rid):
            raise HttpError(400, "record_id 不对")
        for r in self.my_rows(user):            # 只在「他自己的行」里找,不按 id 直接取
            if r["record_id"] != rid:
                continue
            if r["status"] == "已禁用" or not r["key"].startswith("sk-"):
                self.audit_reveal(user, rid, ip, "denied:" + ("disabled" if r["status"] == "已禁用" else "nokey"))
                log("reveal 拒绝(已禁用/无 key)", short(user["union_id"]), rid)
                raise HttpError(403, "这把 key 已停用或还没生成好,不能显示")
            self.audit_reveal(user, rid, ip, "ok")
            log("reveal", short(user["union_id"]), rid)
            return {"record_id": rid, "key": r["key"]}
        self.audit_reveal(user, rid, ip, "denied:not-owner")
        log("reveal 拒绝", short(user["union_id"]), rid)
        raise HttpError(404, "没有这条记录")   # 别人的行和不存在的行同一个回答,不泄露存在性

    def current_key(self, user):
        for r in self.my_rows(user):
            if r["current"]:
                return r
        return None

    # 申请
    def apply_fields(self, user):
        fields = {"姓名": [{"id": user["union_id"]}], "账户类型": [APPLY_TYPE]}
        if self.cfg["APPLY_ORG_NAME"].strip():
            fields["组织名称"] = self.cfg["APPLY_ORG_NAME"].strip()
        assert set(fields) <= APPLY_FIELD_ALLOW
        return fields

    def apply(self, user, body):
        at = body.get("account_type", APPLY_TYPE)
        if at != APPLY_TYPE:            # 先验参数再记次数:填错不该扣额度(照 feedback 的口径)
            raise HttpError(400, "账户类型只能是 Cursor(三个工具共用这一把)")
        if not self.rl_apply.hit(user["union_id"]):
            raise HttpError(429, "申请太频繁了,过一分钟再试")
        rows = self.my_rows(user)               # 写之前先精确查一遍
        cur = next((r for r in rows if r["current"]), None)
        if cur:
            log("apply 已有 current,不写", short(user["union_id"]))
            return {"ok": True, "existing": True, "record_id": cur["record_id"], "dry_run": False}
        pend = next((r for r in rows if r["status"] == "生成中"), None)
        if pend:
            return {"ok": True, "pending": True, "record_id": pend["record_id"], "dry_run": False}
        fields = self.apply_fields(user)
        if self.cfg["APPLY_DRY_RUN"] != "0":
            log("apply dry-run", short(user["union_id"]))
            return {"ok": True, "dry_run": True, "fields": fields}
        # 本地锁:表格搜索有延迟,连点两下时第二次可能还搜不到第一次写的行
        t = self.now()
        self.q("delete from apply_locks where created<?", (t - int(self.cfg["APPLY_LOCK_TTL"]),))
        try:
            self.q("insert into apply_locks values(?,?,?)", (user["union_id"], APPLY_TYPE, t))
        except sqlite3.IntegrityError:
            return {"ok": True, "pending": True, "dry_run": False}
        try:
            rec = self.client.create_record(self.cfg["KEY_BASE_TOKEN"], self.cfg["KEY_TABLE_ID"], fields)
        except FeishuError as e:
            self.q("delete from apply_locks where union_id=? and account_type=?", (user["union_id"], APPLY_TYPE))
            log("apply 写表失败", short(user["union_id"]), e.code)
            raise HttpError(502, "提交失败,稍后再试")
        log("apply", short(user["union_id"]), rec.get("record_id"))
        return {"ok": True, "dry_run": False, "record_id": rec.get("record_id")}

    def rotate(self, user, body):
        # poller 没有轮换路径;清空 API Key 会让新旧两把同时有效 ⇒ 先不做
        raise HttpError(501, "需要换 key 请提反馈")

    # 一次性安装码
    def issue_code(self, user, body):
        tool = body.get("tool")
        if tool not in self.tools:
            raise HttpError(400, "tool 只能是:" + "、".join(self.tools))
        if not self.rl_issue.hit(user["union_id"]):
            raise HttpError(429, "太频繁了,一分钟后再试")
        if not self.current_key(user):
            raise HttpError(404, "你还没有可用的 key,先申请")
        raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        # 8 个一组用 - 连接:前端 CODE_RE=^[A-Za-z0-9][A-Za-z0-9-]{3,63}$,会原样拼进 shell 命令
        code = "-".join(raw[i:i + 8] for i in range(0, CODE_LEN, 8))
        t, ttl = self.now(), int(self.cfg["INSTALL_CODE_TTL"])
        self.q("insert into install_codes values(?,?,?,?,?,?,null)",
               (self._h(self._k_code, raw), user["union_id"], tool, APPLY_TYPE, t, t + ttl))
        log("install-code 签发", short(user["union_id"]), tool)
        return {"code": code, "expires_in": ttl, "tool": tool}

    def redeem(self, body, ip):
        if not (self.rl_redeem_ip.hit(ip) and self.rl_redeem_all.hit("*")):
            raise HttpError(429, "尝试太频繁,稍后再试")
        code = body.get("code")
        raw = code.replace("-", "").strip().upper() if isinstance(code, str) else ""
        if len(raw) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in raw):
            raise HttpError(400, "安装码无效或已过期")
        ch = self._h(self._k_code, raw)
        row = self.q("select union_id, account_type, tool from install_codes where code_hash=? and used is null and expires>?",
                     (ch, self.now()), one=True)
        if not row:
            log("redeem 拒绝", ip)
            raise HttpError(400, "安装码无效或已过期")   # 错码/过期/用过 同一个回答
        try:
            r = self.current_key({"union_id": row[0]})
        except FeishuError as e:
            log("redeem 查表失败", e.code)            # 不作废码:飞书抖一下让他重试
            raise HttpError(502, "查询失败,稍后再试")
        if not r:
            raise HttpError(404, "这个账号下没有可用的 key")
        _, n = self.q("update install_codes set used=? where code_hash=? and used is null and expires>?",
                      (self.now(), ch, self.now()))
        if n != 1:                                      # 并发换取:只有一个赢
            raise HttpError(400, "安装码无效或已过期")
        log("redeem", short(row[0]), row[2])
        # 🔴 11-04 联调时发现:签发时存了 tool,换取时却没回 ⇒ 安装器
        #    (installer/install.sh:113)拿不到 tool 就 die「安装服务没说要装哪个工具」⇒ **每一次安装都在第一步失败**。
        #    两边各自的测试都是绿的(各自对着自己的假对端),只有接起来才看得见。
        # 不回网关地址:两个安装器都不读它(grep 零命中),网关地址写在子安装器自己的 BASE_URL 里
        # (codex-oneclick/src/install-mac.sh:17、claude-oneclick/src/install-mac.sh:20,还带旧地址兜底逻辑)。
        # 回一个没人读的字段 = 让下一个人以为"改服务端就能换网关",实际改了不生效。
        return {"key": r["key"], "account_type": APPLY_TYPE, "tool": row[2]}

    # 反馈
    @staticmethod
    def feedback_form(body):
        """前端字段 {type, tool, title, detail, report, os, page} → feedback_server 的表单形状。
        自检报告并进详细描述(反馈表没有单独的列);打码和截断由 build_fields 统一做。"""
        if any(k in body for k in ("类型", "工具")):        # 也接受 feedback_server 原形状
            return body
        detail = str(body.get("detail") or "")
        report = str(body.get("report") or "")
        if report:
            detail = (detail + "\n\n—— 自检报告 ——\n" + report).strip()
        return {"title": body.get("title"), "类型": body.get("type"), "工具": body.get("tool"),
                "电脑": body.get("os"), "detail": detail, "source": body.get("page")}

    def feedback(self, user, body):
        if not self.rl_feedback.allowed(user["union_id"]):
            raise HttpError(429, "提交太频繁,一分钟后再试")
        if body.get("images"):
            raise HttpError(400, "这里暂不支持截图,请在反馈页提交")
        form = self.feedback_form(body)
        try:
            fields = fb.build_fields(form, user["email"])
        except ValueError as e:
            raise HttpError(400, str(e))
        tag = "\n\n—— 提交人:%s(%s)" % (user["name"], "外部" if user["external"] else "内部")
        fields["详细描述"] = (fields["详细描述"] + tag)[:20000]
        c = self.cfg
        try:
            rec = self.client.create_record(c["FEEDBACK_BASE_TOKEN"], c["FEEDBACK_TABLE_ID"],
                                            dict(fields, 提交人=[{"id": user["union_id"]}]))
        except FeishuError as e:
            # ⚠️ 外部人的 union_id 能不能写进人员格未实测;写不进就去掉人员格再写一次(名字已在描述里)
            log("feedback 人员格写入失败,去掉人员格重试", e.code)
            try:
                rec = self.client.create_record(c["FEEDBACK_BASE_TOKEN"], c["FEEDBACK_TABLE_ID"], fields)
            except FeishuError as e2:
                log("feedback 写表失败", e2.code)
                raise HttpError(502, "提交失败,稍后再试")
        self.rl_feedback.record(user["union_id"])
        log("feedback", short(user["union_id"]), rec.get("record_id"))
        return {"ok": True, "id": rec.get("record_id")}


# ── HTTP ────────────────────────────────────────────────────────────────
class H(http.server.BaseHTTPRequestHandler):
    server_version = "ai-portal"
    sys_version = ""
    app = None          # make_server 里绑定

    def log_message(self, fmt, *a):
        pass            # 默认访问日志会带 query(code/state),整个关掉

    # 输出
    def _common_headers(self, referrer="same-origin"):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer)
        self.send_header("X-Frame-Options", "DENY")

    def send_json(self, code, obj, cookies=()):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self._common_headers()
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, loc, cookies=()):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self._common_headers()
        self.end_headers()

    def send_text(self, code, text):
        b = ("<!doctype html><meta charset=utf-8><title>AI 自助</title><p>%s</p><p><a href=\"%s/\">返回</a></p>"
             % (html.escape(text), self.app.base)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(b)

    def cookie(self, name, value, path, max_age):
        return "%s=%s; Path=%s; Max-Age=%d; HttpOnly; Secure; SameSite=Lax" % (name, value, path, max_age)

    # 输入
    def client_ip(self):
        peer = self.client_address[0]
        if self.app.cfg["TRUST_PROXY"] == "1" and peer in ("127.0.0.1", "::1"):
            return (self.headers.get("X-Real-IP") or peer).strip()[:64]
        return peer

    def same_origin(self, required):
        """浏览器跨站 POST 一定带 Origin;required=False 只给无 cookie 的 redeem(脚本不带 Origin)。"""
        o = self.headers.get("Origin")
        if o is not None:
            return o == self.app.origin
        if required:
            ref = self.headers.get("Referer") or ""
            return ref.startswith(self.app.origin + self.app.base + "/")
        return True

    def body_json(self):
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise HttpError(415, "要 application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "Content-Length 不对")
        if n > MAX_BODY:
            raise HttpError(413, "内容太大")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise HttpError(400, "不是合法 JSON")
        if not isinstance(d, dict):
            raise HttpError(400, "要 JSON 对象")
        return d

    def user(self):
        u = self.app.session_user(self.headers.get("Cookie"))
        if not u:
            raise HttpError(401, "未登录")
        return u

    def sub(self):
        """/ai/xxx ⇒ /xxx;不在 /ai 下 ⇒ None。"""
        p = urllib.parse.urlsplit(self.path).path
        b = self.app.base
        if p == b:
            return ""
        return p[len(b):] if p.startswith(b + "/") else None

    # 路由
    def do_GET(self):
        self._run(self._get)

    def do_POST(self):
        self._run(self._post)

    def _run(self, fn):
        try:
            fn()
        except MagicError as e:
            self.send_json(e.status, {"error": e.code, "message": e.msg})
        except HttpError as e:
            if e.status == 401:
                return self.send_json(401, {"login": False, "error": e.msg, "message": e.msg})
            self.send_json(e.status, {"error": e.msg, "message": e.msg})   # 前端读 message
        except FeishuError as e:
            log("飞书出错", e.where, e.code)
            self.send_json(502, {"error": "飞书接口暂时不可用,稍后再试", "message": "飞书接口暂时不可用,稍后再试"})
        except Exception as e:                   # 不打 str(e):可能带请求内容
            log("未处理异常", type(e).__name__, self.command, urllib.parse.urlsplit(self.path).path)
            self.send_json(500, {"error": "服务出错", "message": "服务出错"})

    def _get(self):
        p, a = self.sub(), self.app
        if p is None:
            raise HttpError(404, "not found")
        if p == "":
            return self.redirect(a.base + "/")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if p == "/healthz":
            return self.send_json(200, {"ok": True})
        if p == "/auth/login":
            # 前端用 ?redirect=,也认 ?next=
            state, url = a.login_start((q.get("redirect") or q.get("next") or [""])[0])
            return self.redirect(url, [self.cookie(STATE_COOKIE, state, a.base + "/auth/", int(a.cfg["STATE_TTL"]))])
        if p == "/auth/callback":
            clear = self.cookie(STATE_COOKIE, "", a.base + "/auth/", 0)
            try:
                sid, nxt = a.login_finish((q.get("code") or [""])[0], (q.get("state") or [""])[0],
                                          self.headers.get("Cookie"))
            except HttpError as e:
                self.send_response(e.status)        # 页面形式回给浏览器,顺手清掉 state cookie
                b = ("<!doctype html><meta charset=utf-8><p>%s</p><p><a href=\"%s/auth/login\">重新登录</a></p>"
                     % (html.escape(e.msg), a.base)).encode()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Set-Cookie", clear)
                self._common_headers()
                self.end_headers()
                return self.wfile.write(b)
            return self.redirect(nxt, [clear, self.cookie(SID_COOKIE, sid, a.base + "/", int(a.cfg["SESSION_TTL"]))])
        if p == "/auth/magic":
            # 🔴 GET 只出页面,绝不消费 token(飞书链接预览/安全扫描会先 GET 一次)
            return self.static("/index.html", referrer_none=True)
        if p == "/auth/logout":
            # 前端是 GET 跳转式退出。登出 CSRF 危害只是「被登出」,接受;Lax cookie 在顶层跳转时会带上
            a.drop_session(self.headers.get("Cookie"))
            return self.redirect(safe_next((q.get("redirect") or [""])[0], a.base),
                                 [self.cookie(SID_COOKIE, "", a.base + "/", 0)])
        if p == "/api/me":
            u = self.user()
            return self.send_json(200, {"login": True, **{k: u.get(k, "") for k in (
                "union_id", "open_id", "tenant_key", "name", "avatar", "email", "external", "login")},
                "tenant": u.get("tenant_key", ""),
                "account_types": [APPLY_TYPE], "tools": list(a.tools), "apply_dry_run": a.cfg["APPLY_DRY_RUN"] != "0",
                "feedback_choices": fb.CHOICES})
        if p == "/api/keys":
            return self.send_json(200, a.keys(self.user()))
        if p.startswith("/api/"):
            raise HttpError(404, "not found")
        return self.static(p)

    def _post(self):
        p, a = self.sub(), self.app
        if p is None or not (p.startswith("/api/") or p in ("/auth/logout", "/auth/magic")):
            raise HttpError(404, "not found")
        if p.startswith("/api/internal/"):
            # 机器人调用:服务端到服务端,不看 Origin,只认 INTERNAL_SECRET
            a.internal_ok(self.headers.get("Authorization"))
            if p == "/api/internal/magic-link":
                return self.send_json(200, a.issue_magic(self.body_json()))
            raise HttpError(404, "not found")
        public = p == "/api/install-code/redeem"
        if not self.same_origin(required=not public):
            raise HttpError(403, "来源不对")
        if p == "/auth/magic":
            if self.headers.get("X-Requested-With") != "ai-portal":
                raise MagicError(403, "invalid", "来源不对")
            sid = a.consume_magic(self.body_json(), self.client_ip())
            return self.send_json(200, {"ok": True}, [self.cookie(SID_COOKIE, sid, a.base + "/", int(a.cfg["SESSION_TTL"]))])
        if p == "/auth/logout":
            a.drop_session(self.headers.get("Cookie"))
            return self.send_json(200, {"ok": True}, [self.cookie(SID_COOKIE, "", a.base + "/", 0)])
        if public:
            return self.send_json(200, a.redeem(self.body_json(), self.client_ip()))
        routes = {"/api/apply": a.apply, "/api/keys/rotate": a.rotate,
                  "/api/install-code": a.issue_code, "/api/feedback": a.feedback}
        if p != "/api/keys/reveal" and p not in routes:
            raise HttpError(404, "not found")
        u = self.user()
        if p == "/api/keys/reveal":
            return self.send_json(200, a.reveal(u, self.body_json(), self.client_ip()))
        return self.send_json(200, routes[p](u, self.body_json()))

    def static(self, p, referrer_none=False):
        root = Path(self.app.cfg["STATIC_DIR"]).resolve()
        rel = urllib.parse.unquote(p).lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        f = (root / rel).resolve()
        if (root not in f.parents or not f.is_file() or any(x.startswith(".") for x in Path(rel).parts)
                or f.suffix.lower() not in STATIC_EXT):     # static/ 里有 dev_mock.py,不许当静态文件出
            raise HttpError(404, "not found")
        b = f.read_bytes()
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store" if referrer_none else
                         ("no-cache" if f.name == "index.html" else "public, max-age=300"))
        self._common_headers(referrer="no-referrer" if referrer_none else "same-origin")
        self.end_headers()
        self.wfile.write(b)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def make_server(app, host="127.0.0.1", port=0):
    handler = type("BoundH", (H,), {"app": app})
    return Server((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI 自助门户后端")
    ap.add_argument("--env", default=os.environ.get("AI_PORTAL_ENV"), help="env 文件路径")
    args = ap.parse_args(argv)
    cfg = load_config(env_file=args.env)
    errs = check_config(cfg)
    if errs:
        print("拒绝启动:" + ";".join(errs), file=sys.stderr)
        return 2
    app = App(cfg)
    if cfg["APPLY_DRY_RUN"] != "0":
        log("APPLY_DRY_RUN 开着:申请只返回将要写的字段,不写表")
    host, port = cfg["LISTEN"].rsplit(":", 1)
    srv = make_server(app, host, int(port))
    log("listening", cfg["LISTEN"], "base", cfg["BASE_PATH"])
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
