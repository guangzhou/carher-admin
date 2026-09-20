#!/usr/bin/env python3
"""chatgpt-creds-audit.py — 跑续订/re-OAuth **之前**先体检凭据 CSV，别让配置错冒充账号故障。

为什么要这个(2026-09-12 acct-173/174 实证):
  这两个号连续多轮报 `mail.com login failed`，一路被当成"邮箱 flaky，重跑一次"。真因是
  188 的 .creds 里**没有这两个号真正的邮箱密码** —— 填的是它们的 ChatGPT 密码。拿 GPT
  密码去登 mail.com 必然失败，重跑一百次也一样。换成飞书表里的真邮箱密码后，mail.com
  当场登进去了(inbox loaded / OTP 取到)，失败形状随之改变。

  ⚠️⚠️ **别用"mail_pw == gpt_pw 字面量相同"当判据 —— 这条判别力是零。**
  当轮 21 行**全部**两栏相同，其中 19 个号照样正常登进 mail.com 并取到 OTP: 卖号商本来
  就常给一个通用密码，两栏相同是**正常形态**。(这是本脚本第一版写死的规则，跑阳性对照
  时当场被自己的数据证伪 —— 21/21 全中，含全部 19 个成功号。留作反面教材，别再加回来。)
  真正有判别力的只有一条: **按邮箱去飞书权威源比对，mail_pw 与表里的「邮箱密码」不一致**。

  ⚠️ 编号会漂: 这两个邮箱在飞书表里记在 **acct-163/164** 名下，按编号 join 根本找不到行，
     会得出"表里没有这个号"的假结论。**唯一可靠的 join key 是邮箱**。

用法:
  python3 scripts/chatgpt-creds-audit.py /tmp/renew-creds.csv --sheet-csv /tmp/sheet-acct.csv
  (不带 --sheet-csv 时只能做空值/列错位这类弱检查，**查不出 173/174 那种形状**，会明确提示)

拉飞书表:
  lark-cli sheets +csv-get --spreadsheet-token FRVJsbGsTh9kWNtp7uycrPSYnzc \\
      --sheet-id 0MAGgd --range A1:H200 --format csv > /tmp/sheet-acct.csv

CSV 列: N,email,mail_pw,chatgpt_pw[,totp]
飞书表(--sheet-csv)当前表头: acct,邮箱,GPT密码,邮箱密码,短信取号,卡密,2FA密钥,2FA接码地址
  ⚠️ 表头**漂过**: `GPT密码` 是后插进 C 列的，把 `邮箱密码` 挤到了 D。所以本脚本按**列名**
     取值，不按列号；表头再变只需确认名字还在，不必改代码。
  ⚠️ 这张表**不是全量**: 实测覆盖 81~244 但 175~194 整段缺失，查不到 != 号有问题。

本脚本**不回显任何密码明文**，只报"一致/不一致"。

退出码: 0=无高危  1=有高危(HIGH)  2=用法/读取错误
"""
import csv
import json
import re
import sys
from pathlib import Path

HIGH, WARN, INFO = "HIGH", "WARN", "INFO"


def load_csv(p):
    rows = []
    with open(p, newline="") as f:
        for r in csv.reader(f):
            if not r or not r[0].strip() or r[0].lstrip().startswith("#"):
                continue
            r = (r + [""] * 5)[:5]
            rows.append({"n": r[0].strip(), "email": r[1].strip(),
                         "mail_pw": r[2].strip(), "gpt_pw": r[3].strip(),
                         "totp": r[4].strip()})
    return rows


def load_sheet(p):
    """飞书表 -> {邮箱小写: {列名: 值}}。按邮箱建索引，**不按 acct 编号**。

    吃两种格式:
      ① lark-cli 原生输出(JSON)。⚠️ `sheets +csv-get` 尽管叫 csv，吐的是 **JSON 包装**,
         真正的表在 `data.annotated_csv`，且**每行前面带 `[row=N] ` 前缀**。
         直接当 CSV 喂给 csv.DictReader **不会报错**，只是一行都匹配不上 —— 静默读出
         零行，然后你会以为"表里没这个号"(2026-09-12 踩过)。
      ② 已经剥好的纯 CSV。
    """
    raw = Path(p).read_text()
    text = raw
    if raw.lstrip().startswith("{"):
        try:
            j = json.loads(raw)
            text = (j.get("data") or {}).get("annotated_csv") or ""
        except Exception:
            text = raw
    lines = []
    for ln in text.splitlines():
        lines.append(re.sub(r"^\[row=\d+\]\s?", "", ln))

    by_email = {}
    for row in csv.DictReader(lines):
        em = ""
        for k, v in row.items():
            if k and "邮箱" in k and "密码" not in k and v and "@" in v:
                em = v.strip()
                break
        if em:
            by_email[em.lower()] = {(k or "").strip(): (v or "").strip()
                                    for k, v in row.items()}
    return by_email


def pick(d, name):
    return d.get(name, "")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    src = args[0]
    sheet_csv = None
    if "--sheet-csv" in args:
        i = args.index("--sheet-csv")
        if i + 1 >= len(args):
            print("FATAL: --sheet-csv 后面要跟文件路径")
            return 2
        sheet_csv = args[i + 1]

    if not Path(src).is_file():
        print(f"FATAL: 读不到 {src}")
        return 2
    rows = load_csv(src)
    sheet = load_sheet(sheet_csv) if sheet_csv else {}
    if sheet_csv and not sheet:
        print(f"⚠ {sheet_csv} 里一行带 @ 的邮箱都没解析出来 —— 表头可能又漂了，先核对列名")

    findings = []
    matched = 0
    for r in rows:
        n, em = r["n"], r["email"]

        # 弱检查: 结构性错误(空值/列错位)。查不出 173/174 那种形状。
        if not r["mail_pw"]:
            findings.append((HIGH, n, em, "邮箱密码为空 — 收不到 OTP，续订/认证都跑不动"))
        if not r["gpt_pw"]:
            findings.append((WARN, n, em, "GPT 密码为空 — FORCE_OTP_LOGIN=1 时可容忍，否则卡密码页"))
        if em and "@" not in em:
            findings.append((HIGH, n, em, "邮箱字段不含 @ — 列顺序可能错位"))

        # 强检查: 按**邮箱**跟飞书权威源比对。这是唯一能抓出 173/174 的规则。
        if not sheet:
            continue
        row = sheet.get(em.lower())
        if not row:
            findings.append((INFO, n, em, "飞书表按邮箱查不到(该表 175~194 整段缺失) — 无权威源可核"))
            continue
        matched += 1
        s_acct = pick(row, "acct").replace("acct-", "").strip()
        s_mail = pick(row, "邮箱密码")
        s_gpt = pick(row, "GPT密码")
        if s_acct and s_acct != n:
            findings.append((INFO, n, em,
                             f"编号漂移: 飞书把这个邮箱记在 acct-{s_acct} — 按编号 join 会查空，"
                             f"按邮箱对就行"))
        if s_mail and r["mail_pw"] and s_mail != r["mail_pw"]:
            extra = "(本地填的正是该号的 GPT 密码 = 经典错填)" if r["mail_pw"] == r["gpt_pw"] else ""
            findings.append((HIGH, n, em, f"邮箱密码与飞书不一致 — 以飞书为准{extra}"))
        if s_gpt and r["gpt_pw"] and s_gpt != r["gpt_pw"]:
            findings.append((WARN, n, em, "GPT 密码与飞书不一致 — 可能改过密码，确认哪个新"))

    head = f"==== 凭据体检: {src} ({len(rows)} 行"
    if sheet:
        head += f"，飞书 {len(sheet)} 行按邮箱比对，对上 {matched} 个)"
    elif sheet_csv:
        head += "，飞书表解析出 0 行 ← 比对没生效)"
    else:
        head += "，未接飞书)"
    print(head + " ====")
    if not sheet:
        why = ("传了 --sheet-csv 但一行都没解析出来" if sheet_csv else "未接 --sheet-csv")
        print(f"⚠ {why}: 只做了空值/列错位弱检查。"
              "acct-173/174 那种「邮箱密码是错的」形状**查不出来**，这不是「体检通过」。")
    order = {HIGH: 0, WARN: 1, INFO: 2}
    for lv, n, em, msg in sorted(findings,
                                 key=lambda x: (order[x[0]], int(x[1]) if x[1].isdigit() else 0)):
        print(f"  [{lv}] acct-{n:<4} {em:<32} {msg}")
    nh = sum(1 for f in findings if f[0] == HIGH)
    nw = sum(1 for f in findings if f[0] == WARN)
    print(f"---- HIGH={nh}  WARN={nw}  INFO={len(findings) - nh - nw} ----")
    if nh:
        print("HIGH 项必须先修凭据再跑 Job，否则失败会伪装成账号故障。")
    elif sheet:
        print("✅ 与飞书比对无高危")
    return 1 if nh else 0


if __name__ == "__main__":
    sys.exit(main())
