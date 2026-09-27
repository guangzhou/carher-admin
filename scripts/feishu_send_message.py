#!/usr/bin/env python3
"""飞书机器人发消息 —— 按邮箱 / user_id / union_id / open_id / 群 chat_id 寻址。

⚠️ open_id 是 per-app 的：别的 app（含 lark-cli 那个）查出来的 `ou_` 塞进来会直接
`99992361 open_id cross app`。跨 app 场景传 user_id 或 union_id，或先 `--resolve-only`
换成本 app 视角的 open_id。

凭据只从环境变量读，不接受命令行明文（避免进 shell history / ps）：
    export FEISHU_APP_ID=cli_xxx
    export FEISHU_APP_SECRET=xxx

用法：
    python3 scripts/feishu_send_message.py --to zhangkairui@auto-link.com.cn --text "hello"
    python3 scripts/feishu_send_message.py --to zhangkairui --email-domain auto-link.com.cn --text "hi"
    python3 scripts/feishu_send_message.py --to 4ae52ag1 --to-type user_id --text "hello"
    python3 scripts/feishu_send_message.py --to oc_xxx --markdown-file note.md
    echo "hello" | python3 scripts/feishu_send_message.py --to zhangkairui@... --text -
    ... --dry-run        # 只取 token + 解析收件人，不真发
    ... --resolve-only   # user_id/union_id → 本 app 视角 open_id（含 round-trip 校验）

退出码：0 成功 / 2 参数或收件人解析错 / 3 鉴权或权限失败 / 4 发送失败
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("FEISHU_OPEN_BASE", "https://open.feishu.cn")
TOKEN_TTL_SAFETY = 60  # 提前 60s 认为 token 过期


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, payload: dict | None = None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.payload = payload or {}


def _request(method: str, path: str, *, token: str | None = None,
             body: dict | None = None, timeout: float = 15.0) -> dict:
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # 飞书业务错误也走 4xx，body 里有 code/msg
        raw = exc.read().decode(errors="replace")
        try:
            payload = json.loads(raw)
        except ValueError:
            raise FeishuError(exc.code, f"HTTP {exc.code}: {raw[:300]}") from exc
    if payload.get("code", 0) != 0:
        raise FeishuError(payload.get("code", -1), payload.get("msg", "unknown"), payload)
    return payload


class FeishuBot:
    """tenant_access_token 自动缓存 + 过期重取。"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expire_at = 0.0

    @property
    def token(self) -> str:
        if self._token and time.time() < self._expire_at:
            return self._token
        payload = _request(
            "POST", "/open-apis/auth/v3/tenant_access_token/internal",
            body={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        self._token = payload["tenant_access_token"]
        self._expire_at = time.time() + max(payload.get("expire", 7200) - TOKEN_TTL_SAFETY, 0)
        return self._token

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            return _request(method, path, token=self.token, body=body)
        except FeishuError as exc:
            if exc.code not in (99991663, 99991668):  # token 失效/无效，重取一次再打
                raise
            self._token = ""
            return _request(method, path, token=self.token, body=body)

    def send(self, receive_id: str, receive_id_type: str, msg_type: str,
             content: dict, uuid: str | None = None) -> dict:
        body = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        if uuid:
            body["uuid"] = uuid
        return self.call(
            "POST", f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}", body
        )

    def per_app_open_id(self, ident: str, id_type: str = "user_id") -> dict:
        """把 user_id / union_id 换成**本 app 视角**的 open_id，并强制 round-trip 反查。

        单查 users/{id} 不需要 contact:user.id:readonly（只有 batch_get_id 需要）。
        round-trip 是为了让跨租户在这里就暴露，而不是等发消息时报一个看不懂的错。
        """
        user = self.call(
            "GET", f"/open-apis/contact/v3/users/{ident}?user_id_type={id_type}"
        )["data"]["user"]
        back = self.call(
            "GET", f"/open-apis/contact/v3/users/{user['open_id']}?user_id_type=open_id"
        )["data"]["user"]
        if back.get("name") != user.get("name"):
            raise FeishuError(-1, f"round-trip 校验失败：{user.get('name')} != {back.get('name')}")
        return {"open_id": user["open_id"], "union_id": user.get("union_id"),
                "user_id": user.get("user_id"), "name": user.get("name"),
                "email": user.get("email")}

    def find_open_id_by_name(self, name: str) -> str | None:
        """兜底：在机器人所在的群里按姓名找 open_id。

        只在收件人给的是姓名、且本 app 没有 contact 权限时用得上；
        机器人和对方没有共同群时必然找不到，返回 None 而不是抛错。
        """
        page_token = ""
        while True:
            q = f"?page_size=100{'&page_token=' + page_token if page_token else ''}"
            chats = self.call("GET", f"/open-apis/im/v1/chats{q}")["data"]
            for chat in chats.get("items", []):
                members = self.call(
                    "GET", f"/open-apis/im/v1/chats/{chat['chat_id']}/members?page_size=100"
                )["data"]
                for m in members.get("items", []):
                    if m.get("name") == name:
                        return m["member_id"]
            if not chats.get("has_more"):
                return None
            page_token = chats.get("page_token", "")
            if not page_token:
                return None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USER_ID_RE = re.compile(r"^(?=.*\d)[0-9a-z]{8}$")  # 同租户跨 app 稳定的 8 位 base36（如 4ae52ag1，
                                                   # 不是 hex）；要求含数字，免得把 8 字母的名字误判
TO_TYPES = ("open_id", "union_id", "user_id", "email", "chat_id")


def resolve_receiver(bot: FeishuBot, raw: str, email_domain: str | None,
                     to_type: str | None = None) -> tuple[str, str]:
    """返回 (receive_id, receive_id_type)。

    ⚠️ open_id 是 **per-app** 的：别的 app（含 lark-cli 那个）查出来的 ou_ 对本 app 无效，
    塞进来会以 230001/不存在收场。跨 app 场景请传 user_id 或 union_id，
    它们是同租户稳定的；本 app 用 users/{id} 单查换自己视角的 open_id（这条不需要
    contact:user.id:readonly，只有 batch_get_id 需要 —— 2026-09-27 实测）。

    手机号不在支持之列（receive_id_type 没有 mobile），显式报错而不是猜。
    """
    raw = raw.strip()
    if to_type:
        if to_type not in TO_TYPES:
            raise LookupError(f"--to-type 只能是 {'/'.join(TO_TYPES)}")
        return raw, to_type
    if raw.startswith("ou_"):
        return raw, "open_id"
    if raw.startswith("on_"):
        return raw, "union_id"
    if raw.startswith("oc_"):
        return raw, "chat_id"
    if EMAIL_RE.match(raw):
        return raw, "email"
    if raw.lstrip("+").isdigit():
        raise LookupError(
            f"{raw!r} 看着是手机号，但 im/v1/messages 的 receive_id_type 不支持 mobile。"
            f"请传邮箱，或先换成 user_id 后加 --to-type user_id。"
        )
    if USER_ID_RE.match(raw):
        return raw, "user_id"
    if email_domain:
        return f"{raw}@{email_domain.lstrip('@')}", "email"
    open_id = bot.find_open_id_by_name(raw)
    if open_id:
        return open_id, "open_id"
    raise LookupError(
        f"无法解析收件人 {raw!r}：不是 open_id/union_id/user_id/chat_id/邮箱，"
        f"且机器人所在的群里没有叫这个名字的人。请传邮箱，或加 --email-domain。"
    )


def build_content(args: argparse.Namespace) -> tuple[str, dict]:
    if args.text is not None:
        text = sys.stdin.read() if args.text == "-" else args.text
        return "text", {"text": text}
    if args.markdown is not None or args.markdown_file:
        if args.markdown_file:
            with open(args.markdown_file, encoding="utf-8") as fh:
                md = fh.read()
        else:
            md = sys.stdin.read() if args.markdown == "-" else args.markdown
        # 富文本卡片：Feishu 的 markdown 消息走 interactive 卡片
        return "interactive", {
            "schema": "2.0",
            "body": {"elements": [{"tag": "markdown", "content": md}]},
        }
    print("必须给 --text / --markdown / --markdown-file 之一", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="飞书机器人发消息")
    ap.add_argument("--to", required=True,
                    help="收件人：邮箱 / open_id(ou_) / union_id(on_) / user_id(8位hex) / "
                         "群 chat_id(oc_) / 姓名")
    ap.add_argument("--to-type", choices=TO_TYPES,
                    help="显式指定 --to 的类型，跳过自动判别")
    ap.add_argument("--email-domain", default=os.environ.get("FEISHU_EMAIL_DOMAIN"),
                    help="收件人是裸名字时用来拼企业邮箱，如 auto-link.com.cn")
    ap.add_argument("--text", help="纯文本内容；传 - 从 stdin 读")
    ap.add_argument("--markdown", help="markdown 内容（发成卡片）；传 - 从 stdin 读")
    ap.add_argument("--markdown-file", help="从文件读 markdown")
    ap.add_argument("--uuid", help="幂等键，同一 uuid 重发不会重复投递")
    ap.add_argument("--dry-run", action="store_true", help="只取 token + 解析收件人，不真发")
    ap.add_argument("--resolve-only", action="store_true",
                    help="只把 --to（user_id/union_id/open_id）换成**本 app 视角**的 "
                         "per-app open_id 并 round-trip 校验，打印身份后退出，不发消息")
    args = ap.parse_args(argv)

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量", file=sys.stderr)
        return 2

    if args.resolve_only:
        bot = FeishuBot(app_id, app_secret)
        try:
            ident, id_type = resolve_receiver(bot, args.to, args.email_domain, args.to_type)
            if id_type == "chat_id":
                print("--resolve-only 不适用于群 chat_id", file=sys.stderr)
                return 2
            if id_type == "email":
                print("邮箱不能直接换 open_id（需要 contact:user.id:readonly 的 batch_get_id）；"
                      "邮箱本身就能直接发，不必解析。", file=sys.stderr)
                return 2
            print(json.dumps(bot.per_app_open_id(ident, id_type), ensure_ascii=False))
        except FeishuError as exc:
            print(f"解析失败：{exc}", file=sys.stderr)
            return 3
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0

    msg_type, content = build_content(args)
    bot = FeishuBot(app_id, app_secret)

    try:
        receive_id, receive_id_type = resolve_receiver(
            bot, args.to, args.email_domain, args.to_type
        )
    except FeishuError as exc:
        print(f"鉴权或权限失败：{exc}", file=sys.stderr)
        return 3
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "receive_id": receive_id,
                          "receive_id_type": receive_id_type, "msg_type": msg_type},
                         ensure_ascii=False))
        return 0

    try:
        resp = bot.send(receive_id, receive_id_type, msg_type, content, args.uuid)
    except FeishuError as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        return 4

    data = resp.get("data", {})
    print(json.dumps({"ok": True, "message_id": data.get("message_id"),
                      "chat_id": data.get("chat_id"), "receive_id": receive_id,
                      "receive_id_type": receive_id_type}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
