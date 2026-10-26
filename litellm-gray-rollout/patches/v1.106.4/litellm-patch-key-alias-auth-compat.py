#!/usr/bin/env python3
"""
LiteLLM 1.105.1 —— key 别名鉴权回到 1.103.2 语义（上游 #43049 的反向补丁，只动鉴权，不动路由）。

#43049（1.104 起）：key 的 `aliases` 把请求名映射走以后，鉴权**只检查映射后的目标**是否在 key 的
`models` 白名单里，请求名本身不再算数。198 的 key 是「白名单写对外名、aliases 把对外名指到内部组」
（如 models 含 claude-opus-5，aliases claude-opus-5 → kiro-claude-opus-5）：2026-10-17 实测
2116 把 key 里 1323 把会对白名单内的名字直接 403（灰度 pod 复现：同一把克隆 key，1.103.2 200 / 1.105.1 403）。

做法：`key_model_aliases_for_auth_check()` 恒返回 None。上游所有鉴权调用点都经它取 key 别名，
返回 None 时 `_can_object_call_model` 等走的正是 #43049 之前的分支（请求名 + 全局/路由别名）。
请求真正发往哪个组仍由 common_request_processing / litellm_pre_call_utils 按 key aliases 改写，
那两处不经过本函数，不受影响。

用法：python3 litellm-patch-key-alias-auth-compat.py [site-packages/litellm 目录]
幂等：已打过就 SKIP 返回 1；锚点次数≠1 ⇒ 不写，返回 2。
"""

import glob
import os
import sys

ROOT = "/app/.venv/lib/python3.13/site-packages/litellm"
REL = "proxy/auth/auth_checks.py"
MARKER = "carher-patch: key aliases do not narrow auth"
ANCHOR = (
    "def key_model_aliases_for_auth_check(valid_token: UserAPIKeyAuth | None) -> Mapping[str, str] | None:\n"
    "    return alias_map(valid_token.aliases) if valid_token is not None and valid_token.aliases else None\n"
)
REPLACEMENT = (
    "def key_model_aliases_for_auth_check(valid_token: UserAPIKeyAuth | None) -> Mapping[str, str] | None:\n"
    "    # carher-patch: key aliases do not narrow auth (#43049 reverted for auth only; routing still uses\n"
    "    # key aliases). Keeps 1.103.2 semantics: the requested name in key.models is enough.\n"
    "    return None\n"
)


def main(root: str) -> int:
    path = os.path.join(root, REL)
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    if MARKER in content:
        print(f"SKIP: already patched {path}")
        return 1
    n = content.count(ANCHOR)
    if n != 1:
        print(f"ERROR: anchor matched {n} times (want 1); nothing written")
        return 2
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content.replace(ANCHOR, REPLACEMENT, 1))
    for pyc in glob.glob(os.path.join(os.path.dirname(path), "__pycache__", "auth_checks*.pyc")):
        os.remove(pyc)
    print(f"OK: key alias auth compat applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ROOT))
