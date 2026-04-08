---
name: clone-instance-memory
description: >-
  Clone a CarHer bot instance with its memory (SQLite) to a new instance ID.
  Use when creating a new her that reuses an existing her's memory/knowledge,
  duplicating instances, or migrating memory between instances.
---

# 克隆 Her 实例 + 复用记忆

将已有 her 实例的配置和记忆完整克隆到一个新 ID。

## 前置条件

```bash
API_KEY=$(kubectl get secret carher-admin-secrets -n carher \
  -o jsonpath='{.data.admin-api-key}' | base64 -d)
```

若本地 kubectl 不通（`127.0.0.1:16443` 拒连），先建 SSH 隧道：

```bash
SSHPASS='<password>' sshpass -e ssh -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 -p 1023 \
  -L 16443:172.16.1.163:6443 -N root@47.84.112.136 &
```

## 步骤

### 1. 获取源实例配置

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{SOURCE_ID} | jq
```

记录：`name`、`app_id`、`model`、`provider`、`prefix`、`deploy_group`、`owner`、`image`。

从 K8s Secret 取 `app_secret`：

```bash
kubectl get secret carher-{SOURCE_ID}-secret -n carher \
  -o jsonpath='{.data.app_secret}' | base64 -d
```

### 2. 创建新实例

```bash
curl -s -X POST "https://admin.carher.net/api/instances/batch-import" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
  "instances": [{
    "id": {NEW_ID},
    "name": "<同源>",
    "app_id": "<同源>",
    "app_secret": "<同源>",
    "owner": "<同源>",
    "model": "<同源>",
    "provider": "<同源>",
    "prefix": "<同源>",
    "deploy_group": "<同源>"
  }]
}'
```

### 3. 同步镜像版本

新实例可能使用默认旧镜像，必须与源实例对齐：

```bash
curl -s -X PUT "https://admin.carher.net/api/instances/{NEW_ID}" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"image":"<源实例 image tag>"}'
```

### 4. 等待 Pod Running

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID} | jq .status
# 期望: "Running"
```

### 5. 拷贝记忆（集群内网直传）

**关键：不要用 `kubectl cp`（经过本地 SSH 隧道，慢且易断）。用 Pod 间 HTTP 直传。**

先检查源实例记忆大小和 WAL 状态：

```bash
# 通过 exec API 检查（不经过隧道）
curl -s -X POST -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  https://admin.carher.net/api/instances/{SOURCE_ID}/exec \
  -d '{"command":"du -sh /data/.openclaw/memory/ && ls -la /data/.openclaw/memory/"}'
```

如果存在 `-wal` 或 `-shm` 文件，先 checkpoint（需 kubectl exec，exec API 不允许 sqlite3）：

```bash
kubectl exec -n carher {SOURCE_POD} -c carher -- \
  sqlite3 /data/.openclaw/memory/main.sqlite "PRAGMA wal_checkpoint(TRUNCATE);"
```

在源 Pod 启动临时 HTTP 文件服务：

```bash
SOURCE_POD=$(kubectl get pods -n carher -l user-id={SOURCE_ID} \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n carher $SOURCE_POD -c carher -- sh -c '
nohup node -e "
const http = require(\"http\");
const fs = require(\"fs\");
const s = http.createServer((req, res) => {
  const file = \"/data/.openclaw/memory/main.sqlite\";
  const stat = fs.statSync(file);
  res.writeHead(200, {\"Content-Length\": stat.size});
  fs.createReadStream(file).pipe(res);
});
s.listen(19876);
setTimeout(() => { s.close(); process.exit(0); }, 300000);
" > /dev/null 2>&1 &
echo "SERVER_STARTED"
'
```

在新 Pod 通过集群内网下载（数据不经过本地）：

```bash
SOURCE_IP=$(kubectl get pod $SOURCE_POD -n carher -o jsonpath='{.status.podIP}')
NEW_POD=$(kubectl get pods -n carher -l user-id={NEW_ID} \
  -o jsonpath='{.items[0].metadata.name}')

kubectl exec -n carher $NEW_POD -c carher -- node -e '
const http = require("http");
const fs = require("fs");
const dir = "/data/.openclaw/memory";
fs.mkdirSync(dir, {recursive: true});
const out = fs.createWriteStream(dir + "/main.sqlite");
const start = Date.now();
http.get("http://'$SOURCE_IP':19876/", (res) => {
  const total = parseInt(res.headers["content-length"] || 0);
  let received = 0;
  res.on("data", (chunk) => { received += chunk.length; });
  res.pipe(out);
  out.on("finish", () => {
    const secs = ((Date.now() - start) / 1000).toFixed(1);
    console.log("DONE: " + (received/1048576).toFixed(1) + "MB in " + secs + "s");
    process.exit(0);
  });
}).on("error", (e) => { console.error("ERR:", e.message); process.exit(1); });
'
```

### 6. 验证

```bash
# 文件大小应与源一致
kubectl exec -n carher $NEW_POD -c carher -- \
  ls -la /data/.openclaw/memory/main.sqlite

# SQLite 文件头校验
kubectl exec -n carher $NEW_POD -c carher -- node -e '
const fs = require("fs");
const buf = Buffer.alloc(16);
const fd = fs.openSync("/data/.openclaw/memory/main.sqlite", "r");
fs.readSync(fd, buf, 0, 16, 0);
fs.closeSync(fd);
console.log("Valid SQLite:", buf.toString("ascii",0,15) === "SQLite format 3" ? "YES" : "NO");
'
```

### 7. 重启新实例

```bash
curl -s -X POST -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID}/restart
```

等 ~30s 后确认：

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://admin.carher.net/api/instances/{NEW_ID} | jq '{status, feishu_ws, oauth_url}'
```

## OAuth 回调地址

自动生成规则：`https://{prefix}-u{id}-auth.carher.net/feishu/oauth/callback`

## 注意事项

| 事项 | 说明 |
|------|------|
| **镜像版本** | 新实例默认镜像可能与源不同，必须在 Step 3 显式对齐 |
| **不要用 kubectl cp** | 177M 文件经 SSH 隧道极易 EOF 中断；用 Pod 间 HTTP 直传 |
| **WAL 文件** | 若有 `-wal`/`-shm`，拷贝前必须 `PRAGMA wal_checkpoint(TRUNCATE)` |
| **exec API 白名单** | 不支持 tar/sqlite3/cp，只允许 ls/cat/du/node 等，大文件操作用 kubectl exec |
| **不影响其他实例** | 每实例独立 PVC，sessions 按 uid 子路径隔离 |
| **同飞书应用** | 若新旧实例用同一 app_id，open_id 一致，记忆匹配无问题 |
