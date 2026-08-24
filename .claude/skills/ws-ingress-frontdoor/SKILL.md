---
name: ws-ingress-frontdoor
description: |
  ws-ingress 网关（#24）「客户端→网关」WS 增量腿的正门启用、验收与全员推广 SOP。
  当需要：为 cc.auto-link.com.cn 打通/排障 codex responses-over-WebSocket 正门、判断某一跳有没有
  剥掉 Upgrade 头（101 vs 405）、给 IT 写透传要求、验收正门是否端到端通、或规划「用户零改动」全员
  推广（supports_websockets 一行的分发边界与版本地板）时使用。
  判据来自 2026-08-24 生产实测（正门握手 101 + IT 透传落地）。与 acct pod→上游那条腿区分：
  那条见 [[litellm-acct-ws-incremental]]；协议机制见 [[codex-multiturn-transport-mechanism]]。
---

# ws-ingress 正门：启用 · 验收 · 全员推广

> 一句话：codex 客户端对自定义 provider 走 responses-over-WebSocket，把每轮全量历史上行
> 砍成「只发新增那句」。网关（ws-ingress）终结客户端 WS、按连接账本重建全量、内部转 HTTP
> 打 litellm-proxy。本 skill 只管**客户端→网关这条腿的正门链路**，不碰 ws-ingress 内部算法。

## 链路（每一跳都可能剥 Upgrade 头）

```
codex ──wss://cc.auto-link.com.cn/pro/v1/responses (443)
      → 公网入口 58.241.5.230 (IT 管, SSL 终结)          ← 历史卡点：曾剥 Upgrade
      → 明文 HTTP 回源 10.68.13.198:80 (198 本机 nginx)   ← map 按 Upgrade 分流
      → NodePort 30403 → ws-ingress (svc :8799)
      → 内部 HTTP → litellm-proxy:4000（计费/路由/换号全在这层，不变）
```

**同一个 URL `/pro/v1/responses` 同时承载 HTTP POST 和 WS**：普通 POST 走 litellm，
带 `Upgrade: websocket` 的握手走 ws-ingress。分流靠 198 nginx 的 `map $http_upgrade`，
所以老用户（不开 WS）零感知继续走今天的 HTTP 路。

## 198 本机 nginx 分流（cc.auto-link.com.cn.conf，实体文件，改前备份）

```nginx
upstream ws_ingress { server 127.0.0.1:30403; keepalive 32; }
map $http_upgrade $pro_responses_backend {
    default      litellm_product;   # 普通 POST → litellm
    ~*websocket  ws_ingress;        # WS 握手 → ws-ingress
}
location = /pro/v1/responses {
    rewrite ^/pro/(.*)$ /$1 break;
    proxy_pass http://$pro_responses_backend;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}
```
198 nginx 是实体文件、glob 会吃 .bak，纪律见 [[feedback_198_nginx_sites_enabled_is_real_file_and_glob_eats_baks]]。

## IT 入口机（58.241.5.230）要做的一件事

在 cc.auto-link.com.cn 回源 198:80 的 location/server 里加：
```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_read_timeout 3600s;     # WS 长连接，避免默认 60s 空闲断
# 依赖 http{} 里的 map $http_upgrade $connection_upgrade { default upgrade; '' close; }
```
**对现有 POST 零影响**：普通请求 `$http_upgrade` 为空，nginx 忽略这两行。

### 「map 那块不敢加」的判据（省一轮扯皮）
若 IT 只加了 location 三行、没加 `map $connection_upgrade`：
- 那个变量**未定义** → `proxy_set_header Connection $connection_upgrade` 引用未定义变量
  → `nginx -t` 直接失败、reload 不了、**更回不了 101**。
- 所以：**能探到 101 就反证「变量其实已定义」**（很多 nginx 通用配置自带），map 不用再加。
- 真要绕开 map 且该 location 专用于 WS：把 `Connection $connection_upgrade` 写死成
  `Connection "upgrade"`（仅当这个 location 只走 WS，否则会污染普通请求的 keepalive）。

## 验收（数据，不靠机制猜）

```bash
# 握手验收（无需有效 key；网关先升级、转发时才校验 key）
python3 scripts/ws-ingress-gateway/frontdoor_probe.py
#   → 101 Switching Protocols = 整条透传链路通
#   → 405 (allow: POST)       = 某一跳把 Upgrade 头剥了（先怀疑 IT 入口机）

# 隔离 198 侧（绕过正门，明文直打 NodePort）
python3 scripts/ws-ingress-gateway/frontdoor_probe.py --scheme ws --host 127.0.0.1 --port 30403

# 两轮增量（临时 key，用完 /key/delete）；增量命中要到 198 grep 日志确认
python3 scripts/ws-ingress-gateway/frontdoor_probe.py --turns 2 --key sk-xxx --model gpt-5.6-sol
ssh cltx@10.68.13.198 'kubectl -n litellm-product logs deploy/ws-ingress | grep ws_ingress | tail'
#   T2 要出现 mode=incremental 才算增量真命中（隧道腿实测 96%）
```
判据纪律：101/405 是硬信号；`mode=incremental` 才是增量命中证据，200 不算数。

## 全员推广：「用户零改动」的真实边界

内容上**只需在 provider 加一行 `supports_websockets = true`**——base_url 不动
（仍 `https://cc.auto-link.com.cn/pro/v1`）、计费/路由/换号全不动。

但这行在**每个用户自己的 `~/.codex/config.toml`**，是客户端配置：
**服务端没有任何开关能替客户端决定开不开 WS，传输方式是客户端说了算。**
所以「用户什么都不改」能否实现，取决于**有无集中下发 config 的通道**：
- 有（装机脚本 / 内网模板 / 共享 CODEX_HOME）→ 源头改一行，一次推给所有人，用户端零动作。
- 无 → 等价于每人加一行（给一条一键命令让其粘贴）。

**安全性（全员开无「炸一片」风险）**：
- 版本地板 **codex ≥0.118**（0.147 实测单行即触发，无需 `--enable` feature flag）；
  低版本加了不生效，但**继续走 HTTP、协议自带回落**。
- WS 任何异常（网关挂/握手失败/中流断）→ codex **协议内建自动回落 HTTP 全量**，用户无感。
- App 与 CLI 同源同内核同配置，一行对两者同时生效，推广不必区分——见
  [[reference_codex_app_and_cli_share_ws_switch_same_config]]。
- 灰度天然按人：谁改配置谁用，随时个体回退。

## 回滚

- 客户端侧：删掉那行 `supports_websockets=true`（或版本回退）→ 立刻回 HTTP。
- 网关侧：`kubectl -n litellm-product scale deploy/ws-ingress --replicas=0`
  → 客户端 WS 连不上、协议内建回落 HTTP，全量流量无感落回今天的路。
- 198 nginx 侧：把 `location = /pro/v1/responses` 的分流去掉恢复直发 litellm（实体文件，先备份）。

## 关键文件 / 指针

- 网关代码/测试：`scripts/ws-ingress-gateway/{app.py,test_app.py}`；探针 `frontdoor_probe.py`
- 部署：`k8s/ws-ingress.yaml`（svc + Deployment + NodePort 30403）
- 设计/进展：`docs/ws-ingress-gateway-plan-20260824.md`（S4 ✅）
- 判据记忆：[[reference_ws_ingress_front_door_blocked_by_it_entry_upgrade_strip]]（含 IT 透传落地与 101 反证）
- 访问：10.68.13.198 `cltx` / `export KUBECONFIG=/home/cltx/.kube/config`（不 sudo）
