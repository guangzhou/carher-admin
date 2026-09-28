---
name: lark-key-models-sync
description: >-
  把飞书 key 表（wiki UvIxwsWLQivn1RkrdPTcVGGnnze / tblJT2s6Y6xjYj5A）的「可用模型」列
  刷成 198 LiteLLM 的当前真相。用户说「把 XX 的模型列表同步到多维表格」「刷一下飞书
  key 表的可用模型」「表里模型过时了」时用。支持 --only 点名几把 key，也支持全表。
---

# 飞书 key 表「可用模型」列同步

**一条命令**：`python3 scripts/sync-lark-key-models.py --only <alias...> [--apply]`

只读 198 的库（一条 SELECT），只写飞书那一列。不碰 litellm、不碰 pod、不碰 config。

## 坐标

| 项 | 值 |
|---|---|
| wiki | `UvIxwsWLQivn1RkrdPTcVGGnnze` |
| base_token | `DlT9bsrwMad12VsogEpcK9Ptncc`（wiki 的 `obj_token`，**不是** node_token） |
| table_id | `tblJT2s6Y6xjYj5A` |
| 要写的列 | `可用模型`（**唯一在用的那列**） |
| 权威源 | 198 `litellm-product` / `litellm-db-0` / `LiteLLM_VerificationToken.models` |

⚠️ 另两列 `Claude Code 可用模型` / `Cursor 可用模型` 各只有 1 行非空，是**废列**，别写。

## 标准流程

```bash
# 1. 预览（必做）。逐行摊开「删了什么 / 加了什么」，≤20 行时自动展开
python3 scripts/sync-lark-key-models.py --only carher-13 carher-14 carher-75

# 2. 真写。自动备份目标行原值到 .tmp-lark/，写完回读校验
python3 scripts/sync-lark-key-models.py --only carher-13 carher-14 carher-75 --apply
```

全表刷（1372 行）就是不带 `--only`。

验收看三个**分开报**的读数，任何一个非零都是没过：

```
目标行回读不一致：0 []
非目标行被改动：0 []          # --only 下这条才是真门禁
⚠️ 行数变了                   # 没出现这行才对
```

## 对账：sha256(key) == token

表的 `API Key` 列存明文 `sk-`，DB 的 `token` 列是它的 sha256。**用 hash 认人，不用
`key_alias`** —— alias 会重名、会被改，hash 一一对应。

`--only` 匹配的是**飞书表的 `key_alias` 列**（人看得懂的那个名），认人仍靠 hash。
打错名字脚本 `exit 1` 并把没匹配上的名字报出来，**不会静默 scope 到 0 行**。

## 三条刻意不做的事

1. **不把 `blocked` 折进这一列**。"被封禁"和"白名单里有哪些名字"是两件事，混进同一
   列就没法行动。脚本单独报有多少把是 blocked。
2. **不把 DB 里查不到的 key 写空**。写空会让"这把 key 没了"长得像"它没有模型"。单独
   列出来由人决定。
3. **不排除 blocked 的行**。模型列照常刷 —— 它记录的是白名单内容，与能不能用无关。

## 🔴 白名单里有这个名 ≠ 这个名真能用

这一列表达的只是「允许请求哪些名字」。缺 alias 目标会 400、同名组挂死会假红。要判
**真能不能用**，走 keyprobe `http://10.68.13.198:30920/` 真打，或
skill `litellm-198-key-allowlist` 的行为层回归。别拿这一列当可用性结论。

## 尺子自检（每轮都做）

「需要更新 0 行」有两种成因：真的已最新，或者尺子根本没数到。**必须做阳性对照**：

```bash
sed 's|return ", ".join(sorted(models or \[\]))|return ", ".join(sorted(models or [])) + ", POISON-CONTROL"|' \
  scripts/sync-lark-key-models.py > .tmp-poison/poisoned.py
python3 .tmp-poison/poisoned.py --only <同一批 alias>
# 必须每行都报 「加: POISON-CONTROL」。报不出来 = 尺子坏了，原来那个 0 不可信
rm -rf .tmp-poison scripts/__pycache__
```

⚠️ 连续下毒同一个文件要**每轮先删 `__pycache__`**，否则复用旧 `.pyc` 会假绿。

## 写飞书的坑（都吃过）

- `+record-batch-update` 是**逐字段覆盖**，patch 里多带一个字段就把原值冲掉，而且
  **不产生任何信号**（没报错、没 diff、`ok:true`）。⇒ patch 里只放这一轮真要改的字段；
  写前必须存原值（脚本已自动备份到 `.tmp-lark/`）。
- body 形状是 `{"update_records": {"<rid>": {"字段名": "值"}}}`。写成
  `{"records":[...]}` 报 `800010701`。
- **`"ok": true` 不证明写进去了**，必须回读比一遍。
- flag 是 `--base-token`，不是 `--app-token`。
- `--output` / `--json @file` **只认相对路径或 `/tmp` 下的路径**；给系统临时目录的绝对
  路径会静默不生成文件、stdout 空。
- `+record-search --format ndjson` 的记录**只落到 `--output` 文件**，stdout 只有
  manifest ⇒ 去 parse stdout 会解出 0 行，看起来像"表里没这些记录"。

## 排序不是洁癖

DB 里 `models` 是插入顺序，会无意义地抖。脚本写进去的是 **sorted** 之后的值 ——
不排序的话每次跑都"有 1300 行要改"，diff 就没有意义了。

## 相关

- `scripts/sync-lark-key-models.py` — 本 skill 的全部实现
- skill `litellm-198-key-allowlist` — 改 198 上 per-key `models`/`aliases`（**写** DB 那侧）
- skill `lark-ops` — 发飞书消息
- 生产库永远 `-n litellm-product`，不是 dev。多行 SQL 经 ssh 双引号会被吃，走 stdin。
