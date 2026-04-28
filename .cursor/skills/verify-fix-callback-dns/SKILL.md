---
name: verify-fix-callback-dns
description: >-
  Verify and fix Cloudflare DNS CNAME records for all CarHer bot instances.
  Use when OAuth callbacks return 502, when migrating instances from S3/Docker
  to K8s, when DNS records might point to old/wrong tunnels, or when the user
  mentions "回调 502"、"callback 502"、"DNS 修复"、"tunnel CNAME".
---

# 验证与修复 Her 实例回调 DNS

## 背景

CarHer 使用 Cloudflare Tunnel 暴露 K8s 内的 bot 实例。每个实例有 3 条 DNS CNAME 记录
（auth/fe/proxy），必须指向当前 K8s tunnel UUID。

从 S3/Docker 迁移到 K8s 的旧实例，DNS 可能仍指向旧 tunnel，导致 OAuth 回调（及 fe/proxy）
返回 502，但飞书 WS 聊天不受影响（WS 不走 Cloudflare）。

## 常量

```
K8s Tunnel UUID:  0e83a70f-93d9-4c17-86cc-7600f52696a2
Tunnel Name:      carher-k8s
CNAME Target:     0e83a70f-93d9-4c17-86cc-7600f52696a2.cfargotunnel.com
CF Account ID:    67e6618e6af7e4342cbd1de02536fa2f
CF Zone ID:       3748a528561bd0e67f85d1ef23271612
Domain:           carher.net
```

## 前置：获取 Cloudflare API Token

```bash
CF_TOKEN=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.cloudflare-api-token}' | base64 -d)
echo "Token: ${CF_TOKEN:0:8}..."
```

## Step 1：批量检查所有活跃实例的 DNS

用 Python 脚本批量检查。核心逻辑：

1. 从 K8s CRD 获取所有活跃实例（`kubectl get her -n carher`）
2. 从 Cloudflare API 分页获取所有 CNAME 记录
3. 交叉比对：找出指向错误 tunnel 的记录和缺失的记录

```python
import json, urllib.request, subprocess

CF_TOKEN = "<token>"
ZONE_ID = "3748a528561bd0e67f85d1ef23271612"
CORRECT_TUNNEL = "0e83a70f-93d9-4c17-86cc-7600f52696a2"
CORRECT_CONTENT = f"{CORRECT_TUNNEL}.cfargotunnel.com"

# 获取活跃实例 hostname 集合
result = subprocess.run(
    ["kubectl", "get", "her", "-n", "carher", "-o",
     "custom-columns=UID:.spec.userId,PREFIX:.spec.prefix",
     "--no-headers"], capture_output=True, text=True)
active = set()
for line in result.stdout.strip().split("\n"):
    parts = line.split()
    if len(parts) >= 2:
        uid, prefix = parts[0], parts[1]
        for suffix in ["auth", "fe", "proxy"]:
            active.add(f"{prefix}-u{uid}-{suffix}.carher.net")

# 分页获取所有 CNAME 记录
all_records = []
page = 1
while True:
    url = f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records?type=CNAME&per_page=100&page={page}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    records = data["result"]
    all_records.extend(records)
    if len(records) < 100:
        break
    page += 1

# 分类
wrong, correct_count, found = [], 0, set()
for rec in all_records:
    name = rec["name"]
    if name not in active:
        continue
    found.add(name)
    content = rec["content"]
    if ".cfargotunnel.com" in content:
        tunnel = content.replace(".cfargotunnel.com", "")
        if tunnel == CORRECT_TUNNEL:
            correct_count += 1
        else:
            wrong.append({"id": rec["id"], "name": name})

missing = active - found
print(f"Correct: {correct_count}, Wrong: {len(wrong)}, Missing: {len(missing)}")
```

## Step 2：修复错误记录（PATCH）

```python
for rec in wrong:
    url = f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records/{rec['id']}"
    body = json.dumps({"content": CORRECT_CONTENT}).encode()
    req = urllib.request.Request(url, data=body, method="PATCH", headers={
        "Authorization": f"Bearer {CF_TOKEN}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    print(f"{'OK' if resp.get('success') else 'FAIL'}: {rec['name']}")
```

## Step 3：创建缺失记录

优先使用 cloudflared pod 的 `tunnel route dns` 命令（不受 API 配额限制）：

```bash
CLOUDFLARED_POD=$(kubectl get pod -n carher -l app=cloudflared \
  -o jsonpath='{.items[0].metadata.name}')

for hostname in <missing_hostnames>; do
  kubectl exec -n carher $CLOUDFLARED_POD -- \
    cloudflared tunnel route dns --overwrite-dns carher-k8s "$hostname"
done
```

如果 Cloudflare API 返回 `81045 Record quota exceeded`：
1. 先清理过期 DNS 记录（不属于任何活跃实例且指向旧 tunnel 的 CNAME）
2. 再通过 cloudflared pod 创建

## Step 4：清理过期 DNS 记录

过期记录 = 指向旧 tunnel + 不属于任何活跃 K8s 实例的 CNAME。

```python
stale = [rec for rec in all_records
         if rec["name"] not in active
         and ".cfargotunnel.com" in rec["content"]
         and CORRECT_TUNNEL not in rec["content"]
         and rec["name"] not in keep_set]  # keep_set = {"admin.carher.net", ...}

for rec in stale:
    url = f"https://api.cloudflare.com/client/v4/zones/{ZONE_ID}/dns_records/{rec['id']}"
    req = urllib.request.Request(url, method="DELETE", headers={
        "Authorization": f"Bearer {CF_TOKEN}"})
    urllib.request.urlopen(req, timeout=15)
```

## Step 5：验证

```bash
# 抽样验证若干实例
for inst in s1-u3 s2-u6 s1-u176 s1-u197; do
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://${inst}-auth.carher.net/oauth/callback")
  echo "${inst}-auth: $code"
done
```

| HTTP 状态 | 含义 |
|-----------|------|
| 404 | 正常（无 OAuth code 参数） |
| 400 | 正常（无效 OAuth code） |
| 200 | 正常（有效 OAuth 流程） |
| 502 | 异常：DNS 指向错误 tunnel，或实例已暂停/无 Pod |
| 超时 | DNS 记录缺失 |

暂停的实例（`paused=true`）返回 502 是正常的（无 Pod 运行）。

## 已知旧 Tunnel UUID

从 S3/Docker 迁移遗留：

| UUID | 来源 |
|------|------|
| `d18effca-6456-4b6c-b735-94dbbdc83299` | S1 Docker (旧) |
| `d4180094-9f8a-4693-99ee-721412df1b4e` | S2 Docker (旧) |
| `750fb00c-6572-4d7c-bed4-60c1a9c3107f` | S3 Docker (旧) |
| `e643eb62-165e-4866-bf81-a2c0579de7fe` | 其他旧环境 |
| `b7193051-8f24-42fc-97d2-6de4b5ac64d3` | Mac 开发环境 |

以上 UUID 均已废弃，任何指向它们的 CNAME 都应更新或删除。

## 历史修复记录

- **2026-04-17**: 全量检查发现 56 条错误（37+19）+ 13 条缺失 + 277 条过期。
  全部修复并清理。根因：S3→K8s 迁移时未更新 DNS CNAME 到新 tunnel。
