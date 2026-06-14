# IT 工单：阿里云 ECS `节点` 安全组放行 TCP 4001-4011

**提交日期**：2026-05-20
**提交人**：刘国现
**优先级**：中（不阻塞当前生产，但阻塞新链路上线）
**预计 IT 操作时间**：3 分钟（控制台点击）

---

## 一句话需求

请在阿里云控制台为 ECS 实例 **`节点`** 的安全组增加 **入方向规则**：
**TCP 4001-4011 端口，源 = VPC 内网段（`172.16.0.0/12` 或更精确 `172.16.0.0/24`），动作=允许**。

---

## 实例信息（控制台搜这个）

| 字段 | 值 |
|------|----|
| 实例名 | `节点` |
| Instance ID | `i-t4nfnlrztca9efdetoqa` |
| Region | `ap-southeast-1`（新加坡） |
| 内网 IP | `172.16.0.228` |
| VPC ID | `vpc-t4nowh579qv9u5zj0i8d5` |
| 交换机 | `vsw-t4nffypvyzr56f39ctcf0` |

---

## 期望的安全组规则

| 方向 | 协议 | 端口范围 | 授权对象（源 CIDR） | 优先级 | 备注 |
|------|------|----------|---------------------|--------|------|
| 入方向 | TCP | `4001/4011` | `172.16.0.0/24` | 1 | carher-chatgpt-tunnel（**只放 VPC 内网，不开公网**） |

> ⚠️ **重要：源限制必须是内网段**，**不要 `0.0.0.0/0`**。这些端口反代的是 ChatGPT Pro 内部代理，公网暴露会被人扫到刷流量。

---

## 为什么需要这些端口

阿里云 `carher` 命名空间下的 LiteLLM Proxy 需要把 ChatGPT 流量打到联通 IDC 机房的 `JSZX-AI-03 (10.68.13.188)`，调用 11 个 ChatGPT Pro 共享订阅账号。

**当前**（要替换的旧链路）：
```
carher pod → cc.auto-link.com.cn (Cloudflare 免费 tunnel)
           → 联通 IDC 198 LiteLLM
           → 联通 IDC 188 LiteLLM-chatgpt-N 容器
           → chatgpt.com
```

**问题**：Cloudflare 免费 tunnel 跑长流式 AI 流量会触发 AUP 限速 / 带宽抖动 / 偶发 524。目前所有 200+ carher bot 实例和 IDE 用户（cursor/codex）都共享这一条 CF tunnel，已感到压力。

**新链路**（不走 CF）：
```
carher pod → 节点 内网 172.16.0.228:4001-4011 (本工单要开的)
           → 节点 的 ssh 反向隧道（已搭好）
           → JSZX-AI-03 (188) LiteLLM-chatgpt-N
           → chatgpt.com
```

- **完全走阿里云 VPC 内网**到 节点（亚毫秒延迟，零外部依赖）
- 节点 ↔ 188 之间用 SSH 反向隧道（加密，已部署完成，188 outbound 走 22 端口）
- 带宽走阿里云 ECS 内网+联通 IDC outbound，不再吃 CF 免费额度

---

## 当前已完成的部署（IT 这边只缺安全组）

| 步骤 | 状态 |
|------|------|
| 节点 启用 `GatewayPorts clientspecified` | ✅ |
| 188 ↔ 节点 SSH 公钥互信 | ✅ |
| 188 → 节点 SSH 反向隧道（127.0.0.1:4001-4011） | ✅ 已起 |
| 节点 本机自测 `curl 127.0.0.1:4002 → 200 OK` | ✅ |
| **阿里云 ECS 安全组放行 4001-4011 入向** | ❌ **需 IT 协助** |
| carher LiteLLM ConfigMap 切到新链路 | 等上一步通了再做 |

---

## 端口分配（11 个，每个对应一个 ChatGPT Pro 账号）

| 端口 | 后端容器 | 备注 |
|------|----------|------|
| 4001 | litellm-chatgpt (acct-1) | legacy slot，可选 |
| 4002 | litellm-chatgpt-2 (acct-2) | 主力 |
| 4003 | litellm-chatgpt-3 (acct-3) | 主力 |
| 4004 | litellm-chatgpt-4 (acct-4) | 主力 |
| 4005 | litellm-chatgpt-5 (acct-5) | 主力 |
| 4006 | litellm-chatgpt-6 (acct-6) | 主力 |
| 4007 | litellm-chatgpt-7 (acct-7) | 主力 |
| 4008 | litellm-chatgpt-8 (acct-8) | 主力 |
| 4009 | litellm-chatgpt-9 (acct-9) | 主力 |
| 4010 | litellm-chatgpt-10 (acct-10) | 主力 |
| 4011 | litellm-chatgpt-11 (acct-11) | 主力 |

---

## 验证方式（开完后我自测）

操作完成后，从阿里云任意 ECS 或 K8s pod 执行：

```bash
nc -vz 172.16.0.228 4002
# 期望: Connection to 172.16.0.228 4002 port [tcp/*] succeeded!

curl -sS http://172.16.0.228:4002/v1/models \
  -H 'Authorization: Bearer <litellm-key>'
# 期望: HTTP 200，返回 chatgpt-gpt-5.5 等模型列表
```

---

## 安全说明

- **源 CIDR 严格限制**：只允许 VPC 内网 `172.16.0.0/24`，**禁止 `0.0.0.0/0` 公网**
- **加密链路**：节点 → 188 之间走 SSH（AES-256-GCM），不是明文 HTTP
- **鉴权**：访问端口需带 LiteLLM master key (Bearer Token)，无 key 直接 401
- **限流**：上游 ChatGPT 账号本身有 5h/7d 滚动 rate-limit，撞顶自动 fallback 到 wangsu

---

## 联系人

| 角色 | 联系 |
|------|------|
| 申请人 | 刘国现 |
| 系统熟悉人 | 刘国现 |
| 涉及业务 | CarHer bot 集群 ChatGPT Pro 接入 |
