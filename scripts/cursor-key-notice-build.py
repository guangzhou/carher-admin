#!/usr/bin/env python3
"""给每个 Cursor Key 持有人发一张「模型权限已更新」卡片 —— 模型按各自 key 的真实权限现生成。

一条命令走完 2026-10-15 / 10-19 两轮手工跑过的全流程：
  pull     现拉飞书 key 表（「全部」视图）→ 筛「在职 + 账户类型含 Cursor」→ 按人去重
  build    逐把 key 对 198 库：模型清单**以库为准**（表是快照，10-19 有 1 行写的还是旧内部名）
           → 每人生成一张卡片 → 逐张过硬门禁
  send     逐人投递（限速 5/s，限流/网络错重试 3 次，230013 等永久错误跳过不重试）

为什么要逐人生成：每把 key 白名单不同（10-19：451 人 37 个、2 人 46 个），统一文案对一部分人是错的。
⛔ 别再拿客户端菜单 / router alias 推「能不能用」——白名单 + per-key alias 的真源在 198 库，
   表里的「可用模型」列由 `sync-lark-key-models.py` 从库同步，两者都过期时只认库。

选哪把 key（用户 10-19 定）：
  - 刘国现本人 ⇒ 永远 `cursor-liuguoxian-std`（他的实验 key）
  - 其他人多把 ⇒ 带姓名拼音那把（白羽 `cursor-baiyu` 而非 `cursor-t1v`）
  - 组织共享 key（`cursor-meituan-N` / `-qiaohu-N` / `-beiqiyanjiuyuan-N`）忽略
  - 只有 `deepseek-v4.1-flash` 一个模型的人（10-08 数据出境合规收窄，10-19 共 164 行）跳过
发不出去的（几乎全是跨租户 `230013`）直接跳过并记名单。

用法：
  export FEISHU_APP_ID=... FEISHU_APP_SECRET=...      # 「ai管理的her」那个 bot
  python3 scripts/cursor-key-notice-build.py pull                    # 落 .work/broadcast-cursor/run/
  python3 scripts/cursor-key-notice-build.py build                   # 生成卡片 + 门禁
  python3 scripts/cursor-key-notice-build.py send --only-me          # 先只发给刘国现看效果
  python3 scripts/cursor-key-notice-build.py send --batch <批次名>   # 全员；批次名决定 uuid

退出码：0 成功 / 1 门禁失败或有人投递失败（看 result.json）/ 2 参数或输入缺失 / 3 量具坏了
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / ".work" / "broadcast-cursor" / "run"
OPEN = "https://open.feishu.cn"

BASE_TOKEN, TABLE_ID, VIEW_ID = "DlT9bsrwMad12VsogEpcK9Ptncc", "tblJT2s6Y6xjYj5A", "vewISEKPg7"
MY_OPEN_ID = "ou_f7253e78ef0cba619d6272cecfb4efd9"          # 刘国现（本 bot 视角）
MY_KEY = "cursor-liuguoxian-std"
ORG_SHARE = re.compile(r"^cursor-(meituan|qiaohu|beiqiyanjiuyuan)-\d+$")
NAMED_KEY = re.compile(r"^cursor-[a-z]{3,}\d*(-[a-z0-9]{4})?$")
RANDOM_SUFFIX = re.compile(r"-[a-z0-9]{4}$")
SSH_198 = ["ssh", "-o", "ConnectTimeout=10", "-n", "cltx@10.68.13.198"]

# ---------------------------------------------------------------- 卡片内容常量
ONECLICK = "https://chat.auto-link.com.cn/oneclick/"
DASHBOARD = "https://chat.auto-link.com.cn/myai"
KEY_QUERY = "https://t83dfrspj4.feishu.cn/share/base/query/shrcnf5OElDguABGM3ZZDVoO3cb"
# 本轮主推（用户 10-19 点名）。⚠️ 口头说的 opus-5.5 / sonnet-5.5 不是真名，真名是 claude-opus-5-5。
# 10-27 用户：deepseek-v4.1-flash、kimi-k3 不再算主推。
HIGHLIGHT = {"claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.1-sol", "gpt-6.1-terra"}
# 体验版：在名字后面挂一个小标签（10-27 用户：gpt-6.1-terra（experience））。
# 只是展示标注，模型真名不变 ⇒ 门禁按真名比对，标签单独加在名字外面。
EXPERIENCE = {"gpt-6.1-terra"}
MODEL_TAG_COLOR = "green"
# 出图名：Cursor 不调用，单独一类并默认折叠
MEDIA = {"muse-image", "gpt-image-2", "gemini-image", "gemini-video", "gemini-music",
         "web-gemini-video", "gemini-3.1-flash-image"}
FAMILY_ORDER = ["Claude", "GPT", "Grok / Composer", "Gemini", "其他", "出图"]
# 老 key 白名单里直接写了上游组名，发给同事等于泄露后端线路 ⇒ 一律滤掉
INTERNAL_PREFIXES = ("ag-", "chatgpt-", "sa-", "kiro-", "zai-", "openrouter-",
                     "muse2api-", "zerokey-", "anthropic.", "cursor-fc-", "wangsu-")


def strip_internal(names: list[str]) -> tuple[list[str], list[str]]:
    keep = [n for n in names if not n.startswith(INTERNAL_PREFIXES)]
    drop = [n for n in names if n.startswith(INTERNAL_PREFIXES)]
    return keep, drop


def family(name: str) -> str:
    if name in MEDIA or name.startswith("grok-imagine"):
        return "出图"
    if name.startswith("claude-"):
        return "Claude"
    if name == "codex-auto-review" or name.startswith("gpt-"):
        return "GPT"
    if name.startswith("grok-") or name.startswith("composer"):
        return "Grok / Composer"
    if name.startswith("gemini-") or name.startswith("web-gemini-"):
        return "Gemini"
    return "其他"


def sort_key(name: str):
    """同族内按版本号从新到旧。"""
    nums = [int(x) for x in re.findall(r"\d+", name)]
    return (-(nums[0] if nums else 0), -(nums[1] if len(nums) > 1 else 0), name)


# 卡片 2.0 markdown 能用的行内样式（10-19 在 188 上拉官方文档原文核过）：
#   加粗 / 斜体 / 删除线 / <font color> / <text_tag>。⛔ 没有下划线（<u> 原样显示成字）、
#   没有任意背景色（带底色的只有 text_tag，字号固定约 12px，text_size 改不动）。
#   ⛔ column 没有 corner_radius（200621 拒收），圆角块只能用 interactive_container。
def emph(text: str, color: str = "red") -> str:
    return f"**<font color='{color}'>{text}</font>**"




def _md(content: str, **kw) -> dict:
    return {"tag": "markdown", "content": content, **kw}


NEW_TAG_COLOR = "orange"   # 新上的体验模型换一个色，和绿色主推区分开（10-27 用户）


def _tag(text: str, color: str) -> str:
    if text in EXPERIENCE:
        # 体验版无论在主推里还是在清单里，一律橙色标签 + 「NEW · experience」小字
        return (f"<text_tag color='{NEW_TAG_COLOR}'>{text}</text_tag>"
                f"<font color='orange'>（NEW · experience）</font>")
    return f"<text_tag color='{color}'>{text}</text_tag>"


def _block(bg: str, elements: list[dict], spacing: str = "8px", last: bool = False) -> dict:
    """带底色+圆角的块。🔴 column 没有 corner_radius（10-19 飞书 200621 拒收；连 lark-im
    卡片规范里的示例都写错了）⇒ 圆角块只能用 interactive_container。它要求 behaviors，
    不需要点击时给空数组。"""
    return {"tag": "interactive_container", "width": "fill",
            "background_style": bg, "corner_radius": "8px", "has_border": False,
            "padding": "12px 12px 12px 12px", "vertical_spacing": spacing,
            "margin": "0px" if last else "0px 0px 12px 0px",
            "behaviors": [], "elements": elements}


def _btn(text: str, url: str, kind: str) -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text},
            "type": kind, "width": "fill", "size": "medium",
            "behaviors": [{"type": "open_url", "default_url": url,
                           "pc_url": "", "ios_url": "", "android_url": ""}]}


def build_card(live: list[str]) -> dict:
    """10-19 定稿（用户：「按 11:17 那次微调」）= 11:17 版骨架 + 11:23 版「自助查询」+ 新看板链接。

    11:17 版：主推块（浅绿底、绿标签）→ 灰底大块里每类一张白卡、每个模型一个标签
    → 出图折叠 → 浅红底下线提醒 → 按钮一主两次；header 蓝色 + 「N 个模型」标签。
    11:23 版的自助查询：小标题 + 两张天蓝并排小卡，命令名用代码样式。
    模型清单按每把 key 自己的白名单生成，主推块只列这个人自己有的主推模型。
    """
    live, _ = strip_internal(live)
    buckets: dict[str, list[str]] = {}
    for m in live:
        buckets.setdefault(family(m), []).append(m)
    total = len(live)
    hl = [m for m in live if m in HIGHLIGHT]

    elements: list[dict] = []

    # ① 主推块：只列这个人自己有的主推模型；一个都没有就不出这块
    if hl:
        hl_sorted = sorted(hl, key=lambda m: (FAMILY_ORDER.index(family(m)), sort_key(m)))
        elements.append(_block("green-50", [
            _md("**本次主推**"),
            _md("  ".join(_tag(m, MODEL_TAG_COLOR) for m in hl_sorted)),
        ]))

    # ② 模型清单：每个模型一个标签，每类一张白卡；主推排本类最前；出图折叠
    def chips(models: list[str]) -> str:
        first = [m for m in models if m in HIGHLIGHT]
        rest = [m for m in models if m not in HIGHLIGHT]
        return " ".join([_tag(m, MODEL_TAG_COLOR) for m in first] +
                        [_tag(m, "neutral") for m in rest])

    fam_cards = []
    media_models: list[str] = []
    for fam in FAMILY_ORDER:
        got = sorted(buckets.get(fam, []), key=sort_key)
        if not got:
            continue
        if fam == "出图":
            media_models = got
            continue
        fam_cards.append({
            "tag": "interactive_container", "width": "fill",
            "background_style": "bg-white", "corner_radius": "8px", "has_border": False,
            "padding": "10px 12px 10px 12px", "vertical_spacing": "6px",
            "behaviors": [],
            "elements": [
                _md(f"**{fam}**　<font color='grey'>{len(got)}</font>"),
                _md(chips(got)),
            ]})
    inner = [_md(f"**你当前可访问的模型**　<font color='grey'>共 {total} 个</font>")] + fam_cards
    if media_models:
        inner.append({
            "tag": "collapsible_panel", "expanded": False,
            "background_color": "bg-white",
            "border": {"color": "grey-100", "corner_radius": "8px"},
            "padding": "4px 12px 10px 12px", "vertical_spacing": "6px",
            "header": {
                "title": {"tag": "markdown",
                          "content": f"**出图**　<font color='grey'>{len(media_models)} · 点击展开</font>"},
                "icon": {"tag": "standard_icon", "token": "down_outlined",
                         "color": "grey", "size": "16px 16px"},
                "icon_position": "right", "icon_expanded_angle": -180,
            },
            "elements": [_md(chips(media_models))],
        })
    elements.append(_block("grey-50", inner, spacing="8px"))

    # ③ 下线提醒
    elements.append(_block("red-50", [
        _md(emph("原先单独的 Claude Key 将于 10 月底下线", "red")),
        _md("后续只需一把 Cursor Key，即可覆盖以上全部模型，无需再单独申请和维护。"),
    ], spacing="4px"))

    # ④ 自助查询（11:23 版样式；10-27 加「刷模型」成三条）
    elements.append(_md("**自助查询**　<font color='grey'>在 Cursor / Codex / Claude Code 的对话框里直接发这几个字</font>",
                        margin="4px 0px 8px 0px"))

    def cmd_card(cmd: str, alias: str, desc: str) -> dict:
        head = f"`{cmd}`" + (f"　<font color='grey'>或</font> `{alias}`" if alias else "")
        return {"tag": "column", "width": "weighted", "weight": 1,
                "background_style": "wathet-50", "padding": "12px 12px 12px 12px",
                "vertical_spacing": "6px",
                "elements": [_md(head), _md(f"<font color='grey'>{desc}</font>",
                                            text_size="notation")]}

    # trisect：三列等宽；窄屏飞书会自动折行，不会横向溢出
    elements.append({
        "tag": "column_set", "flex_mode": "trisect", "horizontal_spacing": "8px",
        "margin": "0px 0px 16px 0px",
        "columns": [
            cmd_card("查余额", "cye", "今天用了多少、还剩多少；高配模型（Opus / Astra / Kimi-K3）单独一栏"),
            cmd_card("测模型", "", "把你能用的模型逐个试一遍，哪个能用、哪个不通一目了然"),
            cmd_card("刷模型", "", "按你的 Key 重新拉模型列表，新开通的出现、下线的消失"),
        ]})

    # ⑤ 按钮：一主两次
    elements.append(_btn("Cursor · Codex · Claude Code 一键配置", ONECLICK, "primary_filled"))
    elements.append({
        "tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "8px",
        "margin": "8px 0px 0px 0px",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [_btn("查看自己的 Key", KEY_QUERY, "default")]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [_btn("模型使用情况看板", DASHBOARD, "default")]},
        ]})

    return {
        "schema": "2.0",
        "config": {"width_mode": "default",
                   "summary": {"content": "你的 Cursor Key 模型权限已更新"}},
        "header": {
            "title": {"tag": "plain_text", "content": "Cursor Key 模型权限已更新"},
            "subtitle": {"tag": "plain_text", "content": "您好，已清理上游不再支持的旧模型"},
            "template": "blue",
            "icon": {"tag": "standard_icon", "token": "myai_colorful"},
            "text_tag_list": [{"tag": "text_tag",
                               "text": {"tag": "plain_text", "content": f"{total} 个模型"},
                               "color": "blue"}],
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px",
                 "elements": elements},
    }



# ---------------------------------------------------------------- 飞书 / 198 读写

def _http(method: str, path: str, token: str | None = None, body: dict | None = None,
          timeout: float = 30) -> dict:
    req = urllib.request.Request(OPEN + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:   # 飞书业务错误走 4xx，code/msg 在 body 里
        try:
            return json.loads(e.read()) | {"_http": e.code}
        except ValueError:
            return {"code": e.code, "msg": f"HTTP {e.code}", "_http": e.code}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"code": -2, "msg": f"network: {e}"}


def tenant_token() -> str:
    app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not secret:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        raise SystemExit(2)
    d = _http("POST", "/open-apis/auth/v3/tenant_access_token/internal",
              body={"app_id": app_id, "app_secret": secret})
    if d.get("code") != 0:
        print(f"换不到 tenant token：[{d.get('code')}] {d.get('msg')}", file=sys.stderr)
        raise SystemExit(3)
    return d["tenant_access_token"]


def txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ",".join((x.get("text") or x.get("name") or "") if isinstance(x, dict) else str(x)
                        for x in v)
    return str(v)


def read_table(token: str) -> list[dict]:
    rows, page_token = [], ""
    for _ in range(100):
        q = {"page_size": "500", "user_id_type": "open_id"} | ({"page_token": page_token} if page_token else {})
        d = _http("POST", f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{TABLE_ID}/records/search?"
                  + urllib.parse.urlencode(q), token,
                  {"view_id": VIEW_ID, "field_names": ["姓名", "账户类型", "key_alias", "可用模型", "是否在职"]})
        if d.get("code") != 0:
            print(f"读表失败：[{d.get('code')}] {d.get('msg')}", file=sys.stderr)
            raise SystemExit(3)
        data = d.get("data") or {}
        rows += data.get("items") or []
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            print("分页没正常结束，拒绝用不完整名单", file=sys.stderr)
            raise SystemExit(3)
    if not rows:
        # bot 读不到行时飞书是 code 0 + 0 行（没加协作者 / 高级权限滤光），不能当「表是空的」
        print("读到 0 行：先跑 scripts/feishu-bot-base-access-check.py 判是哪一层挡住", file=sys.stderr)
        raise SystemExit(3)
    return rows


def read_db_keys() -> dict[str, dict]:
    """198 上所有 cursor-* key 的白名单与封禁状态。DB 用户是 `litellm`（不是 llmproxy）。"""
    # SQL 走 stdin 而不是拼进 ssh 命令行：表名的双引号经 ssh → sudo → kubectl → psql 四层，
    # 拼在 -c 里转义极易错（10-19 整理这段时就写坏过一次）。
    sql = ("SELECT key_alias||'~'||coalesce(array_to_string(models,','),'')||'~'||"
           "coalesce(blocked::text,'f') FROM \"LiteLLM_VerificationToken\" "
           "WHERE key_alias LIKE 'cursor-%';")
    cmd = [c for c in SSH_198 if c != "-n"] + [
        "sudo -n kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm -t -A"]
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True, timeout=120)
    keys = {}
    for line in p.stdout.splitlines():
        parts = line.strip().split("~")
        if len(parts) >= 3 and parts[0].startswith("cursor-"):
            keys[parts[0]] = {"models": sorted(m for m in parts[1].split(",") if m),
                              "blocked": parts[2] == "t"}
    if len(keys) < 100:   # 10-19 是 710 把；读出这么少 = ssh/psql 坏了，不是 key 没了
        print(f"198 只读到 {len(keys)} 把 cursor key（rc={p.returncode}）：{p.stderr[-300:]}",
              file=sys.stderr)
        raise SystemExit(3)
    return keys


# ---------------------------------------------------------------- 三个子命令

def cmd_pull(a) -> int:
    RUN.mkdir(parents=True, exist_ok=True)
    rows = read_table(tenant_token())
    db = read_db_keys()
    (RUN / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (RUN / "db_keys.json").write_text(json.dumps(db, ensure_ascii=False), encoding="utf-8")
    print(f"表 {len(rows)} 行，198 cursor key {len(db)} 把 → {RUN}")
    return 0


def pick_key(open_id: str, rows: list[dict]) -> dict:
    if open_id == MY_OPEN_ID:
        std = [r for r in rows if r["key"] == MY_KEY]
        if std:
            return std[0]
    if len(rows) == 1:
        return rows[0]
    named = [r for r in rows if NAMED_KEY.match(r["key"])]
    return sorted(named or rows, key=lambda r: (len(r["key"]), r["key"]))[0]


def resolve_db_key(alias: str, db: dict, by_base: dict) -> str | None:
    """表里写的 alias 常缺随机后缀（表 `cursor-buyitian` / 库 `cursor-buyitian-pwcf`）。"""
    cand = [alias] if alias in db else by_base.get(alias, [])
    return sorted(cand, key=lambda s: (len(s), s))[0] if cand else None


def cmd_build(a) -> int:
    rows = json.loads((RUN / "rows.json").read_text())
    db = json.loads((RUN / "db_keys.json").read_text())
    by_base = collections.defaultdict(list)
    for k in db:
        by_base[RANDOM_SUFFIX.sub("", k)].append(k)

    per, skip = {}, collections.Counter()
    for r in rows:
        f = r["fields"]
        if "Cursor" not in txt(f.get("账户类型")) or txt(f.get("是否在职")) != "在职":
            continue
        ka = txt(f.get("key_alias")).strip()
        if ORG_SHARE.match(ka):
            skip["组织共享 key"] += 1
            continue
        for u in f.get("姓名") or []:
            per.setdefault(u["id"], {"name": u.get("name") or "", "rows": []})["rows"].append({"key": ka})

    out = RUN / "cards"
    out.mkdir(parents=True, exist_ok=True)
    send, skipped, bad = {}, [], []
    for oid, v in per.items():
        chosen = pick_key(oid, v["rows"])
        real = resolve_db_key(chosen["key"], db, by_base)
        if not real:
            skipped.append({"name": v["name"], "key": chosen["key"], "why": "198 无此 key"})
            continue
        if db[real]["blocked"]:
            skipped.append({"name": v["name"], "key": real, "why": "key 已封"})
            continue
        models = strip_internal(db[real]["models"])[0]      # 以 198 库为准
        if len(models) < 5:
            skip["只有 deepseek 等 <5 个模型"] += 1
            continue
        card = build_card(models)
        blob = json.dumps(card, ensure_ascii=False)
        problem = gate(card, blob, models)
        if problem:
            bad.append({"name": v["name"], "key": real, "why": problem})
            continue
        (out / f"{oid}.json").write_text(blob, encoding="utf-8")
        send[oid] = {"name": v["name"], "key": real, "models": len(models),
                     "n_keys": len(v["rows"])}

    (RUN / "send.json").write_text(json.dumps(send, ensure_ascii=False, indent=1), encoding="utf-8")
    (RUN / "skipped.json").write_text(json.dumps(skipped, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"待发 {len(send)} 人；门禁失败 {len(bad)}；跳过 {len(skipped)}；其它跳过 {dict(skip)}")
    print("每人模型数:", dict(collections.Counter(x["models"] for x in send.values())))
    print("不同卡片种数:", len({(out / f'{o}.json').read_text() for o in send}))
    for s in skipped:
        print("  跳过:", s)
    for b in bad[:10]:
        print("  门禁失败:", b)
    return 1 if bad else 0


def gate(card: dict, blob: str, models: list[str]) -> str:
    """每张卡发出前必过。返回空串 = 通过。"""
    if card.get("schema") != "2.0":
        return "不是 schema 2.0"
    chips = re.findall(r"<text_tag color='(?:green|neutral|orange)'>([^<]+)</text_tag>", blob)
    if set(chips) != set(models):
        return f"模型集合与本人 key 不符（多 {sorted(set(chips)-set(models))} 少 {sorted(set(models)-set(chips))}）"
    if any(m.startswith(INTERNAL_PREFIXES) for m in chips):
        return "含内部上游名"
    if f"{len(models)} 个模型" not in blob:
        return "header 数字不对"
    for need in ("10 月底下线", "`查余额`", "`cye`", "`测模型`", "`刷模型`", ONECLICK, KEY_QUERY, DASHBOARD):
        if need not in blob:
            return f"缺 {need}"
    if "<u>" in blob:
        return "含 <u>（卡片不支持下划线，会原样显示）"
    if len(blob.encode()) > 28000:
        return "超 28KB"
    return ""


def cmd_send(a) -> int:
    send = json.loads((RUN / "send.json").read_text())
    if a.only_me:
        send = {k: v for k, v in send.items() if k == MY_OPEN_ID}
        if not send:
            print("名单里没有刘国现", file=sys.stderr)
            return 2
        batch = f"preview-{int(time.time())}"          # 预览每次都要能重发
    else:
        if not a.batch:
            print("全员发送必须给 --batch <批次名>（决定 uuid；同一批次重跑不会重复投递）", file=sys.stderr)
            return 2
        batch = a.batch
    token, t0 = tenant_token(), time.time()
    res = []
    items = sorted(send.items(), key=lambda kv: kv[1]["name"])
    print(f"发送 {len(items)} 人，批次 {batch}", flush=True)
    for i, (oid, v) in enumerate(items, 1):
        if time.time() - t0 > 5400:
            token, t0 = tenant_token(), time.time()
        body = {"receive_id": oid, "msg_type": "interactive",
                "content": (RUN / "cards" / f"{oid}.json").read_text(encoding="utf-8"),
                "uuid": hashlib.sha256(f"{batch}:{oid}".encode()).hexdigest()[:40]}
        d = {}
        for attempt in (1, 2, 3):
            d = _http("POST", "/open-apis/im/v1/messages?receive_id_type=open_id", token, body)
            code = d.get("code")
            if code in (99991663, 99991668):            # token 失效
                token, t0 = tenant_token(), time.time()
                continue
            if code in (99991400, 230020, -2) or d.get("_http", 0) >= 500:   # 限流 / 网络 / 5xx
                time.sleep(2 ** attempt)
                continue
            break
        res.append({"open_id": oid, "name": v["name"], "key": v["key"], "models": v["models"],
                    "code": d.get("code"), "msg": str(d.get("msg", ""))[:160],
                    "message_id": (d.get("data") or {}).get("message_id", "")})
        if i % 50 == 0 or i == len(items):
            ok = sum(1 for x in res if x["code"] == 0)
            print(f"  {i}/{len(items)}  成功 {ok}  失败 {i - ok}", flush=True)
            (RUN / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        time.sleep(0.2)
    (RUN / "result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    bad = [x for x in res if x["code"] != 0]
    print(f"完成：成功 {len(res) - len(bad)}，失败 {len(bad)}；原因 {dict(collections.Counter(x['code'] for x in bad))}")
    # 230013 = 机器人触达不了：几乎全是跨租户（外部公司的人），机器人不能主动给外部用户开单聊
    with open(RUN / "failed.tsv", "w", encoding="utf-8") as fh:
        fh.write("姓名\tkey\tcode\n")
        for x in sorted(bad, key=lambda y: y["name"]):
            fh.write(f"{x['name']}\t{x['key']}\t{x['code']}\n")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull", help="现拉飞书表 + 198 库")
    sub.add_parser("build", help="逐人生成卡片 + 门禁")
    s = sub.add_parser("send", help="投递")
    s.add_argument("--only-me", action="store_true", help="只发刘国现（预览）")
    s.add_argument("--batch", help="全员发送的批次名，同一批次重跑由飞书按 uuid 去重")
    a = ap.parse_args()
    return {"pull": cmd_pull, "build": cmd_build, "send": cmd_send}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
