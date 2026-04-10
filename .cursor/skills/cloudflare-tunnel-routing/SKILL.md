---
name: cloudflare-tunnel-routing
description: >-
  Cloudflare Tunnel routing for CarHer K8s instances.
  Use when a new instance returns 404 on its OAuth URL, when debugging
  Cloudflare tunnel routing issues, when adding/removing instances from
  the tunnel, or when the user mentions cloudflare, tunnel, 404, DNS,
  ingress, or route.
---

# Cloudflare Tunnel Routing — CarHer K8s

## 架构概览

```
用户浏览器
  → Cloudflare Edge (根据 DNS CNAME 找到 tunnel)
  → Cloudflare 远程 ingress 配置 (hostname → service 映射)
  → cloudflared pod (K8s 内)
  → 后端 Service (carher-{uid}-svc)
```

## 关键事实

1. **Tunnel `carher-k8s` 是远程托管模式** (`config_src: "cloudflare"`)
2. **真正生效的 ingress 是 Cloudflare 云端配置**，不是 K8s ConfigMap
3. 每个新实例需要同时满足两个条件才能通：
   - **DNS CNAME 记录**：`s1-u{id}-auth.carher.net` → `{tunnel_uuid}.cfargotunnel.com`
   - **远程 ingress 规则**：hostname → `http://{ClusterIP}:{port}`
4. `carher-admin` 必须带上 **`CLOUDFLARE_API_TOKEN`**，否则它只能创建实例，
   但无法通过 Cloudflare API 更新远程 ingress

## 新增实例前的前置检查

```bash
# secret 里必须有 cloudflare-api-token
kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.cloudflare-api-token}' | base64 -d

# admin Pod 里环境变量必须为 true
kubectl exec -n carher deploy/carher-admin -- \
  python -c "import os; print(bool(os.environ.get('CLOUDFLARE_API_TOKEN')))"
```

如果这里为空或返回 `False`：

- 现在 `POST /api/instances` / `POST /api/instances/batch-import` 会直接返回 `503`
- 这是**故意 fail-fast**，避免再出现“实例创建成功但 callback URL 实际 404”的静默故障

## 常量

```python
ACCOUNT_ID = "67e6618e6af7e4342cbd1de02536fa2f"
TUNNEL_ID  = "0e83a70f-93d9-4c17-86cc-7600f52696a2"
ZONE_ID    = "3748a528561bd0e67f85d1ef23271612"
CF_TOKEN   = os.environ["CLOUDFLARE_API_TOKEN"]
DOMAIN     = "carher.net"
```

## 新实例上线完整流程

当 `carher-admin` 创建实例后（CRD → Operator → Pod/Service 就绪），还需要：

### Step 1: 注册 DNS CNAME

通过 cloudflared pod 执行（已在 `cloudflare_ops.register_dns_routes()` 实现）：

```bash
kubectl -n carher exec deploy/cloudflared -- \
  cloudflared tunnel route dns --overwrite-dns carher-k8s \
  s1-u{uid}-auth.carher.net
# 重复 fe / proxy
```

或通过 Cloudflare API：

```bash
curl -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"CNAME","name":"s1-u{uid}-auth.carher.net",
       "content":"0e83a70f-93d9-4c17-86cc-7600f52696a2.cfargotunnel.com",
       "proxied":true}'
```

### Step 2: 更新远程 Tunnel ingress（关键！）

这一步是之前遗漏的。必须通过 Cloudflare API 更新远程配置：

```python
import json, os, urllib.request

ACCOUNT = "67e6618e6af7e4342cbd1de02536fa2f"
TUNNEL  = "0e83a70f-93d9-4c17-86cc-7600f52696a2"
TOKEN   = os.environ["CLOUDFLARE_API_TOKEN"]
URL     = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations"

# 1) GET 当前配置
req = urllib.request.Request(URL, headers={
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
})
with urllib.request.urlopen(req) as r:
    config = json.loads(r.read())["result"]["config"]

ingress = config["ingress"]
catch_all = ingress[-1]  # {"service": "http_status:404"}

# 2) 追加新用户的 hostname 规则（在 catch-all 之前）
svc_ip = "<新实例 ClusterIP>"   # kubectl get svc carher-{uid}-svc -o jsonpath='{.spec.clusterIP}'
new_rules = [
    {"hostname": f"s1-u{uid}-auth.carher.net", "service": f"http://{svc_ip}:18891", "originRequest": {}},
    {"hostname": f"s1-u{uid}-fe.carher.net",   "service": f"http://{svc_ip}:8000",  "originRequest": {}},
    {"hostname": f"s1-u{uid}-proxy.carher.net", "service": f"http://{svc_ip}:8080",  "originRequest": {}},
]
config["ingress"] = ingress[:-1] + new_rules + [catch_all]

# 3) PUT 更新
put_data = json.dumps({"config": config}).encode()
put_req = urllib.request.Request(URL, data=put_data, method="PUT", headers={
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
})
with urllib.request.urlopen(put_req) as r:
    print(json.loads(r.read())["success"])  # True
```

### Step 3: 验证

```bash
# 应该返回 HTTP 400（OAuth code 无效），不是 404
curl -sS -o /dev/null -w "%{http_code}" \
  "https://s1-u{uid}-auth.carher.net/feishu/oauth/callback?code=test&state=test"
```

如果是通过 Admin API 创建的，还要检查返回体里的 `cloudflare`：

```json
{
  "cloudflare": {
    "ok": true,
    "message": "DNS + remote tunnel ingress synced"
  }
}
```

## 排查 404 问题

当某个实例的 OAuth URL 返回 404 时，按顺序检查：

| # | 检查项 | 命令 | 正常结果 |
|---|--------|------|----------|
| 1 | DNS CNAME 存在 | `curl -s "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records?name=s1-u{uid}-auth.carher.net" -H "Authorization: Bearer $CF_TOKEN"` | result 非空 |
| 2 | 远程 ingress 包含此 hostname | GET `accounts/$ACCOUNT/cfd_tunnel/$TUNNEL/configurations` → 检查 ingress 数组 | hostname 在列表中 |
| 3 | K8s Service 存在 | `kubectl get svc carher-{uid}-svc -n carher` | ClusterIP 有效 |
| 4 | Pod Running | `kubectl get pod -l app=carher-{uid} -n carher` | STATUS=Running |

**90% 的 404 是因为缺了第 2 步（远程 ingress 没有这个 hostname）。**

## 删除实例

删除时也需要从远程 ingress 中移除对应 hostname，避免配置膨胀。
逻辑同 Step 2，但改为过滤掉该 uid 的 3 条规则后 PUT 回去。

## 与 K8s ConfigMap 的关系

`cloudflared-config` ConfigMap 仍然有用：
- cloudflared 启动时先读本地 config.yml
- 但随后 Cloudflare 云端下发远程配置覆盖它

所以 **ConfigMap 只影响启动瞬间**，运行时完全由远程配置控制。
`cloudflare_ops.sync_tunnel_config()` 更新的是 ConfigMap，对已运行的 tunnel 不产生路由变更。

## 端口映射

每个实例暴露 3 个 hostname，对应不同端口：

| hostname 后缀 | Service 端口 | 用途 |
|---------------|-------------|------|
| `-auth` | 18891 | 飞书 OAuth 回调 + WebSocket |
| `-fe` | 8000 | Web 前端 |
| `-proxy` | 8080 | API 代理 |

## 基础设施路由（非实例）

除了 per-instance 路由外，一些基础设施服务也需要通过 tunnel 暴露。
这些路由在 `cloudflare_ops.INFRA_ROUTES` 中集中定义，`generate_config()` 和
`update_remote_ingress()` 会自动包含它们，无需手动维护。

当前注册的基础设施路由：

| hostname | K8s Service | 端口 | 用途 |
|----------|-------------|------|------|
| `litellm.carher.net` | `litellm-proxy` | 4000 | LiteLLM 代理 + Web UI |

如需新增基础设施路由，只需在 `cloudflare_ops.py` 的 `INFRA_ROUTES` 列表中追加一个
`(hostname_prefix, service_name, port)` 元组即可。

## 历史教训

2026-04-07: 新增 u173~u178 后用户报 404。根因是远程 tunnel ingress
只到 u172，新用户没被加进去。修 ConfigMap、重启 cloudflared、加 DNS 记录
都无效，因为远程配置覆盖了本地配置。最终通过 Cloudflare API PUT
远程 ingress 配置解决。

2026-04-09: LiteLLM UI (`litellm.carher.net`) 返回 502。根因是 `generate_config()`
和 `update_remote_ingress()` 只遍历 carher 实例的 Service，不包含基础设施路由。
手动加到 ConfigMap 的 litellm 路由在下次 `sync_tunnel_config()` 时被覆盖。
修复方案：引入 `INFRA_ROUTES` 常量，在配置生成逻辑中自动包含基础设施路由。

2026-04-10: 新增 u179~u193 的 callback URL 配置值正确，但全部返回 404。根因不是
实例本身，而是 `carher-admin-secrets` 缺少 `cloudflare-api-token`，导致 admin
创建实例时无法调用 Cloudflare API 更新远程 ingress。修复方案：
1. 给 `carher-admin` 补 token 并重启
2. 补跑 `register_dns_routes()` + `update_remote_ingress()`
3. API 创建接口增加 fail-fast：无 token 时直接 `503`
4. API 响应增加 `cloudflare.ok/message`，不再静默成功
