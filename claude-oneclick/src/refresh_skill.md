---
name: carher-refresh-models-claude
description: 仅限 Claude Code(命令行 claude 或 Claude 桌面 App)里用:用户说「刷模型」「更新模型」「刷新模型列表」,或说 /model 里的模型不对时。按用户的 key 从公司网关重新拉模型列表,同时更新 Claude Code 命令行和 Claude 桌面 App 的模型菜单。⛔ 如果你是 Cursor 或 Codex,不要用这个,用你自己那个工具的刷模型。
---

# 刷模型

直接运行下面这条命令,不要先问用户:

```
@@NODE@@ "@@TOOL@@"
```

- 把输出里「新增 / 删除 / 已改回默认模型」那几行原样告诉用户。
- 提醒用户:命令行新开一个 claude 会话就是新列表;Claude 桌面 App 要**完全退出再打开**才显示新菜单。
- 命令报错就把原文告诉用户,不要自己改 settings.json。
- 平时不用手动刷:每次打开 Claude Code 都会在后台自动刷一次。说「刷模型」= 现在立刻刷。
