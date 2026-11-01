# 槽位出口测速 + 排名（36 出口，10-31）

把所有 kiro 槽出口的「连 Kiro / 下载 / 综合分 / 名次 / 来源」算出来，写进 30891 槽位配置和飞书文档。

## 跑的顺序

```bash
# 0) 198 上以 cltx（不要 sudo：脚本读 ~/kiro-us-egress/.ak，sudo 会把 ~ 变成 /root）
#    先看哪些槽还没 bench，以及它们的白名单放了什么
scp bench/probe_allow.py cltx@10.68.13.198:/tmp/ && ssh cltx@10.68.13.198 'sudo python3 /tmp/probe_allow.py'

# 1) 实测：连 Kiro = 18 次中位数；下载 = 4×10MB 中位数（白名单放得过去的才测）
ssh cltx@10.68.13.198 'sudo python3 /tmp/bench_self5.py' > self5.tsv

# 2) 36 个一起重算归一化排名（iGuge 31 来自飞书文档快照 + 自建 5 来自上一步）
python3 bench/rank36.py doc-before.md self5.tsv > rank36.json

# 3) 写进槽位配置：先 dry-run 看每行决定，再 --apply
#    （plan36.json 的构造见 .work/px36 里的那段：按出口 IP 把 rank36 对到现有槽）
ssh cltx@10.68.13.198 'python3 ~/kiro-px-egress/kiro-slots-bench-apply.py /tmp/plan36.json --measured "..."'
ssh cltx@10.68.13.198 'python3 ~/kiro-px-egress/kiro-slots-bench-apply.py /tmp/plan36.json --measured "..." --apply'

# 4) 飞书文档（来源列 + 现状列 + 4 张表全部 36 行）
python3 bench/build_doc.py rank36.json slots_live.json > doc-new.xml
lark-cli docs +update --doc <token> --as user --command overwrite --doc-format xml --content @doc-new.xml
```

## 必须记住的几条

- 🔴 **综合分是「本次集合内」归一化**（连 Kiro 60% + 下载 40%，各项按集合最好/最差拉成 0~100）。
  集合一变，**所有名次和分数都要重算**，不能把新出口插进旧排名里 ——
  10-31 加 5 个自建进去，31→36，`jp-64.118.145.214` 93.8→93.6、`kr-203.231.254.184` 第 8→第 9。
- 🔴 **阿里云 4 个 squid 出口的「下载」是 EIP 带宽上限，不是实测**：白名单只放 Kiro 接口 +
  `api.ipify.org`，7 个下载源全部 `ERR:URLError`。上限取自阿里云控制台（40/20/5/1 Mbps）。
  ⇒ 下载这一列**不同源不可比**，渲染时必须标「上限」，`rank36.json` 里有 `down_is_cap` 供下游判断。
  连 Kiro 这项 5 个都是实测，和 iGuge 同口径，可以直接比。
- 🔴 **不要为了测下载去改在带量的 squid 白名单**（要动 4 个在服务的出口），没拍板别动。
- 🔴 **凭据不上命令行、不用 `curl -v`**：squid 的 `Proxy-Authorization` 头就是密码的 base64，
  `-v` 会把它打进日志/会话记录（10-31 我犯过，见 `probe_allow.py` 头部注释）。
  真实代理密码从槽位 PVC 的 `config.json` 读（Admin API 回的是 `******`），读了也不要打印。
- 🔴 **槽号 ≠ 数组下标**：页面「槽 N」只数 `enabled` 的槽，停用槽不在卡片里。
  `build_doc.py` 的「现状」列就是按这个口径算的，两边同源才不会漂。
