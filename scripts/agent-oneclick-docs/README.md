# 三个 agent 一键配置的飞书文档(正文生成器)

- `bodies.py`:五篇文档的正文(Cursor×2 / Codex 内部+外部分享 / Claude Code),同一骨架同一措辞。
  `⟦ZIP⟧` `⟦MAC⟧` `⟦IMG0⟧` 等占位 = 文档里原有的附件/图片,套用时原样挪回。
- `apply.py --all`:把正文套到五篇文档;`apply.py <doc_id>` 只套一篇。
- 收口:`python3 scripts/agent-oneclick-docs-audit.py`(附件回环 sha、两篇 Cursor 逐字相同、陈旧词 0)。

写文案的规矩(用户 10-13/14 反复点名):短、命令/路径进代码块、不写原理和后端线路名、
不让用户手填网关地址、CLI 与 App 并列写、三篇骨架一致、要先装的东西放前面。
