# cli-anything-myai

只读运维 CLI,对象是 myai(通衢 API / `davidzhu2021/ai_token_dashboard`)。公网两个入口同一个库:
默认打国内入口 `https://chat.auto-link.com.cn/myai`;`https://myai.carher.net` 走 CF,国内常慢,用 `--public-url` 指定。

跑在 243(`AIYJY-litellm-ps`)单节点 K3s 的 `ai-token-dashboard` 命名空间。

## 依赖

- 本机能 ssh `cltx@10.68.13.243`;密码登录需 `sshpass`(`brew install sshpass`)
- 243 上 cltx 可直接 `kubectl`

## 安装

```bash
scripts/myai-cli.sh install        # uv tool editable,改代码即生效
```

## 用法

```bash
export MYAI_SSH_PASSWORD='...'     # 或用 ssh key;只走 env,不进 argv
M=scripts/myai-cli.sh

$M status                          # 公网 /api/health 摘要 + findings
$M diagnose sync-stuck             # lastSuccessAt 不动,是不是 her 稀疏门禁
$M sync runs --days 7              # 每天 ok/partial/failed 计数
$M sync errors                     # 高频错误
$M queue                           # 刷新队列
$M realtime                        # 实时结算滞后
$M usage daily --days 14           # 已发布快照每天用量
$M identity find zhuyida           # 账号↔人
$M billing orders --status pending
$M billing outbox
$M upstream daily --backend her    # 上游 LiteLLM 每天请求/花费(在 worker pod 里用它自己的 key)
$M upstream keys --days 7          # 有花费的 key,按 owner 在不在用户表分类
$M diagnose coverage --days 14     # 每天 看板花费 vs 上游花费,偏差>1% 给原因
$M probe-pod up && $M --probe-pod upstream daily ... && $M probe-pod down   # 重探针放隔离 pod
$M db tables
$M db sql "SELECT count(*) FROM usage_daily"
$M logs sync --since 1h --grep quality
```

不带子命令进 REPL。`--json` 给机器读。

## 只读保证

- SQL 全部在 `default_transaction_read_only=on` 事务里跑,服务端拒写;`db sql` 本地再拦一道
- 上游探针只发 GET,脚本是包内固定模板
- 唯一会建/删资源的是 `probe-pod up/down`,只动 `pod/myai-probe`;没有 rollout / apply 业务资源

## 测试

```bash
cd tools/cli-anything-myai
python -m pytest cli_anything/myai/tests/test_core.py -v                    # 纯本地
CLI_ANYTHING_FORCE_INSTALLED=1 python -m pytest cli_anything/myai/tests/ -v -s   # 含 243 真后端
```
