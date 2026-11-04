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
import argparse, base64, copy, hashlib, hmac, html, http.server, importlib.util, json, mimetypes, os, re, secrets
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
    # 外部人能不能自助申请 —— 和 APPLY_DRY_RUN **分开**的开关(红队 P2-4:别让一个 env 同时打开两件事)。
    # 11-04 用户定:「可以让外部人申请,但是他要登录飞书。后续再加固,先不要堵住缺口」⇒ 默认开。
    # 门槛 = 必须登录(apply 走 self.user(),没会话就 401);外部人的会话只能来自机器人签发的一次性
    # 登录链接 ⇒ 身份是飞书给的、不可伪造。已知风险:飞书个人版注册成本低,可被批量刷 ——
    # 需要收紧时设 0,外部人申请改为登记待审核(audit_apply_request),不用改代码。
    "APPLY_ALLOW_EXTERNAL": "1",
    "APPLY_ORG_NAME": "",       # 非空则写进「组织名称」;⚠️ 必须是表里已有的选项,本服务不校验
    "APPLY_LOCK_TTL": "86400",
    # 重置自己的 key(轮换)。没配 LITELLM_MASTER_KEY ⇒ 整个功能关闭(接口 501,前端退回「提反馈」)
    "LITELLM_MASTER_KEY": "",
    "LITELLM_BASE": "https://chat.auto-link.com.cn/model",   # 198 product 的新地址(cc…/pro 是旧地址,共存期后会停)
    "LITELLM_TIMEOUT": "60",
    "ROTATE_DRY_RUN": "1",          # 同 APPLY_DRY_RUN 的风格:只有显式 "0" 才真写
    "ROTATE_COOLDOWN": "600",       # 两次**成功**重置之间的最小间隔(秒);0 = 不限
    "ROTATE_LOCK_TTL": "300",       # 进行中锁的兜底过期:进程中途被杀时不至于永久锁住这个人
    "ROTATE_PER_MIN": "3",          # 每人每分钟的尝试次数(含失败和 dry-run)
    "ROTATE_PROBE": "1",            # 换表之前用新 key 真发一次推理;"0" 关掉(回读到字段 ≠ 能调)
    "ROTATE_PROBE_MAX_TOKENS": "16",
    "INSTALL_CODE_TTL": "600",
    "MAGIC_TTL": "600",
    # 11-04 红队 P1-2:两道闸都**只数失败**(错码/格式错/过期/用过)。
    #   安装码 160 bit,撞库靠的是失败次数;成功的换取要先有一个真码,真码由 issue_code 按人限流签出来。
    #   原来全局闸每次尝试都算 ⇒ 12 个 IP 各打 10 个错码就能把全公司的安装卡住 1 分钟。
    #   ⚠️ 198 上 nginx 没配 realip 时所有人的 IP 都是前置代理那一个(实测 10.68.13.97)⇒「每 IP」= 全局。
    "REDEEM_PER_IP_PER_MIN": "10",      # 每 IP 每分钟允许失败几次;到了 ⇒ 这个 IP 一律 429(真码也不判,不给对/错信号)
    "REDEEM_GLOBAL_PER_MIN": "120",     # 全站每分钟失败数的告警线;超了 ⇒ 每 IP 只许失败 1 次,**没失败过的 IP 照常换**
    "MY_ROWS_TTL": "15",                # 「我的 key」列表的进程内缓存秒数(只给 GET /api/keys 和签码用;0=关)
    # 反馈:默认写 feedback_server 同一张「反馈」表
    "FEEDBACK_BASE_TOKEN": fb.CFG["BASE_TOKEN"], "FEEDBACK_TABLE_ID": fb.CFG["TABLE_ID"],
    "FEEDBACK_PER_MIN": "3",
    # 11-04 红队 P2-6:12h → 4h。登录本来就是一键;cookie 被偷后的可用窗口跟着缩。
    # 封号要立刻踢人用 `server.py --env … --kill-sessions <union_id>`(见 kill_user)。
    "SESSION_TTL": "14400",
    "STATE_TTL": "600",
    "TRUST_PROXY": "0",     # 1 = 对端是 127.0.0.1 时信 X-Real-IP(nginx 必须 proxy_set_header 覆盖它)
}
REQUIRED = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "SESSION_SECRET")
OPTIONAL_SECRETS = ("INTERNAL_SECRET",)    # 没配 ⇒ /api/internal/* 整个关闭(404)
APPLY_TYPE = "Cursor"                      # poller 不管写什么都落成 Cursor
OAUTH_EXTERNAL_MSG = "这个按钮只认无锡车联(auto-link)的飞书账号"
# 外部人待审核申请 —— 写进反馈行「详细描述」给管理员看的处理步骤(最小可行审核流程,见 README「外部人」一节)
APPLY_REVIEW_HOWTO = (
    "union_id=%s\ntenant_key=%s\n\n"
    "审核通过:在 key 表新建一行 —— 「姓名」选这个人(人员格,按 union_id)、「账户类型」=Cursor、"
    "「组织名称」选他公司(表里已有的选项),API Key 留空。poller 约 1 分钟内发 key;"
    "他刷新 https://chat.auto-link.com.cn/ai/ 就能看到并一行命令装好(poller 的群通知外部人收不到,别指望它)。\n"
    "不通过:把本条状态改成已解决,处理说明写原因。\n"
    "(门户自动登记;同一人 24 小时内只登记一次。没进反馈表时运维可在 198 上跑 "
    "server.py --list-apply-requests 看本地登记)")
TOOLS = ("cursor", "codex", "claude")      # 三个工具共用同一把 current 的 Cursor key
# 11-04 用户要求「一条命令配置和装三个 app,同时也支持分开安装」。
# 形状选 **tool="all"**(而不是 tool 收数组、也不是一次签三个码):
#   ① 一个码装三个 = 用户只复制一行。签三个码要么让他贴三行,要么让安装器连换三次(码是一次性的,
#      换第二次就 400),两条都不行。
#   ② 本来就是**同一把 key** 发给三个工具(TOOLS 注释那句),所以 "all" 只是收货人变了,不涉及多把 key。
#   ③ 数组形状(tool:["codex","claude"])会让「装哪几个」变成 2^3 种组合:库里那一列、安装器的
#      校验、前端的卡片、补装话术全要跟着分叉,而产品上只有「装一个」和「装三个」两种诉求。
#   ④ 库里 install_codes.tool 是一列文本,"all" 直接存得下,不改表。
ISSUABLE = TOOLS + ("all",)                # /api/install-code 能签的;redeem 原样回这一列
APPLY_FIELD_ALLOW = {"姓名", "账户类型", "组织名称"}   # API Key/open_id/邮箱前缀/状态/key_alias 一律不许写
# 重置时允许写的列(白名单;姓名/账户类型/组织名称/open_id/邮箱前缀 一律不动 —— 重置不改归属)
ROTATE_FIELD_ALLOW = {"API Key", "状态", "key_alias", "点我体检", "Cursor Key", "Claude Code Key"}
ROTATE_STATUS = "有效（已重新生成）"   # 🔴 必须是表里**已有**的选项(单选列写新值会被拒/造脏选项)
KEY_PROBE_FIELD = "点我体检"           # 超链接列,不跟着 API Key 自动走(见 keyprobe/backfill_lark_link.py)
KEYPROBE_PAGE = "https://chat.auto-link.com.cn/keyprobe/"
KEYPROBE_TEXT = "▶ 测这把 key"
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
    mk = (cfg.get("LITELLM_MASTER_KEY") or "").strip()
    if mk and (mk == s or mk == isec):
        errs.append("LITELLM_MASTER_KEY 不能和 SESSION_SECRET / INTERNAL_SECRET 相同")
    # 真写模式必须有 master key,否则页面上出着真按钮、点下去只能 501
    if mk == "" and (cfg.get("ROTATE_DRY_RUN") or "1") == "0":
        errs.append("ROTATE_DRY_RUN=0 但没配 LITELLM_MASTER_KEY(重置 key 要调 198 网关)")
    for k in ("ROTATE_COOLDOWN", "ROTATE_LOCK_TTL", "ROTATE_PER_MIN", "ROTATE_PROBE_MAX_TOKENS", "LITELLM_TIMEOUT"):
        v = (cfg.get(k) or "").strip()
        if not v.isdigit():
            errs.append("%s 要是非负整数(现在是 %r)" % (k, v))
    base = (cfg.get("LITELLM_BASE") or "").strip()
    if base and not base.startswith("https://"):
        # master key 会放在 Authorization 头里发出去 ⇒ 明文 http 不许(本机回环除外,给测试)
        if not base.startswith("http://127.0.0.1") and not base.startswith("http://localhost"):
            errs.append("LITELLM_BASE 必须是 https://(master key 走这条连接)")
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

    def update_record(self, app_token, table_id, record_id, fields):
        """逐字段覆盖:只发这几个字段,别人手改过的其它列不动(同 keyprobe/backfill_lark_link.py)。"""
        d = self.bot("PUT", "/bitable/v1/apps/%s/tables/%s/records/%s?user_id_type=union_id" % (
            app_token, table_id, record_id), {"fields": fields})
        return d.get("record") or {}


# ── 198 LiteLLM client(可注入;测试里用假的)────────────────────────────
class LiteLLMError(Exception):
    def __init__(self, where, status, msg):
        super().__init__("%s: HTTP %s %s" % (where, status, msg))
        self.where, self.status, self.msg = where, status, msg


class LiteLLMClient:
    """只用得到的四个口。master key 只在 Authorization 头里,不进日志、不进响应。

    🔴 读 key 必须用 `/key/info?key=<token 的 sha256>`:
    - `/key/info?key_alias=` 在 198 上返回 **HTTP 200 + 所有字段 null**(实测,见
      scripts/litellm-198-key-fallback.py 的注释)—— 什么都不抛,下游把 None 当 []
      就会"跑得很顺、什么都没改"。
    - `/key/list?key_alias=` 能回全行,但得先信飞书表里的 key_alias 那一列;
      我们手上有用户**真正在用的明文**,按它的 sha256 查才是对的归属判据。
    """

    def __init__(self, cfg, timeout=None):
        self.base = cfg["LITELLM_BASE"].rstrip("/")
        self.mk = (cfg.get("LITELLM_MASTER_KEY") or "").strip()
        self.timeout = int(timeout or cfg["LITELLM_TIMEOUT"])

    @property
    def enabled(self):
        return bool(self.mk)

    def _http(self, method, path, body=None, auth=None, timeout=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        h = {"Authorization": "Bearer " + (auth or self.mk)}
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw.decode("utf-8", "replace")[:300]}
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise LiteLLMError(path.split("?")[0], -1, type(e).__name__)

    def key_info(self, token_sha):
        """返回那一行(dict),不存在返回 None。token 列本身被接口置空,我们自己已经有。"""
        st, d = self._http("GET", "/key/info?key=" + urllib.parse.quote(token_sha, safe=""))
        if st == 404:
            return None
        if st != 200:
            raise LiteLLMError("/key/info", st, _lit_msg(d))
        info = (d or {}).get("info")
        if not isinstance(info, dict) or not info:
            return None
        return info

    def generate(self, payload):
        st, d = self._http("POST", "/key/generate", payload)
        key = (d or {}).get("key")
        if st != 200 or not isinstance(key, str) or not key.startswith("sk-"):
            raise LiteLLMError("/key/generate", st, _lit_msg(d))
        return d

    def update(self, payload):
        st, d = self._http("POST", "/key/update", payload)
        if st != 200:
            raise LiteLLMError("/key/update", st, _lit_msg(d))
        return d or {}

    def delete_by_alias(self, alias):
        st, d = self._http("POST", "/key/delete", {"key_aliases": [alias]})
        if st != 200:
            raise LiteLLMError("/key/delete", st, _lit_msg(d))
        return d or {}

    def probe(self, plaintext, model, max_tokens):
        """用新 key 真发一发。判据只看「认证和权限过了没」⇒ 有 choices 就算过。

        🔴 不要求 content 非空:菜单里多是推理模型,max_tokens 给小了会被 reasoning token
        吃光,返回 200 但 content="" —— 那是探针自己饿死,不是 key 不能用
        (见 .work/ext-user-onboarding/15-key-lifecycle.md §6)。真正能分辨好坏的是 401/403。
        """
        st, d = self._http("POST", "/v1/chat/completions", {
            "model": model, "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": "ping"}]}, auth=plaintext)
        if st == 200 and isinstance((d or {}).get("choices"), list) and d["choices"]:
            return True, "200 model=%s" % model
        return False, "HTTP %s %s" % (st, _lit_msg(d))


def _lit_msg(d):
    """错误信封里的人话;**不回显整包**(请求体可能被回显,里面有 payload)。"""
    if not isinstance(d, dict):
        return ""
    e = d.get("error")
    if isinstance(e, dict):
        return str(e.get("message") or e.get("type") or "")[:200]
    return str(e or d.get("message") or d.get("detail") or d.get("raw") or "")[:200]


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


# ── 重置 key 用的纯函数 ──────────────────────────────────────────────────
# alias 基名:只剥 poller 加的那种 4 位 36 进制后缀(Date.now().toString(36).slice(-4),
# litellm-key-poller.js:689),同 poller 自己的 loadPrefixOwners 正则。
# 🔴 不能无脑 rsplit("-",1):真表里有 `cursor-buyitian` 这种**没有后缀**的 alias,
# 剥一刀就变成 `cursor`,新 key 会叫 `cursor-xxxx` —— 名字里的人没了,还可能撞别人。
ALIAS_SUFFIX_RE = re.compile(r"^(.+?)-[a-z0-9]{4}$")
ALIAS_OK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/@]{0,253}[A-Za-z0-9]$")   # LiteLLM 的严格形状

# 逐字段平移(同 scripts/litellm-198-key-reissue.py 的 CARRY_COLS,那套在 127 把真 key 上跑过)。
# 只平移**非空**的字段:空的发过去可能被服务端当成"显式清空"。
# 故意不抄:token/key_name(新发)、key_alias(换后缀)、spend/model_spend(从 0 起)、
# blocked(重置的目的就是给一把能用的)、created_*/updated_*/budget_reset_at、rotation_*/auto_rotate。
ROTATE_CARRY = ("user_id", "team_id", "organization_id", "models", "aliases", "metadata",
                "max_budget", "budget_duration", "soft_budget_cooldown", "tpm_limit", "rpm_limit",
                "tpd_limit", "max_parallel_requests", "model_max_budget", "permissions",
                "allowed_cache_controls", "allowed_routes", "config", "budget_id", "project_id",
                "router_settings", "policies", "key_type")
# 回读必须一致的字段:"建出来了" 和 "闸装上了/权限对了" 是两件事
ROTATE_ASSERT = ("models", "max_budget", "budget_duration", "team_id", "user_id")
# 探针模型从**这把 key 自己的 models** 里挑,不按 alias 前缀猜:两个 key 族模型名前缀是反的
# (cursor-* 裸名 claude-haiku-4-5,claude-code-* 带 anthropic. 前缀),猜错拿到的 403 是量具错。
PROBE_PREF = ("claude-haiku-4-5", "anthropic.claude-haiku-4-5", "deepseek-v4.1-flash",
              "claude-haiku-5-5", "anthropic.claude-haiku-5-5")


def alias_base(alias):
    m = ALIAS_SUFFIX_RE.match((alias or "").strip())
    return m.group(1) if m else (alias or "").strip()


def new_alias(alias, suffix):
    return "%s-%s" % (alias_base(alias), suffix)


def retired_alias(alias, ts):
    """旧 key 改名用:把规范 alias 腾出来,审计行留在主表里。"""
    return "%s-retired-%s" % (alias_base(alias), ts)


def pick_probe_model(models):
    ms = [m for m in (models or []) if isinstance(m, str) and m]
    if not ms or any(m.startswith("all-") for m in ms):
        return None     # all-team-models / all-proxy-models:一个词,解析只对 team 成员生效,探不出来
    for p in PROBE_PREF:
        if p in ms:
            return p
    cheap = [m for m in ms if "haiku" in m or "flash" in m or "mini" in m]
    return (cheap or ms)[0]


def empty_val(v):
    return v is None or v == "" or v == [] or v == {}


def rotate_payload(old, alias, now_iso):
    """新 key 的 /key/generate 请求体:老行的非空字段逐个抄过来 + 本次重置的记账。"""
    p = {"key_alias": alias}
    for c in ROTATE_CARRY:
        v = old.get(c)
        if not empty_val(v):
            p[c] = v
    md = dict(p.get("metadata") or {})
    md["base_alias"] = alias_base(alias)
    md["rotated_from"] = old.get("key_alias") or ""
    md["rotated_at"] = now_iso
    md["rotated_by"] = "ai-portal/self-service"
    p["metadata"] = md
    return p


def carry_mismatch(old, new):
    """回读比对。只报真漂移:老的为空、新的被服务端补上(key_type 恒 default、
    organization_id 按 team 补齐)不算漏平移 —— 这两条在换发脚本上报过假红。"""
    bad = []
    for c in ROTATE_ASSERT:
        ov, nv = old.get(c), new.get(c)
        if empty_val(ov) and empty_val(nv):
            continue
        if empty_val(ov) and not empty_val(nv):
            continue
        if json.dumps(ov, sort_keys=True) != json.dumps(nv, sort_keys=True):
            bad.append(c)
    return bad


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
        """只记该记的那次(feedback 记成功的,同 feedback_server:填错表单不扣次数;redeem 记失败的)。"""
        with self.lock:
            t, h = self._window(key)
            h.append(t)

    def count(self, key):
        with self.lock:
            n = len(self._window(key)[1])
            self._forget_if_idle(key)
            return n


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
-- 重置 key:进行中锁(主键 = 一个人只能有一把在转)+ 审计。两张表都**不存明文**,只存 alias。
create table if not exists rotate_locks(union_id text primary key, record_id text, created real);
-- 安装码换 key(P1-3):出事时查「这把 key 装到了哪台机器 / 哪个 IP」。**不存明文**:
-- key_sha = sha256(明文)(和 LiteLLM 的 token 列 / SpendLogs.api_key 同一个值,能直接 join 网关用量),
-- code_id = 安装码 HMAC 的前 16 位(能 join install_codes.code_hash 查是谁什么时候签的)。
create table if not exists redeem_audit(id integer primary key autoincrement, at real, union_id text, tool text,
    record_id text, key_sha text, key_masked text, code_id text, ip text, ua text, result text);
create index if not exists redeem_audit_key on redeem_audit(key_sha);
create index if not exists redeem_audit_uid on redeem_audit(union_id, at);
create table if not exists rotate_audit(id integer primary key autoincrement, at real, union_id text, name text,
    record_id text, ip text, login text, step text, result text, old_alias text, new_alias text, detail text);
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
    def __init__(self, cfg, client=None, now=time.time, litellm=None):
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
        self.issuable = ISSUABLE
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
        self.rl_rotate = RateLimiter(cfg["ROTATE_PER_MIN"], 60, now)
        self.rl_feedback = RateLimiter(cfg["FEEDBACK_PER_MIN"], 60, now)
        self.litellm = litellm or LiteLLMClient(cfg)
        self._rows_cache, self._rows_lock = {}, threading.Lock()     # my_rows 的按人缓存

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

    def kill_user(self, union_id):
        """P2-6 运维入口:踢掉这个人的**全部**会话,顺带作废他手上还没用的安装码和一次性登录链接
        (安装码不需要会话就能换明文,只删会话等于没踢干净)。返回各删了几条。"""
        if not isinstance(union_id, str) or not re.fullmatch(r"on_[A-Za-z0-9]{8,64}", union_id):
            raise ValueError("union_id 要形如 on_xxx")
        rows, _ = self.q("select sid_hash, user from sessions")
        sids = []
        for sh, u in rows:
            try:
                if json.loads(u).get("union_id") == union_id:
                    sids.append(sh)
            except ValueError:
                continue
        for sh in sids:
            self.q("delete from sessions where sid_hash=?", (sh,))
        _, n_code = self.q("delete from install_codes where union_id=? and used is null", (union_id,))
        mrows, _ = self.q("select token_hash, user from magic_tokens where used is null")
        n_magic = 0
        for th, u in mrows:
            try:
                if json.loads(u).get("union_id") == union_id:
                    self.q("delete from magic_tokens where token_hash=?", (th,))
                    n_magic += 1
            except ValueError:
                continue
        self.drop_rows_cache(union_id)
        log("踢人", short(union_id), "会话", len(sids), "安装码", n_code, "登录链接", n_magic)
        return {"sessions": len(sids), "install_codes": n_code, "magic_tokens": n_magic}

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
            if e.code == 20010:
                # 20010「用户无应用使用权限」= 外部公司 / 飞书个人版点了内部登录按钮(网页 OAuth 只认本租户)。
                # 原来回一句「飞书登录失败,请重试」⇒ 他会一直重试一个永远不会成功的按钮。
                raise HttpError(403, OAUTH_EXTERNAL_MSG)
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
    # 11-04 红队 P1-1 补充:按 union_id 的进程内缓存(MY_ROWS_TTL 秒)。
    #   只有「看列表 / 签安装码」吃缓存;**交明文或写表的路径(reveal / redeem / apply / rotate)一律 fresh=True**,
    #   而且它们做完都会 drop_rows_cache(uid) ⇒ 重置后页面下一次轮询必看到新 key,管理员停用后
    #   最多 MY_ROWS_TTL 秒后列表跟上,但在那之前也已经交不出明文(reveal/redeem 不吃缓存)。
    def my_rows(self, user, fresh=False):
        uid = user["union_id"]
        ttl = int(self.cfg.get("MY_ROWS_TTL") or 0)
        if not fresh and ttl > 0:
            with self._rows_lock:
                hit = self._rows_cache.get(uid)
                if hit and self.now() - hit[0] < ttl:
                    return copy.deepcopy(hit[1])
        rows = self._my_rows_uncached(user)
        if ttl > 0:
            with self._rows_lock:
                t = self.now()
                if len(self._rows_cache) > 2000:        # 常驻进程别只增不减(同 P1-4)
                    for k in [k for k, v in self._rows_cache.items() if t - v[0] >= ttl]:
                        del self._rows_cache[k]
                self._rows_cache[uid] = (t, copy.deepcopy(rows))
        return rows

    def drop_rows_cache(self, uid):
        with self._rows_lock:
            self._rows_cache.pop(uid, None)

    def _my_rows_uncached(self, user):
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
                         "org": text_of(f.get(c["KEY_ORG_FIELD"])).strip(), "at": to_ms(f.get(c["KEY_TIME_FIELD"])),
                         # 重置要跟着换的两类副本列。只在这里原样带着,/api/keys 出去前整体打码。
                         "probe_link": bool(f.get(KEY_PROBE_FIELD)),
                         "extra_keys": {k: text_of(f.get(k)).strip() for k in ("Cursor Key", "Claude Code Key")
                                        if f.get(k)}})
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
        # 交明文 ⇒ 不吃缓存(停用后不能再显示),做完也让这个人的缓存失效
        self.drop_rows_cache(user["union_id"])
        for r in self.my_rows(user, fresh=True):  # 只在「他自己的行」里找,不按 id 直接取
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

    def current_key(self, user, fresh=False):
        for r in self.my_rows(user, fresh=fresh):
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

    def audit_apply_request(self, user):
        """外部人申请待审核:记一行本地审计 + 尽量写进反馈表(管理员在那里看)。写反馈表失败不影响回给用户的话。"""
        self.q("create table if not exists apply_requests(union_id text, name text, tenant_key text, at real)")
        if not self.q("select 1 from apply_requests where union_id=? and at>?", (user["union_id"], self.now() - 86400), one=True):
            self.q("insert into apply_requests values(?,?,?,?)",
                   (user["union_id"], user.get("name") or "", user.get("tenant_key") or "", self.now()))
            try:
                # 🔴 11-04 外部旅程实测:原来写的 type="其他" 不在反馈表「类型」选项里(只有「其他建议」)
                #    ⇒ build_fields 抛 ValueError ⇒ 被下面的 except 吞掉 ⇒ **每一条待审核申请都没进反馈表**,
                #    管理员永远看不到,外部人永远等不到。
                self.feedback(user, {"type": "其他建议", "tool": "其他",
                                     "title": "外部合作方申请 key:%s" % (user.get("name") or user["union_id"][:12]),
                                     "detail": APPLY_REVIEW_HOWTO % (user["union_id"], user.get("tenant_key") or "")})
            except Exception as e:      # noqa: BLE001 —— 登记失败不能让用户看到报错
                log("外部申请写反馈表失败", short(user["union_id"]), type(e).__name__)
        log("外部申请待审核", short(user["union_id"]))

    def list_apply_requests(self, days=30):
        self.q("create table if not exists apply_requests(union_id text, name text, tenant_key text, at real)")
        rows, _ = self.q("select union_id, name, tenant_key, at from apply_requests where at>? order by at desc",
                         (self.now() - days * 86400,))
        return rows

    def apply_review_at(self, user):
        """外部人最近 7 天内登记过的待审核申请时间(秒),没有就 0。页面刷新后据此显示「已登记、等审核」,
        而不是再出一个申请按钮让他以为没提交上。key 发下来以后页面有 current key,这条自然不再显示。"""
        if not user.get("external"):
            return 0
        self.q("create table if not exists apply_requests(union_id text, name text, tenant_key text, at real)")
        row = self.q("select max(at) from apply_requests where union_id=? and at>?",
                     (user["union_id"], self.now() - 7 * 86400), one=True)
        return int((row or [0])[0] or 0)

    def apply(self, user, body):
        at = body.get("account_type", APPLY_TYPE)
        if at != APPLY_TYPE:            # 先验参数再记次数:填错不该扣额度(照 feedback 的口径)
            raise HttpError(400, "账户类型只能是 Cursor(三个工具共用这一把)")
        if not self.rl_apply.hit(user["union_id"]):
            raise HttpError(429, "申请太频繁了,过一分钟再试")
        if user.get("external") and self.cfg["APPLY_ALLOW_EXTERNAL"] != "1":
            # 外部人:不写表(写了 poller 会直接发 key),只登记一条待审核,管理员在反馈表里看得到
            self.audit_apply_request(user)
            return {"ok": True, "review": True,
                    "message": "已收到申请。外部合作方的 key 需要管理员审核,通过后会出现在这个页面上(一般 1 个工作日内)"}
        rows = self.my_rows(user, fresh=True)   # 写之前先精确查一遍(不吃缓存)
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
        self.drop_rows_cache(user["union_id"])  # 新写了一行「生成中」⇒ 页面马上要看到,好开始轮询
        log("apply", short(user["union_id"]), rec.get("record_id"))
        return {"ok": True, "dry_run": False, "record_id": rec.get("record_id")}

    # 重置自己的 key(轮换)
    def audit_rotate(self, user, rid, ip, step, result, old_alias="", new_alias="", detail=""):
        """🔴 只记 alias / 步骤 / 结果,不记任何明文(detail 再过一遍 sk- 打码兜底)。"""
        self.q("insert into rotate_audit(at, union_id, name, record_id, ip, login, step, result, old_alias,"
               " new_alias, detail) values(?,?,?,?,?,?,?,?,?,?,?)",
               (self.now(), user["union_id"], user.get("name") or "", rid, ip, user.get("login") or "",
                step, result, old_alias, new_alias, mask_deep(str(detail))[:500]))

    def rotate_cooldown_left(self, union_id):
        """上一次**成功**重置离现在还差多少秒(0 = 可以重置)。失败和 dry-run 不计入。"""
        cd = int(self.cfg["ROTATE_COOLDOWN"])
        if cd <= 0:
            return 0
        row = self.q("select max(at) from rotate_audit where union_id=? and result='ok'", (union_id,), one=True)
        last = (row or [None])[0]
        return 0 if not last else max(0, int(cd - (self.now() - last)))

    def rotate_lock(self, user, rid):
        """进行中锁:主键 = union_id ⇒ 同一人并发只有一个 insert 成功。"""
        t = self.now()
        self.q("delete from rotate_locks where created<?", (t - int(self.cfg["ROTATE_LOCK_TTL"]),))
        try:
            self.q("insert into rotate_locks values(?,?,?)", (user["union_id"], rid, t))
            return True
        except sqlite3.IntegrityError:
            return False

    def rotate_unlock(self, union_id):
        self.q("delete from rotate_locks where union_id=?", (union_id,))

    def rotate(self, user, body, ip=""):
        """把这个人**当前那把** key 换成新的。

        顺序是定死的:**先生成新的 → 回读确认 → 真发一发 → 写表 → 最后才封旧的**。
        理由是只有这个顺序能保证「绝不出现旧的废了、新的没给他」:
        - 任何一步失败,用户手里那把**原封不动还能用**,表也没动 ⇒ 重试即可。
        - 新 key 建出来但后面失败 ⇒ 把新的删掉再报错(不留两把都活着的状态)。
        - 封旧 key 放最后:到这一步表里已经是新 key,就算封禁调用失败,用户是好的,
          只剩一把该封没封的旧 key —— 这是**可以事后补**的,而反过来不行。

        旧 key 的处置是**改名 + blocked=true**,不是删除:
        - 改名把规范 alias 腾出来,审计行留在主表里(`/key/delete` 是硬删,只往
          DeletedVerificationToken 另存);blocked 可以解封 ⇒ 万一这次重置有问题还能救回来。
        - 要彻底删,由运维对 `*-retired-*` 那批另做一轮(自助入口不做不可逆的事)。
        """
        rid_in = body.get("record_id")
        if rid_in is not None and not (isinstance(rid_in, str) and REC_ID.match(rid_in)):
            raise HttpError(400, "record_id 不对")
        if not self.litellm.enabled:
            # 没配网关 master key ⇒ 功能整个关闭。前端看到 501 就退回「提反馈」那条路。
            raise HttpError(501, "需要换 key 请提反馈")
        if not self.rl_rotate.hit(user["union_id"]):
            raise HttpError(429, "操作太频繁了,过一分钟再试")
        left = self.rotate_cooldown_left(user["union_id"])
        if left:
            raise HttpError(429, "刚刚才重置过。%s 后才能再重置一次(这样做是为了避免连点生成一堆 key)"
                                 % ("%d 分钟" % ((left + 59) // 60) if left >= 60 else "%d 秒" % left))
        cur = self.current_key(user, fresh=True)   # 🔴 只按登录身份查;前端传什么都不决定改谁
        if not cur:
            raise HttpError(404, "你现在没有可用的 key,不用重置 —— 直接申请一把新的就行")
        rid = cur["record_id"]
        if rid_in is not None and rid_in != rid:
            # 前端那一行和服务端认定的 current 不是同一把(页面放久了 / 期间换过)
            raise HttpError(409, "页面上这把 key 和服务器上的不是同一把了,刷新页面再试")
        if not self.rotate_lock(user, rid):
            raise HttpError(409, "你的 key 正在重置中,等几秒刷新页面看看")
        try:
            return self._rotate_locked(user, cur, rid, ip)
        finally:
            self.rotate_unlock(user["union_id"])
            self.drop_rows_cache(user["union_id"])   # 换过表 ⇒ 下一次 /api/keys 必须看到新 key

    def _rotate_locked(self, user, cur, rid, ip):
        c, L = self.cfg, self.litellm
        old_plain = cur["key"]
        try:
            old = L.key_info(sha(old_plain))
        except LiteLLMError as e:
            self.audit_rotate(user, rid, ip, "read", "fail", detail="key_info %s %s" % (e.status, e.msg))
            log("rotate 读旧 key 失败", short(user["union_id"]), e.status)
            raise HttpError(502, "网关暂时读不到你的 key,没有做任何改动,稍后再试")
        if not old:
            self.audit_rotate(user, rid, ip, "read", "fail", detail="not-found-on-gateway")
            log("rotate 旧 key 网关上查不到", short(user["union_id"]), rid)
            raise HttpError(409, "表里这把 key 在网关上查不到(可能已经被换过),没有做任何改动。"
                                 "刷新页面看看;还是这样请提反馈")
        # 🔴 被封的 key 绝不能自助重置 —— 否则就是**自助解封**。
        #   新 key 照抄字段但不抄 blocked(重置的目的就是给一把能用的)⇒ 给被封的人一重置,他就拿到一把能用的。
        #   而 10-29 起的异常账户处置(skill litellm-198-key-usage-audit)是**只在网关上封 key、不动飞书表**:
        #   表里那一行状态照样是「有效」,所以 current_key() 会照常把它挑出来 —— 只看表根本拦不住。
        #   必须以**网关上的 blocked**为准。封号恢复只能管理员做(要用户点头,见那份 skill)。
        if old.get("blocked") is True:
            self.audit_rotate(user, rid, ip, "read", "fail", old_alias=(old.get("key_alias") or ""),
                              detail="old-key-blocked")
            log("rotate 拒绝:旧 key 已被封", short(user["union_id"]), rid)
            raise HttpError(403, "这把 key 已被停用,不能自助重置。有疑问请联系管理员")
        old_alias = (old.get("key_alias") or "").strip()
        if not old_alias or not ALIAS_OK_RE.match(old_alias):
            # 没名字就没法「改名退役」,也没法给新 key 起名 ⇒ 失败关闭,不猜
            self.audit_rotate(user, rid, ip, "read", "fail", old_alias=old_alias, detail="bad-alias")
            log("rotate 旧 key 没有可用 alias", short(user["union_id"]), rid)
            raise HttpError(409, "你这把 key 在网关上没有规范的名字,自助重置处理不了,请提反馈(我们手工换)")
        # 新 alias:同基名 + 新后缀;和旧后缀撞上会 400(alias 全局唯一)⇒ 换一个
        alias = ""
        for _ in range(8):
            cand = new_alias(old_alias, "".join(secrets.choice("0123456789abcdefghijklmnopqrstuvwxyz")
                                                for _ in range(4)))
            if cand != old_alias and ALIAS_OK_RE.match(cand):
                alias = cand
                break
        if not alias:
            raise HttpError(500, "服务出错")
        carried = [k for k in ROTATE_CARRY if not empty_val(old.get(k))]
        if c["ROTATE_DRY_RUN"] != "0":
            self.audit_rotate(user, rid, ip, "dry-run", "dry-run", old_alias, alias)
            log("rotate dry-run", short(user["union_id"]), old_alias, "→", alias)
            return {"ok": True, "dry_run": True, "record_id": rid, "old_alias": old_alias,
                    "new_alias": alias, "carry": carried, "masked": mask_key(old_plain),
                    "message": "演练模式:没有生成新 key,也没有改任何东西。"}
        return self._rotate_apply(user, cur, rid, ip, old, old_alias, alias, carried)

    def _rotate_apply(self, user, cur, rid, ip, old, old_alias, alias, carried):
        c, L = self.cfg, self.litellm
        uid = user["union_id"]
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.now()))

        # ① 生成新 key。失败 ⇒ 什么都没动,旧 key 照常能用
        try:
            res = L.generate(rotate_payload(old, alias, now_iso))
        except LiteLLMError as e:
            self.audit_rotate(user, rid, ip, "generate", "fail", old_alias, alias,
                              "generate %s %s" % (e.status, e.msg))
            log("rotate 生成失败", short(uid), alias, e.status)
            raise HttpError(502, "没有生成出新 key。你现在的 key 还能继续用,什么都没变,稍后再试")
        plain = res["key"]
        self.audit_rotate(user, rid, ip, "generate", "ok", old_alias, alias)

        def rollback(step, msg, detail):
            """新 key 已生成但后面失败:把新的删掉,回到「只有旧 key」的干净状态。"""
            try:
                L.delete_by_alias(alias)
                self.audit_rotate(user, rid, ip, step, "rolled-back", old_alias, alias, detail)
                log("rotate 回滚:已删掉新 key", short(uid), alias, step)
                return HttpError(502, msg + "已经把刚生成的那把撤掉了,你现在的 key 还能继续用,稍后再试")
            except LiteLLMError as e2:
                # 删不掉 ⇒ 两把都活着。必须说清楚,不能假装成功
                self.audit_rotate(user, rid, ip, step, "orphan", old_alias, alias,
                                  "%s;delete %s %s" % (detail, e2.status, e2.msg))
                log("🔴 rotate 回滚失败,新 key 成了孤儿", short(uid), alias, e2.status)
                return HttpError(502, msg + "而且刚生成的那把也没撤掉。你现在的 key 还能继续用,"
                                            "但请立刻提一条反馈(把这句话复制过去),我们来清理")

        # ② 回读确认:建出来了 ≠ 预算/权限/归属都跟过来了
        try:
            back = L.key_info(sha(plain))
        except LiteLLMError as e:
            raise rollback("verify", "新 key 生成后读不回来。", "readback %s %s" % (e.status, e.msg))
        if not back:
            raise rollback("verify", "新 key 生成后在网关上查不到。", "readback-empty")
        bad = carry_mismatch(old, back)
        if bad:
            raise rollback("verify", "新 key 的配置和旧的不一致(" + "、".join(bad) + ")。", "mismatch:" + ",".join(bad))

        # ③ 真发一发。只有这一步能证明「这把 key 真能用」,/key/info 读得回来不算
        if c["ROTATE_PROBE"] != "0":
            model = pick_probe_model(back.get("models"))
            if not model:
                self.audit_rotate(user, rid, ip, "probe", "skipped", old_alias, alias, "no-concrete-model")
                log("rotate 探针跳过(没有能直接打的模型名)", short(uid), alias)
            else:
                try:
                    ok, why = L.probe(plain, model, c["ROTATE_PROBE_MAX_TOKENS"])
                except LiteLLMError as e:
                    ok, why = False, "%s %s" % (e.status, e.msg)
                if not ok:
                    raise rollback("probe", "新 key 生成出来了但试调不通(" + why[:80] + ")。", "probe:" + why)
                self.audit_rotate(user, rid, ip, "probe", "ok", old_alias, alias, why)

        # ④ 写表。这一步是用户拿到新 key 的唯一途径 ⇒ 失败必须回滚,不能留两把活的
        fields = self.rotate_fields(cur, plain, alias)
        try:
            self.client.update_record(c["KEY_BASE_TOKEN"], c["KEY_TABLE_ID"], rid, fields)
        except FeishuError as e:
            raise rollback("bitable", "新 key 生成了但没写进表里(飞书 code=%s)。" % e.code,
                           "update_record %s" % e.code)
        self.audit_rotate(user, rid, ip, "bitable", "ok", old_alias, alias)

        # ⑤ 最后才封旧的。到这儿用户已经有能用的新 key 了 ⇒ 这步失败**不算整体失败**,
        #    只是留了一把该封没封的旧 key(可以事后补);反过来先封再写表就会把人锁在外面。
        #    🔴 /key/update 三个坑:不捎带 metadata(整体替换会擦掉 lark_open_id 这些)、
        #    不传 budget_duration(会重算 budget_reset_at,窗口无限后推)、回读只认 /key/info。
        warning = ""
        try:
            # 用 sha256 定位,不用 old["token"]:`/key/info` 把 token 列置空了(实测),信它会发出 key=None
            L.update({"key": sha(cur["key"]),
                      "key_alias": retired_alias(old_alias, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.now()))),
                      "blocked": True})
            self.audit_rotate(user, rid, ip, "block-old", "ok", old_alias, alias)
            log("rotate 完成", short(uid), old_alias, "→", alias)
        except LiteLLMError as e:
            warning = "旧 key 没能立刻停用(我们会处理)。在那之前旧 key 可能还能用,别再把它给别人。"
            self.audit_rotate(user, rid, ip, "block-old", "fail", old_alias, alias,
                              "update %s %s" % (e.status, e.msg))
            log("🔴 rotate 旧 key 封禁失败(新 key 已生效)", short(uid), old_alias, e.status)
        self.audit_rotate(user, rid, ip, "done", "ok", old_alias, alias, "warn" if warning else "")
        out = {"ok": True, "dry_run": False, "record_id": rid, "key": plain, "masked": mask_key(plain),
               "old_alias": old_alias, "new_alias": alias, "carry": carried, "old_blocked": not warning}
        if warning:
            out["warning"] = warning
        return out

    def rotate_fields(self, cur, plain, alias):
        """写回表的字段。只动这几列,别人手改过的其它列不碰(飞书 PUT 是逐字段覆盖)。

        - `API Key`:新明文。这是所有下游(安装器 / redeem / 体检页)唯一读的那一列。
        - `状态`:写表里**已有的选项**「有效（已重新生成）」(前缀仍是「有效」⇒ 还是 current、还能装)。
          故意**不先写「重新生成中」**:那会让 front_status 变成「生成中」⇒ 页面把安装命令收起来,
          万一后面失败就得再写一次改回去 —— 多一次写就多一种把行留在错状态上的可能。
        - `key_alias`:写**基名**(表里历来就是基名,poller 和换发脚本都这样写),不带后缀。
        - **不动 `申请时间`**:这一行本来就是 current(最新的有效 Cursor 行),换 key 不改变这一点;
          而客户批量 key 的聚类规则(drop_batches)读的就是这一列,改它可能把一行本人 key 聚进批量里藏掉。
        - `点我体检`:超链接列,不会自己跟着 API Key 走(公式列才会)。不更新就指向已经封掉的旧 key,
          用户点体检会看到「这把 key 不可用」,以为新 key 坏了。
        """
        c = self.cfg
        f = {c["KEY_FIELD"]: plain, c["KEY_STATUS_FIELD"]: ROTATE_STATUS, "key_alias": alias_base(alias)}
        if cur.get("probe_link"):
            f[KEY_PROBE_FIELD] = {"link": KEYPROBE_PAGE + "#k=" + plain, "text": KEYPROBE_TEXT}
        # 这两列真表里基本是空的;只有当它存的就是这把旧 key 时才跟着换,否则不碰
        for col in ("Cursor Key", "Claude Code Key"):
            if cur.get("extra_keys", {}).get(col) == cur["key"]:
                f[col] = plain
        assert set(f) <= ROTATE_FIELD_ALLOW, set(f) - ROTATE_FIELD_ALLOW
        return f

    # 一次性安装码
    def issue_code(self, user, body):
        tool = body.get("tool")
        # all = 一个码装三个(安装器按 ALL_TOOLS 顺序依次装);仍然只发同一把 key
        if tool not in self.issuable:
            raise HttpError(400, "tool 只能是:" + "、".join(self.issuable))
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

    def redeem_blocked(self, ip):
        """P1-2:闸只看**失败**次数。
        - 这个 IP 一分钟内失败满 REDEEM_PER_IP_PER_MIN 次 ⇒ 429(真码也不判,不给撞库对/错信号)。
        - 全站失败数过了 REDEEM_GLOBAL_PER_MIN(有人在多 IP 撞库)⇒ 只收紧**失败过的** IP(1 次就停),
          一次都没失败过的 IP 照常换 ⇒ 打满全局闸再也锁不住正常用户。"""
        if not self.rl_redeem_ip.allowed(ip):
            return True
        if not self.rl_redeem_all.allowed("*") and self.rl_redeem_ip.count(ip) > 0:
            return True
        return False

    def redeem_fail(self, ip):
        self.rl_redeem_ip.record(ip)
        self.rl_redeem_all.record("*")

    def audit_redeem(self, ip, ua, result, union_id=None, tool=None, rec=None, code_hash=""):
        """P1-3:安装码换出明文的审计。🔴 不记明文:只记 key 的 sha256(= LiteLLM 库里的 token 列 /
        SpendLogs.api_key,能直接 join 网关用量)和打码尾巴;安装码只记它 HMAC 的前 16 位(可 join install_codes)。"""
        key = (rec or {}).get("key") or ""
        self.q("insert into redeem_audit(at, union_id, tool, record_id, key_sha, key_masked, code_id, ip, ua, result)"
               " values(?,?,?,?,?,?,?,?,?,?)",
               (self.now(), union_id, tool, (rec or {}).get("record_id"), sha(key) if key.startswith("sk-") else None,
                mask_key(key) if key else None, code_hash[:16] or None, ip, mask_deep((ua or "")[:200]), result))

    def redeem(self, body, ip, ua=""):
        if self.redeem_blocked(ip):
            log("redeem 限流", ip)
            raise HttpError(429, "尝试太频繁,稍后再试")      # 不写审计:撞库时这条最多,只进日志
        code = body.get("code")
        raw = code.replace("-", "").strip().upper() if isinstance(code, str) else ""
        if len(raw) != CODE_LEN or any(ch not in CODE_ALPHABET for ch in raw):
            self.redeem_fail(ip)
            self.audit_redeem(ip, ua, "denied:bad-format")
            raise HttpError(400, "安装码无效或已过期")
        ch = self._h(self._k_code, raw)
        row = self.q("select union_id, account_type, tool from install_codes where code_hash=? and used is null and expires>?",
                     (ch, self.now()), one=True)
        if not row:
            self.redeem_fail(ip)
            self.audit_redeem(ip, ua, "denied:invalid", code_hash=ch)
            log("redeem 拒绝", ip)
            raise HttpError(400, "安装码无效或已过期")   # 错码/过期/用过 同一个回答
        try:
            # 交明文的这一步不吃缓存:管理员刚在表里停用的 key 不能再装出去
            r = self.current_key({"union_id": row[0]}, fresh=True)
        except FeishuError as e:
            log("redeem 查表失败", e.code)            # 不作废码:飞书抖一下让他重试
            self.audit_redeem(ip, ua, "error:feishu-%s" % e.code, row[0], row[2], code_hash=ch)
            raise HttpError(502, "查询失败,稍后再试")
        if not r:
            self.audit_redeem(ip, ua, "denied:nokey", row[0], row[2], code_hash=ch)
            raise HttpError(404, "这个账号下没有可用的 key")
        _, n = self.q("update install_codes set used=? where code_hash=? and used is null and expires>?",
                      (self.now(), ch, self.now()))
        if n != 1:                                      # 并发换取:只有一个赢
            self.redeem_fail(ip)
            self.audit_redeem(ip, ua, "denied:race", row[0], row[2], code_hash=ch)
            raise HttpError(400, "安装码无效或已过期")
        self.audit_redeem(ip, ua, "ok", row[0], row[2], r, ch)
        log("redeem", short(row[0]), row[2], ip)
        # 🔴 11-04 联调时发现:签发时存了 tool,换取时却没回 ⇒ 安装器
        #    (installer/install.sh:113)拿不到 tool 就 die「安装服务没说要装哪个工具」⇒ **每一次安装都在第一步失败**。
        #    两边各自的测试都是绿的(各自对着自己的假对端),只有接起来才看得见。
        # 不回网关地址:两个安装器都不读它(grep 零命中),网关地址写在子安装器自己的 BASE_URL 里
        # (codex-oneclick/src/install-mac.sh:17、claude-oneclick/src/install-mac.sh:20,还带旧地址兜底逻辑)。
        # 回一个没人读的字段 = 让下一个人以为"改服务端就能换网关",实际改了不生效。
        # tool 原样回签发时存的那个值,可能是 "all"(安装器看到 all 就依次装三个,仍只用这一把 key)。
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
        # 外部人:机器人签链接时不传名字(name 恒空)、人员格也可能写不进 ⇒ 只剩这行能认人。
        # 带上 union_id + tenant_key,管理员才知道是谁、去哪个租户找(都不是凭据)。
        who = user["name"] or "(没有名字)"
        if user["external"]:
            who += " union_id=%s tenant_key=%s" % (user["union_id"], user.get("tenant_key") or "-")
        tag = "\n\n—— 提交人:%s(%s)" % (who, "外部" if user["external"] else "内部")
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
# 11-04 红队 P2-2:安全头。
# - 非页面响应(JSON / 302 / 错误页)一律最严的 CSP:什么都不许加载、不许被嵌。
# - 页面(index.html)的内联 <script>/<style> 按**内容的 sha256** 放行(serve 时现算,文件改了自动跟上),
#   不用 'unsafe-inline'。🔴 页面里**不能有**内联 style="" / onclick="" / <script src>:
#   hash 管不到属性,会被 CSP 直接拦掉(test_index_has_no_inline_attrs 钉着)。
# - img-src data::二维码库先用 data: GIF 探测、再把 canvas 转成 data: PNG 显示。
# - 没有 font-src / 外链:展示明文 key 的页面不加载任何第三方资源(Google Fonts 已删)。
# ⚠️ CSP 只管「这个页面里跑什么」。同域别的页面(OWUI / myai / feedback)一旦有 XSS,
#   仍然可以直接 fetch('/ai/api/keys/reveal')(同源请求会带 Path=/ai/ 的 cookie)——
#   这条只有把门户挪到**独立子域**才能根治;frame-ancestors + COOP 只堵住了「嵌/开一个 /ai/ 窗口去读 DOM」。
CSP_STRICT = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
PERMISSIONS_POLICY = ("camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                      "clipboard-read=(), clipboard-write=(self)")
# <script>/<style> 是 raw text 元素:浏览器在**第一个**结束标签处收尾 ⇒ 非贪婪匹配和浏览器切法一致。
# 🔴 别在 CSS/HTML 注释里写字面的开始标签,会被当成一块(test_index_has_no_external_resources_or_inline_attrs 钉着)
INLINE_BLOCK_RE = re.compile(rb"<(script|style)>(.*?)</\1>", re.S)
_csp_cache = {}


def csp_hashes(b):
    """页面字节 → (script 的 hash 列表, style 的 hash 列表)。按浏览器的算法:对标签之间的原始字节做 sha256。"""
    out = {b"script": [], b"style": []}
    for m in INLINE_BLOCK_RE.finditer(b):
        out[m.group(1)].append("'sha256-%s'" % base64.b64encode(hashlib.sha256(m.group(2)).digest()).decode())
    return out[b"script"], out[b"style"]


def page_csp(b):
    key = hashlib.sha256(b).digest()
    v = _csp_cache.get(key)
    if v is None:
        sc, st = csp_hashes(b)
        v = ("default-src 'none'; script-src %s; style-src %s; img-src 'self' data:; connect-src 'self'; "
             "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
             % (" ".join(sc) or "'none'", " ".join(st) or "'none'"))
        if len(_csp_cache) > 16:
            _csp_cache.clear()
        _csp_cache[key] = v
    return v


ALLOW = "GET, HEAD, POST, OPTIONS"
HEAD_OK = ("", "/healthz", "/auth/magic")   # HEAD 只给没有副作用的路径 + 静态文件(login 会建 state,logout 会删会话)


class H(http.server.BaseHTTPRequestHandler):
    server_version = "ai-portal"
    sys_version = ""
    app = None          # make_server 里绑定
    _head = False

    def log_message(self, fmt, *a):
        pass            # 默认访问日志会带 query(code/state),整个关掉

    def version_string(self):
        return "ai-portal"      # 不报 BaseHTTP/x.y Python/x.y(P2-5)

    # 输出
    def _common_headers(self, referrer="same-origin", csp=CSP_STRICT):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", referrer)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Permissions-Policy", PERMISSIONS_POLICY)
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def _write(self, b):
        if not self._head:
            self.wfile.write(b)

    def send_error(self, code, message=None, explain=None):
        """P2-5:http.server 自己发的错误(请求行坏了 / 不支持的方法 / 头太长)也走统一的 JSON 信封,
        不出它默认那张会回显请求内容、带 Python 版本的 HTML 页。"""
        if code == 501:         # 不认识的方法 ⇒ 405 + Allow,而不是「服务器没实现」
            code = 405
        msg = {400: "请求格式不对", 405: "不支持这个方法", 408: "请求超时", 414: "地址太长",
               431: "请求头太大"}.get(code, "请求没成功")
        self.close_connection = True
        try:
            self.send_json(code, {"error": msg, "message": msg},
                           extra=(("Allow", ALLOW),) if code == 405 else ())
        except Exception:       # noqa: BLE001 —— 连接已经坏了就算了
            pass

    def send_json(self, code, obj, cookies=(), extra=()):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        for c in cookies:
            self.send_header("Set-Cookie", c)
        for k, v in extra:
            self.send_header(k, v)
        self._common_headers()
        self.end_headers()
        self._write(b)

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
        self._write(b)

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

    def do_HEAD(self):
        """P2-5:HEAD 只对没副作用的路径(静态文件 / healthz / magic 落地页)= GET 去掉正文;其余 405。"""
        self._head = True
        self._run(self._head_route)

    def _head_route(self):
        p = self.sub()
        if p is None or p in HEAD_OK or not (p.startswith("/api/") or p.startswith("/auth/")):
            return self._get()
        raise HttpError(405, "不支持这个方法")

    def do_OPTIONS(self):
        """不做 CORS:只回 Allow,**不给任何 Access-Control-Allow-***(跨站预检因此必失败)。"""
        self.send_response(204)
        self.send_header("Allow", ALLOW)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()

    def _method_not_allowed(self):
        self.send_error(405)

    do_PUT = do_DELETE = do_PATCH = do_TRACE = do_CONNECT = _method_not_allowed

    def _run(self, fn):
        try:
            fn()
        except MagicError as e:
            self.send_json(e.status, {"error": e.code, "message": e.msg})
        except HttpError as e:
            if e.status == 401:
                return self.send_json(401, {"login": False, "error": e.msg, "message": e.msg})
            self.send_json(e.status, {"error": e.msg, "message": e.msg},   # 前端读 message
                           extra=(("Allow", ALLOW),) if e.status == 405 else ())
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
                if e.msg == OAUTH_EXTERNAL_MSG:
                    # 外部人点错按钮 ⇒ 送回首页,页面按 login_error=external 告诉他去找机器人(白名单查表,不带原文)
                    return self.redirect(a.base + "/?login_error=external", [clear])
                self.send_response(e.status)        # 页面形式回给浏览器,顺手清掉 state cookie
                b = ("<!doctype html><meta charset=utf-8><p>%s</p><p><a href=\"%s/auth/login\">重新登录</a></p>"
                     % (html.escape(e.msg), a.base)).encode()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Set-Cookie", clear)
                self._common_headers()
                self.end_headers()
                return self._write(b)
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
                "issuable_tools": list(a.issuable),   # 含 "all";前端不读它(卡片写死),给排障看
                # 前端据此决定「重置我的 key」出真按钮还是退回「提反馈」。没配网关 key ⇒ false
                "rotate_enabled": a.litellm.enabled, "rotate_dry_run": a.cfg["ROTATE_DRY_RUN"] != "0",
                "rotate_cooldown": int(a.cfg["ROTATE_COOLDOWN"]),     # 二次确认里写「N 分钟内不能再重置」
                "feedback_choices": fb.CHOICES,
                # 外部人待审核申请的登记时间(秒,0 = 没有);前端刷新后据此显示「已登记、等审核」
                "apply_review_at": a.apply_review_at(u),
                "apply_external_review": a.cfg["APPLY_ALLOW_EXTERNAL"] != "1"})
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
            return self.send_json(200, a.redeem(self.body_json(), self.client_ip(), self.headers.get("User-Agent") or ""))
        routes = {"/api/apply": a.apply, "/api/install-code": a.issue_code, "/api/feedback": a.feedback}
        withip = {"/api/keys/reveal": a.reveal, "/api/keys/rotate": a.rotate}   # 这两个要记审计 ⇒ 带 IP
        if p not in routes and p not in withip:
            raise HttpError(404, "not found")
        u = self.user()
        if p in withip:
            return self.send_json(200, withip[p](u, self.body_json(), self.client_ip()))
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
        self._common_headers(referrer="no-referrer" if referrer_none else "same-origin",
                             csp=page_csp(b) if ctype.startswith("text/html") else CSP_STRICT)
        self.end_headers()
        self._write(b)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def make_server(app, host="127.0.0.1", port=0):
    handler = type("BoundH", (H,), {"app": app})
    return Server((host, port), handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI 自助门户后端")
    ap.add_argument("--env", default=os.environ.get("AI_PORTAL_ENV"), help="env 文件路径")
    ap.add_argument("--kill-sessions", metavar="UNION_ID", action="append",
                    help="运维:踢掉这个人的全部会话 + 作废他没用的安装码/登录链接,然后退出(不起服务)。可重复")
    ap.add_argument("--list-apply-requests", action="store_true",
                    help="运维:列出外部人待审核的 key 申请(本地库,最近 30 天),然后退出")
    args = ap.parse_args(argv)
    cfg = load_config(env_file=args.env)
    errs = check_config(cfg)
    if errs:
        print("拒绝启动:" + ";".join(errs), file=sys.stderr)
        return 2
    app = App(cfg)
    if args.list_apply_requests:
        # 反馈表写不进时(11-04 实测 carher-236 对反馈表 91403)待审核申请只在本地库里 ⇒ 运维从这里看
        for uid, name, tk, at in app.list_apply_requests():
            print("%s\t%s\t%s\t%s" % (time.strftime("%F %T", time.localtime(at)), uid, tk or "-", name or "-"))
        return 0
    if args.kill_sessions:
        rc = 0
        for uid in args.kill_sessions:
            try:
                r = app.kill_user(uid.strip())
            except ValueError as e:
                print("跳过 %s:%s" % (uid, e), file=sys.stderr)
                rc = 1
                continue
            print("已踢 %s:会话 %d 条,未用安装码 %d 个,未用登录链接 %d 个" % (
                short(uid), r["sessions"], r["install_codes"], r["magic_tokens"]))
        return rc
    if cfg["APPLY_DRY_RUN"] != "0":
        log("APPLY_DRY_RUN 开着:申请只返回将要写的字段,不写表")
    host, port = cfg["LISTEN"].rsplit(":", 1)
    srv = make_server(app, host, int(port))
    log("listening", cfg["LISTEN"], "base", cfg["BASE_PATH"])
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
