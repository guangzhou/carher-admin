---
name: lark-docx-whiteboard-authoring
description: >-
  用 lark-cli 写"给同事看的飞书操作文档"：整篇 overwrite + 画板(whiteboard)手写 SVG + 图片/附件复用。
  当需要新建或重写飞书 docx、往文档里画板画图、判断画板/图片有没有真的落进文档、
  或从文档里把附件拉下来比对时使用。踩过的坑全在这里，按这条路走能一次成。
---

# 飞书 docx + 画板 写作 SOP

对象:面向同事的"照着做就能装上"的操作文档。写作动作只有三类 —— 整篇 `docs +update --command overwrite`、
画板 `whiteboard +update --input_format svg`、素材(图片/附件)按 token 复用。

**域指南在 lark-cli 自带 skill 里**(`lark-cli skills read lark-whiteboard` / `lark-doc`),
本文件只记**那里没写、我们实测踩过**的形状。⛔ 不要去改 `~/.claude/skills/lark-*`(用户常驻配置)。

## 硬规矩

1. **`<whiteboard>` 只认 `token=` 或 `type=`,没有 `src=`。**
   `<img src="tok">` / `<source token="tok">` 各是一套,`src=` 的手感会串过来。写错的后果不是报错:
   `degrade_code=5004 Required attribute group on <whiteboard> is missing; require at least one of: token, type`,
   整块被**静默丢掉**,文档还是 `partial_success`。
2. **`ok:true` 不等于写成了。** 每次写完必须读 `.data.result` 和 `.data.warnings[].degrade_code`,
   `result: failed` 和 `degrade_code` 都藏在 `ok:true` 底下。
3. **overwrite 会删掉画板块,而孤儿画板克隆不回来。** 见下节的占位符流程。
4. **写整篇之前先把原文备份成 json**,正文里写清"动了啥·备份在哪·怎么回滚"。

## overwrite 会杀掉画板 ⇒ 占位符流程

`docs +update --command overwrite` 用新 XML 整篇重建,文档里的画板块跟着消失。
拿旧 token 想把它插回来会失败:

```
<whiteboard token="AfkGwj...">  ⇒  result: failed
  degrade_code=2105  Whiteboard clone failed. Retry later
  degrade_code=1003  Instruction produced empty content after transform
```

**孤儿画板不能再被引用**,只能重新开一块空板。所以每次 overwrite 都按这个顺序走:

```bash
# 1) 新 XML 里画板位置放纯文本占位符,而不是 <whiteboard token=...>
#    (Python 里先 assert 占位目标在文件里,替换失败要当场停,不能静默跳过)
#    <p>__BOARD_PLACEHOLDER__</p>

# 2) 整篇覆盖
lark-cli docs +update --document-id <doc> --command overwrite --source @doc.xml --as user

# 3) 找到占位段的 block_id
lark-cli docs +blocks-list --document-id <doc> --as user | grep -B3 __BOARD_PLACEHOLDER__

# 4) 换成一块**空板**(type="blank",不是 token=)
lark-cli docs +update --document-id <doc> --command block_replace \
  --block-id <ph_id> --source '<whiteboard type="blank"></whiteboard>' --as user

# 5) 取新板 token,把 SVG 写进去
lark-cli whiteboard +update --whiteboard-token <new_token> \
  --input_format svg --source @diagram.svg --overwrite --as user
```

每轮 overwrite 会留下一块孤儿画板(旧 token 还在,内容还在,只是不在文档里)。
**把孤儿 token 记进交付说明**,否则回滚时找不到上一版的图。

## 判"画板有没有落进去":`--output-type raw`,不是 preview

`+export --output-type preview` 回的是 **`.jpg`**,给 `.png` 路径会被拒
(`preview response is .jpg but output path has extension .png`)。
更要紧的是它的画布是**补白到 2560×2560 的正方形,内容挤在一角** ——
肉眼看就是"一片空白"。**这是假红形状**,不许据此说画板写失败。

唯一可信的尺子是节点结构:

```bash
lark-cli whiteboard +export --whiteboard-token <tok> --output-type raw --as user \
  | jq '{n: (.data.nodes|length),
         bbox: {x0:([.data.nodes[].x]|min), x1:([.data.nodes[].x]|max),
                y0:([.data.nodes[].y]|min), y1:([.data.nodes[].y]|max)},
         kinds: ([.data.nodes[].type]|group_by(.)|map({(.[0]):length})|add)}'
```

判据三条:节点数 > 0、bbox 落在 SVG 画布尺寸内、同一元素**只有一份**(重复说明写了两遍没 `--overwrite`)。
**画板在文档里的视觉渲染我量不到** —— 只能证明块存在、token 对、几何对;好不好看要用户自己看一眼。

## SVG 落板前先本地 check

画板端把字体硬编码成 Noto Sans SC,文字必须是 `<text>`/`<tspan>`;认 `rect`/`circle`/`ellipse`/`polygon`,
连线用 `line`/`polyline`/`path`;渐变要 ≥2 个 `<stop>`;阴影只能 `<feDropShadow>`(最多一层)。
**不支持 `<pattern>` / `<clipPath>` / `<mask>` / 非阴影 `<filter>`**,避开 `skewX`/`skewY`/`matrix()`。
中文按 ≈1em、拉丁按 ≈0.6em 折行。

```bash
npx -y @larksuite/whiteboard-cli@^0.2.13 -i diagram.svg -o diagram.png -f svg
npx -y @larksuite/whiteboard-cli@^0.2.13 -i diagram.svg -o diagram.png -f svg --check
```

`--check` 报 `text-overflow` / `node-overlap` / `text-occlusion`。实测两类高发:
装饰性大圆压在卡片下面 ⇒ 两条 `node-overlap`(删掉圆就干净);中文标签压着容器右边界 ⇒ `text-overflow ... by 23px`。
**要求 0 errors / 0 warnings 再落板**,别把 warning 带上去。

## 图形形状:用户要的是对齐表格,不是判定流程图

2026-09-21 实测被打回一次:菱形判定 + 折线分支 + 虚线括号分组的"流程图"被评为**比较乱**。
改成**零连线的四行对齐布局**(列头「你的情况 / 双击这个 / 会问 Key 吗」+ 四行等宽卡片,
按钮 chip 和答案列 x 坐标全部对齐)当场通过。

给非技术同事看的"我该点哪个"类图:**先考虑无连线的对齐行,再考虑连线**。
连线数从 5 降到 0,check 也从 1 error / 2 warn 降到 0/0。

## 素材 token:每次 overwrite 都会变,token 变 ≠ 文件换了

`<img src="tok">` 和 `<source token="tok">` 是**复制资源**语义 —— 同样的字节,每次 overwrite 都拿到
一个**新的**块 token。所以"token 和上次不一样"不能读成"传错文件了"。
判据是 `size=` 加一次 sha256 往返(把附件拉下来重新算)。

从文档里把附件拉下来:

```bash
lark-cli docs +media-download --token <file_token> --output /tmp/old.zip --as user
# --type media | whiteboard
```

⛔ `lark-cli drive +download --token ...` 对文档附件**静默不落盘**(输出看着正常,`ls` 说 No such file)。

## 其他实测形状

- `<ol seq="auto">` 不合法(`degrade_code=5002 Unsupported attribute "seq"`),内容照样建、编号自动。
- `lark-cli --file` / `--source @file` 只认 cwd、`/tmp`、`~/files` 三处。
- 画板产物目录约定 `./diagrams/YYYY-MM-DDTHHMMSS/`,固定文件名 `diagram.svg` / `diagram.png` / `diagram.json`。

## 文档瘦身的落点(同事读得快的那几刀)

2026-09-21 把一篇 14458 字压到 6723 字,起效的是这几刀,按收益排:

1. **删表格。**4 张表全删,改成分段落 + 画板一张图。表格在手机飞书上最难读。
2. **拉平标题层级。**`<h2>` 全部并进 `<h1>`,章名里的括号解释删掉(「省流量的小代理」后面那串括号只留一处)。
3. **图片挪到段末。**截图/GIF 跟在文字后面而不是插在中间,效果 GIF 放全文最后。
4. **正文只留动作。**"为什么这样设计"一律删;`<ol>` 里一步一句话。

删除/覆盖类改动,正文必须写清备份路径(如 `~/.cursor-team-setup-backup/`)和"备份写不下去就中止"。
