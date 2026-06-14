# Deprecated: K8s-based deployment (CLI-subprocess proxy, v1/v2)

These were used 2026-05-23 morning when the proxy ran as a K8s Pod on the
Aliyun carher cluster (`default/claude-max-proxy`, node 86 hostNetwork).

Superseded by:
- **Deployment**: 188 Docker (see `../claude-max-proxy.Dockerfile` +
  `../docker-compose.claude-max-proxy.yml`)
- **Architecture**: transparent passthrough (no CLI subprocess) — see
  `../claude-max-proxy.py` v3

Reasons for migrating off K8s:
1. Aliyun → 198 LiteLLM cross-network 不通; 188 同内网更稳
2. Aliyun EIP DNAT 对非 22 端口仅响应 SYN, data 不转发 (实测)
3. v3 透传式不需要 hostPath 挂 claude CLI binary

Kept for historical reference / disaster-recovery rollback.
