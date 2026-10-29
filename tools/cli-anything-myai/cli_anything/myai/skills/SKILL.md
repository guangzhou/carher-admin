---
name: cli-anything-myai
description: >-
  myai（通衢 API / ai_token_dashboard，243 K3s）只读运维 CLI 的随包说明。真身 skill 在仓库
  `.claude/skills/cli-anything-myai/SKILL.md`，以那份为准。
---

# cli-anything-myai（随包指针）

这份只跟着 `pip install` 一起分发，不维护内容。完整说明（系统现状、health 判据、198 key 同步、
发布流程、补丁 0001~0018）只写在仓库里的真身：

    .claude/skills/cli-anything-myai/SKILL.md

2026-10-14 这里曾经放了一份全文副本，到 10-29 已落后两周（还写着 myai.carher.net 是唯一入口），
所以改成指针，避免两份漂移。

最常用：

```bash
scripts/myai-cli.sh status            # 唯一入口 https://chat.auto-link.com.cn/myai 的 /api/health 摘要
scripts/myai-cli.sh diagnose coverage # 看板花费 vs 上游
```
