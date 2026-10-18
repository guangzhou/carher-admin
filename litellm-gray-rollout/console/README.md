# 升级驾驶台（gray console）

**作用**：把一次版本灰度（试点 → 1/5/10/50/100% → 换生产 → 完成）变成「机器开顺路，碰到判断不了的事停下来，摆出选项等人拍板」。
设计与调研见 `DESIGN.md`。

## 打开

浏览器直接打开 **http://10.68.13.198:8795**（公司内网 / VPN 下）。

```bash
# 198 上（root）启动：监听内网 IP，来源白名单 = 10.68.30.0/24（办公网）+ 10.68.13.0/24（机房网段）
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml GRAY_REPO_SCRIPTS=/opt/gray-console/scripts nohup setsid python3 /opt/gray-console/console/server.py \
  --bind 10.68.13.198 --port 8795 --allow 10.68.30.0/24 --allow 10.68.13.0/24 --no-auto > /opt/gray-console/server.log 2>&1 &
# 没有灰度在跑时用 --no-auto；开新一轮灰度、想让它自己升档时去掉 --no-auto 重启（或页面上打开「自动推进」）

# 写操作口令（页面右下「操作口令」里粘贴；每次启动重新生成）
ssh cltx@10.68.13.198 'sudo cat /root/l3/.console-token'
```

- 不在白名单的来源直接 403；只看页面不需要口令，任何会动线上的操作都要口令 + 页面上二次确认。
- 不给 `--bind` 时只听 127.0.0.1（老方式，需要 ssh -L）。
- 🔴 **不是 systemd 服务**：198 重启后要按上面命令手动拉起；页面打不开先 `ss -ltnp | grep 8795`。

部署 / 更新：`tar -C litellm-gray-rollout -czf - console | ssh cltx@10.68.13.198 'sudo bash -c "cd /opt/gray-console && rm -rf console && tar xzf -"'`，再重启进程。
回归用到的仓库脚本要同步到 `/opt/gray-console/scripts/`（`GRAY_REPO_SCRIPTS` 指向它）：
`tar -czf - scripts/litellm-198-lane-sweep.py scripts/litellm-198-key-behavior-regress.py scripts/litellm-198-auth-parity.py scripts/litellm-lane-bench.py scripts/litellm-198-her-name-probe.py scripts/litellm-198-budget-notice-smoke.sh scripts/codex-compaction-regression.py | ssh cltx@10.68.13.198 'sudo tar -C /opt/gray-console -xzf -'`

## 它会自己做什么、不会做什么

| 机器自己做 | 必须人点 |
|---|---|
| 放量 1→5→10→50→100（每档驻留够、≥5 周期全过、新版 5xx 不高于旧版 1pp、worker 0 被杀、巡检号已钉旧版；≥50 另要容量 + 保险丝健康） | 开始灰度（试点、钉巡检号、起 monitor——终端做） |
| 每一步写事件流 `$D/console-events.jsonl` | 换生产（§7 converge）、切回生产（commit） |
| 升档失败 ⇒ 本档不再重试，出决策卡 | 全部退回旧版、关 run |

- 没人回答 = **保持现状**：当前档继续跑，monitor loop 的自动止损照常（止损不经过驾驶台）。
- 驾驶台挂了 / 被停了 = 什么都不发生。真相仍在 `/etc/nginx/gray-route/active`。
- 页面上可以关「自动推进」，变成每档都问。

## 用例回归

页面「用例回归」区列出 7 条用例（回归什么、怎么算过、为什么要这条），每条新旧车道并排跑。
点「跑一轮回归」（口令 + 二次确认，约 35 分钟），结果三态：**通过** / **同形**（两边一样不过 ⇒ 上游或用例问题）/ **不过**（停）。
每轮落 `$D/console/regress/<ts>/`（meta.json + 每条日志），历史在页面上可展开。10-18 首轮 gray vs prod 7/7 通过。
用例定义在 `regress.py` 的 `CASES`，加用例要同时补 `tests/fixtures/console-regress/` 的输出样本和 `test_console_regress.py`。

## 和命令行并存

- 所有动作都是调现有脚本（`$D/step.sh`、`converge.sh`、`commit.sh`、`gray-global-rollback.sh`、`gray-run-close.sh`），同一把 `_lib.sh` 锁、同一套 gate evidence。
- **人在终端手动跑 run/ 脚本时**先 `flock $D/.console.lock -c '<命令>'`：驾驶台看到锁被占就进「人工接管」，只看不动。
- 不想用驾驶台：不启动它就行，现有流程完全不受影响。

## 回放测试

```bash
python -m pytest litellm-gray-rollout/tests/test_console_engine.py litellm-gray-rollout/tests/test_console_actions.py litellm-gray-rollout/tests/test_console_regress.py
```
用例取自 2026-10-17 那晚的真实读数（探针 402、杭州凭据、上一轮残留的容量证据、升档失败后重复重试）。
198 上可用 `/root/l3/mk-sandbox.sh <split>` 造只读沙盒，第二个实例指向它（`GRAY_ROOT=… GRAY_RUN_POINTER=… --port 8796`）做端到端演练，
沙盒里的 step.sh 是空操作。
