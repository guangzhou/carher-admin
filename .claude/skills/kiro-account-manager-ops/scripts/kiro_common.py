#!/usr/bin/env python3
"""Kiro Account Manager 批量登录 —— 共享库。

PKCE / AWS SSO OIDC / getUsageLimits / accounts.json 幂等写入。

规格来源 = kiro-account-manager Rust 源码（唯一权威，勿凭记忆改）：
  src-tauri/src/clients/aws_sso_client.rs   register_client / create_token
  src-tauri/src/auth/providers/idc.rs       authorize URL、本地 callback、两种 redirect_uri
  src-tauri/src/clients/kiro_client.rs      getUsageLimits URL + headers
  src-tauri/src/utils/client_id_hash.rs     clientIdHash = sha1(紧凑 JSON)

⚠️ 禁回显密码/token 值，禁 git add 任何 creds 产物。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import httpx

# ---------------------------------------------------------------- 常量

REGION = "us-east-1"
OIDC_BASE = f"https://oidc.{REGION}.amazonaws.com"
KIRO_AUTH_API = "https://prod.us-east-1.auth.desktop.kiro.dev"  # GitHub/social 流程用，当前未实现
BUILDER_ID_START_URL = "https://view.awsapps.com/start"
KIRO_APP_VERSION = "0.6.18"

# idc.rs: GRANT_SCOPES.join(",")
GRANT_SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]

# idc.rs:296 —— register 用「不带端口」，authorize/createToken 用「带端口」。
# 这两个不是一个值，混用会让 createToken 400。
REGISTER_REDIRECT_URI = "http://127.0.0.1/oauth/callback"

ACCOUNTS_PATH = (
    Path.home()
    / "Library/Application Support/.kiro-account-manager/accounts.json"
)


# ---------------------------------------------------------------- 工具

def normalize_pw(pw: str) -> str:
    """全角 → 半角（U+FF01~U+FF5E）。Binus 表格混了全角标点。"""
    out = []
    for ch in pw:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def pw_variants(pw: str) -> list[str]:
    """原始值优先，归一化值兜底（去重）。"""
    norm = normalize_pw(pw)
    return [pw] if norm == pw else [pw, norm]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def gen_pkce() -> tuple[str, str]:
    """(verifier, challenge) —— auth_social.rs: 32 随机字节 + SHA256，均 base64url 无 padding。"""
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def client_id_hash(start_url: str = BUILDER_ID_START_URL) -> str:
    """client_id_hash.rs —— sha1 of 紧凑 JSON，separators 必须无空格。"""
    payload = json.dumps({"startUrl": start_url}, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()


def now_str() -> str:
    return time.strftime("%Y/%m/%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- 本地 callback

class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # 🔴 只认 /oauth/callback 且带 code/error 的请求，且**不覆盖**已记录的结果。
        # 实测 2026-10-08：浏览器渲染回调页后会再请求 /favicon.ico，
        # 无条件记录会把真结果冲成 code=None/state=None，
        # 表现为「state 不匹配，可能是串台的回调」—— 看着像串台，其实是自己打自己。
        is_callback = parsed.path.rstrip("/").endswith("/oauth/callback")
        has_payload = bool(qs.get("code") or qs.get("error"))
        if is_callback and has_payload and self.server.oauth_result is None:  # type: ignore[attr-defined]
            self.server.oauth_result = {  # type: ignore[attr-defined]
                "code": (qs.get("code") or [None])[0],
                "state": (qs.get("state") or [None])[0],
                "error": (qs.get("error") or [None])[0],
            }

        body = b"<html><body><h2>Kiro login OK, you can close this tab.</h2></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # 静音
        return


class CallbackServer:
    """idc.rs:89 —— 绑 127.0.0.1:0 让内核选端口。"""

    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        self.httpd.oauth_result = None  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.redirect_uri = f"http://127.0.0.1:{self.port}/oauth/callback"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 300.0, expect_state: str | None = None) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.httpd.oauth_result  # type: ignore[attr-defined]
            if result:
                if expect_state and result.get("state") != expect_state:
                    raise RuntimeError("state 不匹配，可能是串台的回调")
                if result.get("error"):
                    raise RuntimeError(f"授权被拒: {result['error']}")
                if not result.get("code"):
                    raise RuntimeError("回调里没有 code")
                return result
            time.sleep(0.4)
        # 别只写「MFA 码错」：2026-10-08 rhania 实测真因是点击式 WAF 挑战没点，
        # 一直停在密码页。超时只说明「没走到 consent」，不指向具体哪一步。
        raise TimeoutError(
            f"等 callback 超时 {timeout}s —— 没走到 consent。"
            "常见真因：WAF 点击挑战未处理 / 验证码连错 / 密码错 / MFA 码过期。"
            "查最后几条 `page ->` 停在哪一步"
        )

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


# ---------------------------------------------------------------- AWS SSO OIDC

def register_client() -> dict:
    """aws_sso_client.rs:register_client —— 注意 redirect_uri 用不带端口那个。"""
    body = {
        "clientName": "Kiro IDE",
        "clientType": "public",
        "scopes": GRANT_SCOPES,
        "grantTypes": ["authorization_code", "refresh_token"],
        "redirectUris": [REGISTER_REDIRECT_URI],
        "issuerUrl": BUILDER_ID_START_URL,
    }
    resp = httpx.post(
        f"{OIDC_BASE}/client/register",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"client/register {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def build_authorize_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    """idc.rs:build_authorize_url —— 参数名是 `scopes`（复数，逗号分隔），不是 OAuth 标准的 `scope`。"""
    return (
        f"{OIDC_BASE}/authorize"
        f"?response_type=code&client_id={quote(client_id, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scopes={quote(','.join(GRANT_SCOPES), safe='')}"
        f"&state={quote(state, safe='')}"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )


def create_token(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str) -> dict:
    """aws_sso_client.rs:create_token —— camelCase body，redirect_uri 必须是带端口那个。"""
    body = {
        "clientId": client_id,
        "clientSecret": client_secret,
        "grantType": "authorization_code",
        "code": code,
        "codeVerifier": verifier,
        "redirectUri": redirect_uri,
    }
    resp = httpx.post(
        f"{OIDC_BASE}/token",
        json=body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"createToken {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ---------------------------------------------------------------- getUsageLimits

def list_available_profiles(access_token: str, region: str = REGION) -> str | None:
    """kiro_client.rs:list_available_profiles —— 返回第一个非空 profile ARN。

    实测 2026-10-08：不带 profileArn 调 getUsageLimits 会 400 `Invalid profileArn.`
    （错误文案很误导——不是「带错了」，是「该带没带」）。先用这个解析真 ARN。

    注意这是 control-plane 调用：POST 到 host 根路径 + x-amz-target，
    headers 跟 management 那套不同（无 machineId UA）。
    """
    resp = httpx.post(
        f"https://management.{region}.kiro.dev/",
        json={},
        headers={
            "Authorization": f"Bearer {access_token}",
            "content-type": "application/x-amz-json-1.0",
            "x-amz-target": "KiroControlPlaneBearerService.ListAvailableProfiles",
            "user-agent": (
                "aws-sdk-js/1.0.0 ua/2.1 os/darwin#24.0.0 lang/js md/nodejs#20.18.1 "
                "api/kirocontrolplanebearer#1.0.0 m/N,E"
            ),
            "x-amz-user-agent": "aws-sdk-js/1.0.0",
            "amz-sdk-invocation-id": str(uuid.uuid4()),
            "amz-sdk-request": "attempt=1; max=3",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"ListAvailableProfiles {resp.status_code}: {resp.text[:200]}")
    for profile in (resp.json().get("profiles") or []):
        arn = (profile.get("arn") or "").strip()
        if arn:
            return arn
    return None


def get_usage_limits(
    access_token: str, machine_id: str, region: str = REGION, profile_arn: str | None = None
) -> dict:
    """kiro_client.rs:get_usage_limits —— 必须带 Kiro IDE 的 aws-sdk-js UA，否则会被判非法客户端。"""
    url = (
        f"https://management.{region}.kiro.dev/getUsageLimits"
        "?isEmailRequired=true&origin=AI_EDITOR&resourceType=AGENTIC_REQUEST"
    )
    if profile_arn:
        url += f"&profileArn={quote(profile_arn, safe='')}"
    suffix = f"KiroIDE-{KIRO_APP_VERSION}-{machine_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "user-agent": (
            f"aws-sdk-js/1.0.0 ua/2.1 os/darwin#24.0.0 lang/js md/nodejs#20.18.1 "
            f"api/codewhispererruntime#1.0.0 m/N,E {suffix}"
        ),
        "x-amz-user-agent": f"aws-sdk-js/1.0.0 {suffix}",
        "amz-sdk-invocation-id": str(uuid.uuid4()),
        "amz-sdk-request": "attempt=1; max=1",
        "accept": "application/json",
    }
    resp = httpx.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"getUsageLimits {resp.status_code}: {resp.text[:300]}")
    return resp.json()


# ---------------------------------------------------------------- accounts.json

def load_accounts() -> list[dict]:
    if not ACCOUNTS_PATH.exists():
        return []
    return json.loads(ACCOUNTS_PATH.read_text())


def account_exists(email: str) -> bool:
    return any((a.get("email") or "").lower() == email.lower() for a in load_accounts())


def append_account(entry: dict) -> str:
    """原子写入 + 时间戳备份。按 email 幂等：已存在则就地替换而非追加重复行。"""
    accounts = load_accounts()
    if accounts:
        backup = ACCOUNTS_PATH.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(json.dumps(accounts, ensure_ascii=False, indent=2))

    email = (entry.get("email") or "").lower()
    for idx, acc in enumerate(accounts):
        if (acc.get("email") or "").lower() == email:
            accounts[idx] = entry
            verb = "replaced"
            break
    else:
        accounts.append(entry)
        verb = "appended"

    tmp = ACCOUNTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2))
    os.replace(tmp, ACCOUNTS_PATH)
    return f"{verb}, total={len(accounts)}"


def build_idc_entry(email: str, token: dict, reg: dict, usage: dict | None, machine_id: str) -> dict:
    """account.rs + idc.rs —— BuilderId 条目字段。start_url/profileArn 对 BuilderId 恒为 null。"""
    expires_at = time.strftime(
        "%Y/%m/%d %H:%M:%S",
        time.localtime(time.time() + int(token.get("expiresIn") or 0)),
    )
    user_id = None
    if usage:
        user_id = (usage.get("userInfo") or {}).get("userId")
    return {
        "id": str(uuid.uuid4()),
        "email": email,
        "password": None,
        "label": "Kiro BuilderId",
        "status": "active",
        "addedAt": now_str(),
        "accessToken": token["accessToken"],
        "refreshToken": token["refreshToken"],
        "expiresAt": expires_at,
        "provider": "BuilderId",
        "userId": user_id,
        "authMethod": "IdC",
        "clientId": reg["clientId"],
        "clientSecret": reg["clientSecret"],
        "region": REGION,
        "clientIdHash": client_id_hash(),
        "ssoSessionId": token.get("aws_sso_app_session_id"),
        "idToken": token.get("idToken"),
        "startUrl": None,
        "profileArn": None,
        "usageData": usage,
        "groupId": None,
        "tagLinks": [],
        "machineId": machine_id,
        "availableModelsCache": None,
        "failureCount": 0,
        "lastFailureAt": None,
        "disabledReason": None,
        "successCount": 0,
        "enabled": True,
    }


# ---------------------------------------------------------------- Graph 邮箱取 AWS 验证码

MS_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"


def graph_get_access_token(refresh_token: str, client_id: str) -> tuple[str, str]:
    """用 Microsoft consumer refresh-token 换 Graph access-token。
    返回 (access_token, new_refresh_token)。"""
    resp = httpx.post(
        MS_TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": "https://graph.microsoft.com/.default offline_access",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Graph token exchange {resp.status_code}: {resp.text[:200]}")
    tok = resp.json()
    return tok["access_token"], tok.get("refresh_token", refresh_token)


def graph_fetch_aws_otp(
    refresh_token: str, client_id: str, timeout: float = 90,
    after_ts: str | None = None, used_codes: set | None = None,
) -> str | None:
    """从 Outlook 收件箱取最新的 AWS 验证码（6 位数字）。

    after_ts：只取这个 ISO 时间戳之后的邮件（排除旧码）。
    used_codes：已经提交过的码集合（同一个码只返回一次）。
    """
    import re as _re

    access_token, _ = graph_get_access_token(refresh_token, client_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    deadline = time.time() + timeout
    if used_codes is None:
        used_codes = set()

    while time.time() < deadline:
        # 拉最新邮件（不用 $filter——consumer 账号不支持 from 过滤，返回 400 InefficientFilter）
        resp = httpx.get(
            "https://graph.microsoft.com/v1.0/me/messages"
            "?$top=5&$orderby=receivedDateTime desc"
            "&$select=subject,body,from,receivedDateTime",
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 200:
            for msg in resp.json().get("value", []):
                # 只看 AWS MFA 邮件（发件人 no-reply@login.awsapps.com）
                sender = (msg.get("from", {}).get("emailAddress", {}).get("address", "")).lower()
                if "awsapps.com" not in sender and "signin.aws" not in sender:
                    continue
                # 只取 after_ts 之后的邮件
                received = msg.get("receivedDateTime", "")
                if after_ts and received < after_ts:
                    continue
                # 从 body 提取验证码。
                # 🔴 HTML 邮件里 color:#555555 等 CSS 颜色值也是 6 位数字，
                # 会被 \d{6} 误匹配。真码在 class="code" 的 div 里。
                # 策略：先找 class="code" 标签的内容，找不到再 fallback 到全文 \d{6}。
                body_html = msg.get("body", {}).get("content", "")
                subject = msg.get("subject", "")
                # 优先：<div class="code"...>520151</div> 或类似
                code_tag = _re.findall(r'class="code[^"]*"[^>]*>(\d{6})', body_html)
                if code_tag:
                    code = code_tag[0]
                else:
                    # fallback：找不在 CSS 属性里的 6 位数（排除 #NNNNNN 和 :NNNNNN 模式）
                    # 用 body 纯文本（去 HTML 标签）
                    text = _re.sub(r'<[^>]+>', ' ', body_html) + " " + subject
                    all_codes = _re.findall(r'(?<![#:;])\b(\d{6})\b', text)
                    code = all_codes[0] if all_codes else None
                if code:
                    if code in used_codes:
                        continue
                    used_codes.add(code)
                    log(f"  Graph 取到验证码: {code} (来自 {received[:16]})")
                    return code
                if codes:
                    log(f"  Graph 取到验证码: {codes[0]} (来自 {msg.get('receivedDateTime', '?')[:16]})")
                    return codes[0]

        time.sleep(5)

    log("  !! Graph 轮询超时，没取到验证码")
    return None
