# test_brake_recovery.py -- LOOP BRAKE 失败恢复分支的控制流
#
# 背景:codex 侧端到端回归里 multiturn-chain 失败。真因不是模型能力,而是
# 模型写了个有 bug 的一行命令(next=$(cat step1.txt) 拿到的是整行
# "next_file: step2_X.txt",cat 就拿到了错路径),重试同一条 -> brake 触发 ->
# brake 把 "check the command is valid ... tell me how to proceed" 丢给**用户**。
# 模型从没被告知命令坏了,所以没机会修一个一改就好的 bug。
#
# 与结构重试同一个教训:要**改变信号**,不要只是再摇骰子或直接放弃。
#
# 跑: python3 testkit/test_brake_recovery.py
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "..", "zerokey-codex-responses-bridge.py"),
           encoding="utf-8").read()

bad = 0
def chk(n, c, extra=""):
    global bad
    if not c: bad += 1
    print(("PASS  " if c else "FAIL  ") + n + ("" if c or not extra else "  -> " + extra))

chk("有 MAX_BRAKE_RETRIES 常量", "MAX_BRAKE_RETRIES = int(os.environ.get" in src)
chk("可用 env 关闭/调整", 'BRIDGE_BRAKE_RETRIES' in src)
chk("brake_retries 每请求初始化", re.search(r'brake_retries = 0', src) is not None)
chk("恢复分支受上限约束", "elif brake_retries < MAX_BRAKE_RETRIES:" in src)
chk("恢复前自增(防无限恢复)", re.search(r'brake_retries \+= 1', src) is not None)

# 切到"超限兜底"的 else 之后,否则会把恢复失败分支自己截掉
blk = src[src.find("elif brake_retries < MAX_BRAKE_RETRIES:"):]
blk = blk[:blk.find("LOOP BRAKE: repeated cmd %r -> converge to text")]
chk("把失败命令回灌给模型", "[ERROR] You issued the same command twice" in blk)
chk("明确要求换一条命令", "DIFFERENT command" in blk)
chk("给出可操作的修法提示", "sed/awk/cut" in blk)
chk("声明是自动消息", "Automated message" in blk)
chk("用拼接不 mutate 原 messages", "messages + [{" in blk)
chk("恢复调用标记 mid_task", "mid_task=True" in blk)
chk("拿到新 tool_call 才替换", re.search(r'if tcs2:', blk) is not None)
chk("恢复失败仍安全收敛", re.search(r'tcs = \[\]', blk) is not None)

# 旧的"甩给用户"文案必须仍存在(作为超限后的最终兜底),但不再是唯一路径
chk("超限后仍有兜底文案", "I stopped after repeating the same command" in src)

print(("\n%d 条失败" % bad) if bad else "\n14/14 通过")
sys.exit(1 if bad else 0)
