---
name: lark-ops
description: >-
  CarHer 项目飞书运营通知：发送部署通知、告警、状态报告到飞书群/个人。
  当需要发飞书消息、通知团队、查看历史消息时使用。
---

# CarHer 飞书运营通知

通过已配置的飞书 App（cli_a91569fab9b81bc6）发送消息。

## 基础用法

### 发送文字消息给个人

```bash
lark-cli im +messages-send --user-id <open_id_or_email> --text "消息内容"
```

### 发送消息到群聊

```bash
lark-cli im +messages-send --chat-id <oc_xxx> --text "消息内容"
```

### 发送 Markdown 格式消息

```bash
lark-cli im +messages-send --chat-id <oc_xxx> --markdown "**部署完成** 版本: v20240101-abc1234"
```

### 搜索群聊

```bash
lark-cli im +chat-search --keyword "CarHer"
```

### 查看消息历史

```bash
lark-cli im +chat-messages-list --chat-id <oc_xxx> --limit 20
```

## 常用场景

### 部署通知

```bash
TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "unknown")
lark-cli im +messages-send --chat-id <CHAT_ID> \
  --markdown "**[CarHer] 部署通知**\n版本: \`$TAG\`\n时间: $(date '+%Y-%m-%d %H:%M')\n状态: ✅ 成功"
```

### 告警通知

```bash
lark-cli im +messages-send --chat-id <CHAT_ID> \
  --markdown "**[CarHer] ⚠️ 告警**\n$ALERT_MSG"
```

## 身份说明

- `--as user`（默认）：以授权用户（刘国现）身份发送
- `--as bot`：以 App Bot 身份发送（Bot 需要在群内）

## 注意事项

- Bot 发群消息前需先将 Bot 加入群聊
- 查看已授权的 scopes: `lark-cli auth status`
- 全局 skills 位于 `~/.claude/skills/lark-*/`，涵盖日历、文档、任务等更多能力
