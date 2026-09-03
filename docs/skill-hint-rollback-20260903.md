# skill-hint / skill-kick 回滚表（2026-09-03）

**是什么**：让模型在「我没有飞书工具」之前先去搜本机 skill 库（`~/.claude/skills` 等），并把
首轮拒绝直接换成一条真 `grep` Shell 调用喂回去。只改 lane 的 `responses.js`，五刀依次
`scripts/zk-cursor-web/skill-hint/patch_skill{hint,hint_v2,kick,kick_v2,kick_v3}.py`，
全部门在 env `ZK_SKILL_HINT=1`（默认关 = 零行为差）。

**现网（09-03 01:30）**：
| lane | CM | responses.js sha | ZK_SKILL_HINT |
|---|---|---|---|
| 135 136 137 138 139 140 | `zk-cursor-bpi-patch-135` | `fc77f9e90e134981…` | 1 |
| 84 | `zk-cursor-bpi-patch-pool` | `6a1c249e33fc7467…`（含 v1 常量、默认关） | unset |
| 82 (canary) | `zk-cursor-bpi-patch-82` | `ba2f5e77955b8893…`（未动） | unset |

同日把 136~140 补进主池名 `cr-g-5.6`（原只有 135 一腿）：`zerokey-cr-g-{136..140}-5.6`。

## 回滚
1. **秒关（不重启、不改 CM）**：`kubectl -n litellm-product set env deploy/zero-cursor-bpi-<N> ZK_SKILL_HINT-`
   （六条各来一次）。所有 skill-hint/kick 代码路径全灭。
2. **代码整回**：`kubectl -n litellm-product patch deploy zero-cursor-bpi-<N> --type json -p '[{"op":"replace","path":"/spec/template/spec/volumes/3/configMap/name","value":"zk-cursor-bpi-patch-pool"}]'`
   然后 `rollout restart` 该 lane；六条都回了再删 CM `zk-cursor-bpi-patch-135`，
   并把 `scripts/zk-cursor-web/pool_consistency.py` 里 `-135` 那组 FORKED_CM 与三条 ZK_SKILL_HINT 允许项删掉。
   deployment 改前整份备份：`/Data/backups/zk-bpi-deploy-{136..140}-20260903-011633-pre-skillhint.json`。
3. **`-135` CM 各版本备份**（改前整份）：`/Data/backups/zk-cursor-bpi-patch-135-20260903-*-pre-skillhint-v2 / -pre-skillkick / -pre-skillkick-v2 / -pre-skillkick-v3.json`。
   `-pool` 改前：`/Data/backups/zk-cursor-bpi-patch-pool-20260903-002626-pre-skillhint.json`。
4. **池腿回滚**：`/model/delete` 五个 id `zerokey-cr-g-{136,137,138,139,140}-5.6`，再 `rollout restart deploy/litellm-proxy`。
   建前表快照：`/Data/backups/litellm-modeltable-names-20260903-012157-pre-crg56-pool.txt`。

## 判据（真 Cursor，不是探针）
lane 日志：首轮 `[handshake] … skill-hint 1578c`；拒绝时 `[skill-kick] refusal … -> real search call kw=…`；
之后连续 `complete-run … prose 0 chars`；最终 `[url-prior]` 出现 docx 链接（**日志会截掉 URL 最后一个字符**，
拿全 token 用 `lark-cli docs +fetch --as user --doc <url>` 独立打开）。

## 已知残余
- 探针形状：打这些名字必须 chat + stream + **tools 非空**，否则不走 chat→responses 桥，掉进 lane
  `chatgpt.js` 报 `user is not a function`（500）—— 那是探针错，不是腿坏。
- `cr-g-5.6` 现 6 腿（无 84），其余 13 个池名 7 腿。84 是否跟进由用户定。
- zk-delta（客户端→LiteLLM 增量）本机未开，原则⑤未验。
