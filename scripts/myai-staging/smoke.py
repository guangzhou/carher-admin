#!/usr/bin/env python3
"""myai 预发冒烟：把「收款码 + 人工核账」两条链路跑完，并打印每步的请求/响应要点。

在预发 dashboard pod 里跑（由 scripts/myai-staging.sh smoke 拷进去执行），所以打的是
127.0.0.1:8000 —— dev-login 只认回环对端，这是唯一能拿到平台管理员身份的路径
（密码账号在 `require_user` 里被硬编码为非管理员：authType == "password" ⇒ isAdmin False）。

个人链路：注册（验证码从 pod 日志取不到，这里直接读 auth.sqlite3 的 code_hash 做不了反查
⇒ 改为用 AuthStore 自己插一条已知验证码）→ 登录 → 下 manual_qr 单 → 提交付款说明
→ 管理员 dev-login → /api/admin/billing/orders/{no}/complete → 看桩上建了什么。

企业链路：平台管理员建测试企业（第一位管理员必须先注册）→ 等 outbox 把企业开到桩上
→ 企业管理员接受邀请 → 下企业单 → 提交 → 平台确认 → 查 ledger。

退出码：0 全绿；1 有步骤失败（输出里每步都有 OK/FAIL 和原因）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

BASE = os.getenv("SMOKE_BASE", "http://127.0.0.1:8000")
DOMAIN = os.getenv("SMOKE_DOMAIN", "staging.test")
ADMIN_EMAIL = os.getenv("SMOKE_ADMIN", "staging-admin@staging.test")
AUTH_DB = Path(os.getenv("AUTH_DATABASE_PATH", ".data/auth.sqlite3"))
RUN = uuid.uuid4().hex[:6]
# 测试账号口令不写死：每轮随机生成（冒烟不需要事后再登录）。要复用某个口令就传 SMOKE_PASSWORD。
PASSWORD = os.getenv("SMOKE_PASSWORD") or ("Stg-" + uuid.uuid4().hex[:12] + "-Aa1!")
FAILURES: list[str] = []


def log(step: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {step}" + (f"  {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(f"{step}: {detail}")


class Client:
    """带 cookie 与 CSRF 的最小客户端；Origin 必须等于 APP_BASE_URL，否则 enforce_csrf 403。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.origin = os.getenv("APP_BASE_URL", BASE).rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None, *, expect: int = 200) -> tuple[int, object]:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.origin)
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=60) as response:
                payload = response.read().decode("utf-8", "ignore")
                status = response.status
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "ignore")
            status = exc.code
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload[:400]
        if isinstance(parsed, dict) and parsed.get("csrfToken"):
            self.csrf = str(parsed["csrfToken"])
        return status, parsed

    def bootstrap_csrf(self) -> None:
        status, payload = self.call("GET", "/api/auth/csrf")
        if isinstance(payload, dict):
            self.csrf = str(payload.get("csrfToken") or self.csrf)
        log(f"{self.label}: 取 CSRF", status == 200 and bool(self.csrf), f"status={status}")


def seed_verification_code(email: str) -> str:
    """直接往 auth.sqlite3 写一条已知验证码。

    /api/auth/verification/request 只会把验证码写进日志（AUTH_EMAIL_DEBUG），
    冒烟脚本读不到日志，所以这里用 AuthStore 自己的方法插一条，确保注册步可自动化。
    """
    sys.path.insert(0, "/app")
    from backend.auth_store import AuthStore  # noqa: E402  —— 必须在 chdir/app 之后

    code = "424242"
    store = AuthStore(AUTH_DB)
    from datetime import datetime, timedelta, timezone

    store.create_verification_code(
        email, "signup", "", datetime.now(timezone.utc) + timedelta(minutes=10), 5, code=code
    )
    return code


def personal_flow() -> dict:
    out: dict[str, object] = {}
    email = f"user-{RUN}@{DOMAIN}"
    user = Client("个人")
    user.bootstrap_csrf()

    status, payload = user.call("GET", "/api/auth/config")
    ready = isinstance(payload, dict) and payload.get("publicSignupAvailable")
    log("个人: /api/auth/config 注册可用", bool(ready),
        f"status={status} code={payload.get('publicSignupUnavailableCode') if isinstance(payload, dict) else '?'}")

    code = seed_verification_code(email)
    status, payload = user.call(
        "POST", "/api/auth/register",
        {"email": email, "name": "预发测试用户", "password": PASSWORD, "verificationCode": code},
    )
    account_status = (payload.get("user") or {}).get("accountStatus") if isinstance(payload, dict) else None
    log("个人: 注册", status == 200, f"status={status} account={account_status} detail={str(payload)[:160]}")
    out["email"] = email

    status, payload = user.call("POST", "/api/auth/login", {"email": email, "password": PASSWORD})
    log("个人: 登录", status == 200, f"status={status} detail={str(payload)[:160]}")

    status, payload = user.call("GET", "/api/me/billing")
    config = (payload or {}).get("config", {}) if isinstance(payload, dict) else {}
    manual = config.get("manualPay", {}) if isinstance(config, dict) else {}
    log("个人: /api/me/billing 看到收款码渠道", bool(manual.get("enabled")) and "manual_qr" in (config.get("channels") or []),
        f"status={status} channels={config.get('channels')} methods={[m.get('method') for m in manual.get('methods', [])]} qr={[m.get('qrUrl') for m in manual.get('methods', [])]}")
    log("个人: 模拟充值已关闭", not config.get("mockPaymentEnabled", False) if isinstance(config, dict) else False,
        f"mockPaymentEnabled={config.get('mockPaymentEnabled')}")

    status, order = user.call(
        "POST", "/api/me/billing/orders",
        {"amount": 20, "paymentMethod": "alipay", "channel": "manual_qr",
         "cursorAmountUsd": 12, "claudeCodeAmountUsd": 8},
    )
    trade_no = order.get("tradeNo") if isinstance(order, dict) else None
    log("个人: 下 manual_qr 单", status == 200 and bool(trade_no),
        f"status={status} tradeNo={trade_no} money={order.get('moneyCny') if isinstance(order, dict) else '?'} "
        f"qrUrl={order.get('qrUrl') if isinstance(order, dict) else '?'} detail={str(order)[:160]}")
    out["tradeNo"] = trade_no
    if not trade_no:
        return out

    status, payload = user.call(
        "POST", f"/api/me/billing/orders/{trade_no}/submit",
        {"payerNote": f"预发测试 支付宝后四位 0000 订单 {trade_no}", "paymentReference": f"SMOKEP{trade_no[-16:]}"},
    )
    order_status = (payload.get("order") or {}).get("status") if isinstance(payload, dict) else None
    log("个人: 提交付款说明", status == 200, f"status={status} orderStatus={order_status} detail={str(payload)[:160]}")

    admin = Client("管理员")
    admin.bootstrap_csrf()
    status, payload = admin.call("POST", "/api/auth/dev-login", {"email": ADMIN_EMAIL})
    log("管理员: dev-login", status == 200 and bool(isinstance(payload, dict) and payload.get("isAdmin")),
        f"status={status} isAdmin={payload.get('isAdmin') if isinstance(payload, dict) else '?'} detail={str(payload)[:160]}")

    status, payload = admin.call("GET", "/api/admin/billing/orders")
    pending = payload.get("pendingReviewCount") if isinstance(payload, dict) else None
    log("管理员: 待确认列表含该单", status == 200 and bool(pending),
        f"status={status} pendingReviewCount={pending}")

    status, payload = admin.call(
        "POST", f"/api/admin/billing/orders/{trade_no}/complete", {"note": "预发演练：人工核账确认"},
    )
    synced = payload.get("entitlementSynced") if isinstance(payload, dict) else None
    settled = payload.get("settled") if isinstance(payload, dict) else None
    log("管理员: 确认到账", status == 200 and bool(settled),
        f"status={status} settled={settled} entitlementSynced={synced} account={(payload.get('account') if isinstance(payload, dict) else None)}")
    out["entitlementSynced"] = synced

    status, payload = user.call("GET", f"/api/me/billing/orders/{trade_no}")
    order_final = payload.get("order") if isinstance(payload, dict) else {}
    log("个人: 订单终态", status == 200 and (order_final or {}).get("status") == "success",
        f"status={status} orderStatus={(order_final or {}).get('status')} syncState={(order_final or {}).get('syncState')} "
        f"syncError={str((order_final or {}).get('syncError'))[:200]}")

    status, payload = user.call("GET", "/api/me/keys")
    log("个人: 个人密钥列表", status == 200,
        f"status={status} detail={str(payload)[:240]}")
    return out


def enterprise_flow() -> dict:
    out: dict[str, object] = {}
    admin_email = f"ent-admin-{RUN}@{DOMAIN}"
    org_name = f"预发测试企业-{RUN}"

    owner = Client("企业管理员")
    owner.bootstrap_csrf()
    code = seed_verification_code(admin_email)
    status, payload = owner.call(
        "POST", "/api/auth/register",
        {"email": admin_email, "name": "预发企业管理员", "password": PASSWORD, "verificationCode": code},
    )
    log("企业: 管理员先注册个人账号", status == 200, f"status={status} detail={str(payload)[:160]}")

    platform = Client("平台")
    platform.bootstrap_csrf()
    status, payload = platform.call("POST", "/api/auth/dev-login", {"email": ADMIN_EMAIL})
    log("企业: 平台 dev-login", status == 200, f"status={status}")

    status, created = platform.call(
        "POST", "/api/platform/organizations",
        {"name": org_name, "adminName": "预发企业管理员", "adminEmail": admin_email},
    )
    org_id = ((created or {}).get("organization") or {}).get("id") if isinstance(created, dict) else None
    log("企业: 建测试企业", status == 200 and bool(org_id),
        f"status={status} orgId={org_id} invitationStatus={(created or {}).get('invitationStatus') if isinstance(created, dict) else '?'} detail={str(created)[:200]}")
    out["organizationId"] = org_id
    if not org_id:
        return out

    upstream_ready = False
    for _ in range(24):
        status, payload = platform.call("GET", f"/api/platform/organizations/{org_id}")
        org = (payload or {}).get("organization") if isinstance(payload, dict) else {}
        if isinstance(org, dict) and org.get("upstreamStatus") == "active":
            upstream_ready = True
            out["upstreamOrganizationId"] = org.get("upstreamOrganizationId")
            break
        time.sleep(5)
    log("企业: outbox 把企业开通到桩上", upstream_ready,
        f"upstreamOrganizationId={out.get('upstreamOrganizationId')}")

    token = ""
    try:
        sys.path.insert(0, "/app")
        import asyncio  # noqa: E402

        from backend.organization_repository import PostgreSQLOrganizationRepository  # noqa: E402

        async def fetch_token() -> str:
            repo = PostgreSQLOrganizationRepository.from_environment()
            await repo.connect()
            row = await repo._require_pool().fetchrow(  # noqa: SLF001 - 预发取邀请链接没有公开接口
                "SELECT id FROM customer_invitation WHERE organization_id=$1 AND consumed_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC LIMIT 1",
                org_id,
            )
            if row is None:
                return ""
            return await repo.invitation_token_for_delivery(row["id"]) or ""

        token = asyncio.run(fetch_token())
    except Exception as exc:  # noqa: BLE001
        log("企业: 取邀请 token", False, f"{exc.__class__.__name__}: {exc}")
    log("企业: 取邀请 token", bool(token), f"len={len(token)}")
    if not token:
        return out

    status, payload = owner.call("POST", "/api/auth/invitations/accept", {"token": token})
    log("企业: 管理员接受邀请", status == 200, f"status={status} detail={str(payload)[:200]}")

    # 🔴 接受邀请只是绑定账号，并不建立会话；要先登录才能在 /api/auth/me 里看到企业身份
    # （否则 organizationRole 恒为 None，会被误读成「开通没完成」）。
    owner2 = Client("企业管理员(已登录)")
    owner2.bootstrap_csrf()
    status, _ = owner2.call("POST", "/api/auth/login", {"email": admin_email, "password": PASSWORD})
    log("企业: 管理员登录", status == 200, f"status={status}")

    member_active = False
    payload: object = {}
    for _ in range(24):
        status, payload = owner2.call("GET", "/api/auth/me")
        if isinstance(payload, dict) and payload.get("organizationRole") == "admin":
            member_active = True
            break
        time.sleep(5)
    log("企业: 成员开通完成（organizationRole=admin）", member_active,
        f"role={payload.get('organizationRole') if isinstance(payload, dict) else '?'} "
        f"canViewBilling={payload.get('canViewOrganizationBilling') if isinstance(payload, dict) else '?'}")
    if not member_active:
        return out

    status, payload = owner2.call("GET", "/api/organization/current/billing/orders")
    config = (payload or {}).get("config") if isinstance(payload, dict) else {}
    log("企业: 看到企业收款码", status == 200 and bool((config or {}).get("enabled")),
        f"status={status} qrUrl={(config or {}).get('qrUrl')} label={(config or {}).get('label')}")

    status, order = owner2.call(
        "POST", "/api/organization/current/billing/orders",
        {"amount": 500, "paymentMethod": "alipay", "channel": "manual_qr",
         "idempotencyKey": f"smoke-{RUN}"},
    )
    trade_no = ((order or {}).get("order") or {}).get("tradeNo") if isinstance(order, dict) else None
    # 🔴 已知断点（2026-10-31 预发实测）：企业收款码下单挂在 MOCK_PAYMENT_ENABLED 上。
    # require_organization_billing_topup_operator 要求 canSimulateOrganizationTopup，而那一项
    # = can_view_billing and (demo or billing.mock_payment_enabled())（main.py:2347）
    # ⇒ 关掉模拟充值时企业管理员下不了收款码单（403 ORGANIZATION_TOPUP_FORBIDDEN）；
    #    打开模拟充值又会同时放开「零付款直接发额度」。要改代码，配置绕不过去。
    if status == 403 and "ORGANIZATION_TOPUP_FORBIDDEN" in str(order):
        log("企业: 下企业单", False,
            "status=403 ORGANIZATION_TOPUP_FORBIDDEN —— 已知断点：企业收款码下单被 MOCK_PAYMENT_ENABLED 挡住，"
            "权限位 canSimulateOrganizationTopup 把『收款码人工核账』和『模拟充值』绑成了一个开关（main.py:2347）")
        out["blocker"] = "enterprise manual_qr order requires MOCK_PAYMENT_ENABLED"
        return out
    log("企业: 下企业单", status == 200 and bool(trade_no),
        f"status={status} tradeNo={trade_no} detail={str(order)[:200]}")
    if not trade_no:
        return out
    out["orgTradeNo"] = trade_no

    status, payload = owner2.call(
        "POST", f"/api/organization/current/billing/orders/{trade_no}/submit",
        {"payerNote": f"预发企业测试 订单 {trade_no}", "paymentReference": f"SMOKEO{trade_no[-16:]}"},
    )
    log("企业: 提交付款说明", status == 200,
        f"status={status} orderStatus={((payload or {}).get('order') or {}).get('status') if isinstance(payload, dict) else '?'}")

    status, payload = platform.call(
        "POST", f"/api/admin/organization-billing/orders/{trade_no}/complete",
        {"note": "预发演练：企业人工核账确认"},
    )
    record = (payload or {}).get("record") if isinstance(payload, dict) else None
    log("企业: 平台确认到账 + 入 ledger", status == 200 and bool(record),
        f"status={status} settled={(payload or {}).get('settled') if isinstance(payload, dict) else '?'} record={str(record)[:220]}")

    status, payload = owner2.call("GET", "/api/organization/current/billing")
    account = (payload or {}).get("account") if isinstance(payload, dict) else {}
    log("企业: 余额已到账", status == 200 and float((account or {}).get("availableBalanceUsd") or 0) > 0,
        f"status={status} account={json.dumps(account, ensure_ascii=False) if isinstance(account, dict) else account}")

    status, payload = platform.call("GET", "/api/health")
    org_health = (payload or {}).get("organization") if isinstance(payload, dict) else {}
    outbox = (org_health or {}).get("outbox") or {}
    # 判据是 failedCount，不是 pendingCount：outbox 里会留下 organization.invitation.created
    # 的 pending 行，但那是冒烟自己造成的 —— 脚本直接从库里取 token 并立刻接受了邀请，
    # 邀请被 consume 之后 invitation_token_for_delivery 就返回不了 token，投递任务只能一直重试
    # （退避 30s→3600s，8 次后转 failed）。真人流程是「先收到邮件再点链接」，不会出现这一行：
    # 单独跑一次「建企业但不接受邀请」的对照，该行是 sent（2026-10-31 实测）。
    log("企业: outbox 无 failed 行", status == 200 and not outbox.get("failedCount"),
        f"outbox={json.dumps(outbox, ensure_ascii=False)}"
        "（pending 的 invitation.created 是冒烟抢先 consume 造成，非缺陷）"
        f" settlement={json.dumps((org_health or {}).get('settlement'), ensure_ascii=False)[:160]}")
    return out


def main() -> int:
    os.chdir("/app")
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(f"=== myai 预发冒烟 run={RUN} base={BASE} 范围={which} ===", flush=True)
    status, payload = Client("健康").call("GET", "/api/health")
    log("/api/health", status == 200, f"status={status} overall={payload.get('status') if isinstance(payload, dict) else '?'}")

    results: dict[str, object] = {}
    if which in {"all", "personal"}:
        print("\n--- 个人链路 ---", flush=True)
        results["personal"] = personal_flow()
    if which in {"all", "enterprise"}:
        print("\n--- 企业链路 ---", flush=True)
        results["enterprise"] = enterprise_flow()

    print("\n=== 结果 ===", flush=True)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    if FAILURES:
        print(f"\n{len(FAILURES)} 步失败：", flush=True)
        for item in FAILURES:
            print("  - " + item, flush=True)
        return 1
    print("\n全部通过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
