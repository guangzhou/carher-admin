#!/usr/bin/env bash
# Wrapper: zerokey-pool smoke — 每个端口发随机数字，间隔 5-10s
exec python3 "$(dirname "$0")/zerokey-pool-smoke.py" "$@"
