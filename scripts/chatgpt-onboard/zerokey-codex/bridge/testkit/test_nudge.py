# test_nudge.py -- 结构重试 nudge 注入的不变量
#
# 背景:结构重试原来只换 pod,messages 一字未改 -> 模型没有任何理由改变行为,
# 生产实测救回率上界仅 8.4%(83 次触发,76 次仍是文本)。
# Cline 的做法是喂一条显式错误轮(prompts/responses.ts:33 noToolsUsed),
# 是"改变信号"而不是"再摇一次骰子"。
#
# 必须钉住的不变量:
#   1. 非重试路径 messages 一字不改(prompt cache 前缀必须稳定)
#   2. 用拼接而非 append —— 不能 mutate 调用方的 list
#   3. 注入 user 角色,不动 instructions/system
#
# 跑: python3 testkit/test_nudge.py
# nudge 注入的不变量:不能改原 messages、只在重试时加、必须是 user 角色
import os,re,sys
here=os.path.dirname(os.path.abspath(__file__))
src=open(os.path.join(here,"..","zerokey-codex-responses-bridge.py"),encoding="utf-8").read()
blk=re.search(r'round_messages = messages\n(.*?)futs = \{', src, re.S).group(0)
bad=0
def chk(n,c):
    global bad
    if not c: bad+=1
    print(("PASS  " if c else "FAIL  ")+n)
chk("默认不改 messages(round_messages = messages)", 'round_messages = messages\n' in blk)
chk("只在 struct_retries 真值时注入", re.search(r'if struct_retries:', blk) is not None)
chk("用拼接不是 append(不改原 list)", 'messages + [{' in blk)
chk("注入的是 user 角色", '"role": "user"' in blk)
chk("提交用 round_messages 而非 messages", '_call_one, b, round_messages,' in src)
chk("原 messages 未被 mutate", '.append(' not in blk and 'messages +=' not in blk)
chk("含 ERROR 明示语", '[ERROR] You did not use a tool' in blk)
print(("\n%d 失败"%bad) if bad else "\n7/7 通过")
sys.exit(1 if bad else 0)
