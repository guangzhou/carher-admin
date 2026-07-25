# zerokey-codex-bridge 集群镜像
# 纯 Python 标准库,无第三方依赖 → 极简镜像。
# 构建(必须在 47.84.112.136 用 nerdctl,禁本地 Mac):
#   nerdctl build -t <ACR>/her/zerokey-codex-bridge:latest -f Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY zerokey-codex-responses-bridge.py /app/bridge.py

# 集群内默认:监听所有网卡,上游由 Deployment env 注入 svc DNS
ENV BRIDGE_LISTEN=0.0.0.0:8788 \
    BRIDGE_FANOUT=1 \
    BRIDGE_MODEL=gpt-5.6-terra \
    BRIDGE_LOG=/tmp/zk_bridge.log \
    BRIDGE_SPENDLOG=/tmp/zk_spendlog.jsonl

EXPOSE 8788
CMD ["python3", "/app/bridge.py"]
