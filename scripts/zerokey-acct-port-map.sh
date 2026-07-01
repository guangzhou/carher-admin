#!/usr/bin/env bash
# Wrapper: zerokey 端口 ↔ 198 chatgpt-acct 映射
exec python3 "$(dirname "$0")/zerokey_acct_port_map.py" "$@"
