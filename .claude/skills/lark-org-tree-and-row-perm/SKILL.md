---
name: lark-org-tree-and-row-perm
description: >-
  从飞书通讯录建「谁向谁汇报」的组织树、把在职状态刷进账户表、建逐人确认表，
  并用多维表的记录级权限让每个 leader 只能改自己团队那几行。
  用户说「梳理下这个表格的组织关系」「哪些是 leader 哪些是下面的人」
  「把离职的状态更新下 / 去掉」「怎么保证 leader 只能改自己团队的行」
  「把某个组织的 leader 都设成 X」时用。
  也含：离职 key 去 198 LiteLLM 置 blocked 的闭环。
---

# 飞书组织树 + 逐行确认 + 记录级权限

**一条命令**：`python3 scripts/lark-org-tree-sync.py <tree|status|sync-status|org|leader> [--apply]`

对象：账户表 `wiki UvIxwsWLQivn1RkrdPTcVGGnnze` → base `DlT9bsrwMad12VsogEpcK9Ptncc`，
账户表 `tblJT2s6Y6xjYj5A`、确认表 `tbln0k5HFwdmkkxr`。

## 0. 上下级关系不在表里，只在通讯录里

🔴 **账户表的「姓名.直属上级」「姓名.人员类型」「姓名.工号」三列全行为空**
（2026-09-30 实测 0/1389）。想从表里读汇报关系 ⇒ 读出来是空，看着像"没人填"，
实际是**这张表从来没有这个数据**。

权威源 = `contact/v3/users/batch` 的 `leader_user_id`，**必须逐层向上闭包**：
表里的人未必包含他们的上级（38 位管理者本人不持 key、不在表里），不闭包树是断的。

```bash
python3 scripts/lark-org-tree-sync.py tree     # 只读，打印全树 + leader 名册
```

## 1. 跨租户是硬缺口，不是漏拉

660 人里 **181 个是跨租户**（`is_cross_tenant=true`，上海车联天下 145 / 北汽研究院 14 /
组织也空 21 / 奇瑞·美团·巧虎 各 1）。对这批人通讯录**同时给不出 leader 和 status**，
`+search-user` 和 `users/batch` 两条路都返回空。

⇒ **这批必须单列成桶**，不能跟「已确认没问题」混在一起
（[[feedback_survey_gate_denominator_must_be_measurable_accts]]）。

**怎么证明是飞书给不出、而不是我查法错**：同一个 batch 请求里**混一个已知活人**
（如卜弋天 `ou_12e9d3bdd0d71657977aa1f1d2fbbdd2`）。他回来了、那 20 个没回来
⇒ 接口是通的，确实查不到。不打这个阳性对照就说"查不到"是无证据归因。

顺带一个形状：这批人的**邮箱前缀全是随机 hex**（`2f5fb986`、`cursor-f2751430`），
而正常同事是 `buyitian`、`yangcheng` 这种拼音。这是**观察到的相关性，不是结论** ——
它可能意味着外部协作号/批量临时号/登记没填真邮箱，判清只能问登记人。

## 2. 认人只认 open_id

- 🔴 **账户表的 `key_alias` 和 LiteLLM 的对不上 91/92**（表里少随机后缀：
  `cursor-lijie` vs `cursor-lijie-3ef2`）。
- 🔴 **「李杰」「杜小畔」「刘咏强」各是两个不同的人**。两个李杰里只有
  「算法高级工程师」那个离职了，「SOC基础软件高级工程师」那个在职。
  按姓名匹配会一起误杀。
- 对 LiteLLM 的 key 一律 `sha256(明文key) == LiteLLM_VerificationToken.token`
  （同 [[project_lark_key_table_synced_from_198_2026_09_26]]）。

## 3. 在职状态对账

```bash
python3 scripts/lark-org-tree-sync.py status                 # 只读，列差异
python3 scripts/lark-org-tree-sync.py sync-status --apply    # 刷「是否在职」
```

判据是 `status.is_resigned || status.is_exited`。

⚠️ **「是否在职」和 key 的「状态」是两件事，别一起改**：前者是 HR 事实，
后者是 key 能不能用。2026-09-30 那轮用户明确只改前者 ——
封不封 key 要留给 leader 判（[[feedback_two_failure_faces_must_be_counted_separately]]）。

⚠️ **表是快照不是镜子**：09-30 刷完，10-05 再跑就又多出 1 个新离职（孙燚辉），
行数也从 1389 涨到 1401。每次要用先跑 `status`。

## 4. 删行会让存量快照变成谎话

从确认表删掉离职的人之后，**三处会静默过期**，删完必须一起修：

| 过期的东西 | 症状 |
|---|---|
| `直属下属数` / `辖下持key人数` | 28 行数字偏大（卜弋天辖下 193→93、吴超 59→23） |
| `角色` | 5 个人的下属全离职了，自己该从 leader 变成员（陈平、马晓辉、李孟鑫、何银川、易楷伦） |
| 靠「离职冲突」列筛的视图 | 变成**空视图**（冲突行都删了），得改筛选条件 |

删前还要查一件事：**离职的 leader 名下有没有在职下属**。09-30 那轮王勇萍（直属 4）、
韩冰（直属 1）都是离职 leader，但那 5 个下属**自己也全离职**，所以删完没有悬空归属。
如果有在职下属，删掉他们的 leader 会留下指向不存在的人的 `归属leader`。

## 5. 记录级权限：一个角色覆盖全部 leader

需求「leader 只能改自己团队那几行」的正解**不是建 N 个角色**，而是
**人员字段 = 当前登录者**这个动态条件。

🔴 **前提：拿来做条件的必须是 `user` 类型字段。** 原来的 `归属leader` 是 text，
做不了这个条件 —— 必须另加一个人员列 `归属leader(人)` 并回写。

```bash
# 按通讯录回填（跨租户/顶层自动留空）
python3 scripts/lark-org-tree-sync.py leader --apply
# 跨租户那批人工归口：把某组织的行统一指给一个人
python3 scripts/lark-org-tree-sync.py leader --set-org 上海车联 \
  --to ou_69ff0e6df50e17fb73bd5e018086d68e --apply
```

角色本体（`base +role-create`，高级权限须已开）：

```json
{"role_name":"leader自核","role_type":"custom_role",
 "base_rule_map":{"copy":false,"download":false},
 "table_rule_map":{
  "组织关系确认(2026-09-30)":{"perm":"edit",
   "view_rule":{"allow_edit":false,"visibility":{"all_visible":true}},
   "record_rule":{"record_operations":[],
    "edit_filter_rule_group":{"conjunction":"and","filter_rules":[{"conjunction":"and",
      "filters":[{"field_name":"归属leader(人)","operator":"contains"}]}]},
    "other_record_all_read":false},
   "field_rule":{"field_perm_mode":"specify","field_perms":{
     "确认结果":"edit","修正后的直属上级":"edit","备注":"edit",
     "归属leader(人)":"read","姓名":"read","职务":"read"}}},
  "数据表":{"perm":"no_perm"}}}
```

五个踩过的点：

1. 🔴 **`user` 字段的操作符是 `contains`，不是 `is`。** 传 `is` 报
   `field type 'User' is incompatible with operator 'is'`。
2. 🔴 **`filter_values` 不传 = 当前登录者。** 服务端补成
   `{"operator":"contains","field_type":"User","filter_values":null}` ——
   **null 就是动态语义**，所以 1 个角色覆盖 73 个 leader，不用建 73 个。
3. 🔴 **筛选用的字段不能是 `no_perm`。** `field_perm_mode:"specify"` 时未列出的
   字段默认 no_perm，而筛选字段必须至少 `read`，否则报
   `field '归属leader(人)' is used in record filters and cannot be set to no_perm`。
4. 🔴 **`other_record_all_read` 在 `role-get` 里根本不回显。** 传 true 传 false
   都查不到这个键 ⇒ **这把尺子读不出读范围**。只能找一个真 leader 本人打开表实测。
5. ⛔ **管理员不受自定义角色约束** ⇒ 拿自己的号测「只能看自己团队」永远全表可见，
   是假绿（同 [[feedback_bitable_collaborator_is_not_row_access]]：只有「可管理」不受
   高级权限限制）。

加成员：lark-cli **没有**这个命令，走裸 API
`POST /open-apis/bitable/v1/apps/<base>/roles/<role_id>/members`
+ `--params '{"member_id_type":"open_id"}'` + `--data '{"member_id":"ou_..."}'`。
⚠️ `base/v2/...` 那个路径是 404。

## 6. 离职 key 去 198 置 blocked

```bash
# ① 名单：确认表「是否在职=离职」的行 → sha256(API Key)
# ② 查库（⛔ -n litellm-product，不是 dev）
kubectl -n litellm-product exec litellm-db-0 -- psql -U litellm -d litellm
#    DB 凭据在 pod env：POSTGRES_USER=litellm / POSTGRES_PASSWORD=pro-pg-pass-...
# ③ 封：POST /key/block  body {"key":"sk-..."}  Bearer=LITELLM_MASTER_KEY
#    master key 在 proxy pod 的 $LITELLM_MASTER_KEY，svc 10.43.149.225:4000
```

**判据必须是 `auth_error`，不能是 HTTP 403。** 09-30 我用错模型名拿到封禁前就 403 ——
封前封后都 403，**这把尺子读不出封禁有没有生效**。两个族的模型名前缀是反的：

| key 族 | 正确模型名 | 用错了会得到 |
|---|---|---|
| `cursor-*` | `claude-haiku-4-5`（裸名） | `key_model_access_denied` 403 |
| `claude-code-*` | `anthropic.claude-haiku-4-5` | 同上 |

- **已封** → `{"type":"auth_error","code":"401"}`
- **未封** → 200 且返回真实内容
- `key_model_access_denied` ≠ 被封：那是**认证过了**、只是模型不在白名单。

🔴 **blocked 只挡认证，不清白名单。** 封完 `models` 原封不动 ⇒
将来谁解封就**静默恢复全部权限**。要彻底撤权得另跑
`litellm-198-key-allowlist.py --include-blocked`（不可逆，且必须带这个 flag
才能覆盖已 blocked 的，见 [[feedback_prefix_sweep_is_not_the_intended_set]]）。

⚠️ **封之前查近期流量**，不然会掐掉在用的人。09-30 那轮 54 把待封里
**4 把当天还在跑**（7022 / 1165 / 232 / 364 次），得先问 leader 再封：

```sql
select v.key_alias, v.blocked, count(*), max(s."startTime")
  from "LiteLLM_SpendLogs" s join "LiteLLM_VerificationToken" v on v.token=s.api_key
 where s.api_key = any($1) and s."startTime" > now() - interval '14 days'
 group by 1,2 order by 3 desc;
```

## 7. 写飞书表的通用纪律

- `"ok": true` **不证明写进去了**，一律回读逐字段比（含「非目标行未被改动」这腿，
  拿改前快照做对照）。
- batch-update **逐字段覆盖** ⇒ 只发你真想改的字段
  （[[feedback_bitable_batch_update_only_send_fields_you_mean_to_change]]）。
- ⛔ 删行是 `+record-delete --json '{"record_id_list":[...]}' --yes`，
  **没有** `+record-batch-delete` 这个命令（它存在但报空输出）。
- ⛔ lark-cli 的 `--json @file` 有的子命令吃、有的不吃：`+role-create` 不吃 `@`，
  得 `--json "$(cat f.json)"`。
- 198 上写临时文件**用唯一名**（`/tmp/lgx-<ts>.sql`）—— `/tmp/q1.sql` 这种会撞上
  别人的文件，读出来是**别人的查询结果**，我踩过一次。

## 8. 相关

- 脚本：`scripts/lark-org-tree-sync.py`
- [[reference_lark_cli_base_and_sheets_invocation]] — lark-cli 的调用形状
- [[feedback_bitable_collaborator_is_not_row_access]] — 协作者 ≠ 能读到行
- [[project_lark_key_table_synced_from_198_2026_09_26]] — 同一张表的模型列同步
- [[feedback_198_prod_db_is_litellm_product_not_dev]] — 生产库是哪个 ns
- skill `lark-key-models-sync`、`litellm-198-key-block`（nginx 层封禁，另一回事）
