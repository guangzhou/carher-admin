# 杭州 gw（`vllm` 47.96.23.21，`/opt/llm-gateway`）脚本的权威副本

`/opt/llm-gateway` 不是 git 仓，机上脚本只有一份；这里是它的权威副本，判据 = `sha256sum` 逐字相同。

| 机上路径 | 本仓 | 由谁跑 |
|---|---|---|
| `/opt/llm-gateway/sync/sync_models.py` | `sync_models.py` | systemd `litellm-model-sync.timer`，每天 03:30（±120s） |

## sync_models.py

把 198 `https://cc.auto-link.com.cn/pro/v1/models` 的目录镜像进杭州 LiteLLM（`/model/new`，
`model_info.managed_by=model-sync`）。

2026-10-14 加：非聊天模型（upstream `mode` 是 image_generation/embedding/…，或名字含
image/imagine/embed/bge/…）的 `model_info` 带 `disable_background_health_check: true`
（upstream 给了 mode 就一并写上）。已有行缺这个标记时**先 add 再 delete**（名字永不为 0 个
deployment），不走 `/model/update`（会冲字段）。收尾校验标记在不在。

**为什么**：杭州开着 `background_health_checks: true`，没 mode 的行一律按聊天探。
`grok-imagine-image-2.0` 因此每 5~6 分钟收到一条带 `response_format:"b64_json"` 的聊天请求，
x.ai 回 422，198 sub2api 面板每天约 530 条。判据：探针 prompt 是 litellm
`proxy/health_check.py` 里的 `Hey how's it going?` / `What's 1 + 1?`。

部署（在本机）：

```bash
R=$(scripts/jms ssh vllm 'mktemp -d /tmp/hzsync.XXXXXX' | tail -1)
scripts/jms scp scripts/hangzhou-gw/sync_models.py vllm:$R/sync_models.py
scripts/jms ssh vllm "D=/opt/llm-gateway/sync; sudo -n cp -p \$D/sync_models.py \$D/sync_models.py.bak-\$(date +%Y%m%dT%H%M%S); \
  sudo -n install -m 644 $R/sync_models.py \$D/sync_models.py; sha256sum \$D/sync_models.py; sudo -n python3 \$D/sync_models.py; rm -rf $R"
```

回滚：`sync_models.py.bak-healthexempt-20261014T110*` 复制回原名；3 行模型改前的 model_info 在
`/opt/llm-gateway/sync/rows-before-healthexempt-20261014T110*.txt`（0600）。
