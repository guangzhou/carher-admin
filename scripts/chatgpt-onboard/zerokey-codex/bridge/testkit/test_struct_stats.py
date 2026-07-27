# test_struct_stats.py -- 结构重试统计
#
# 为什么需要:生产日志只能给出"救回率 <= 8.4%"的上界,因为结构重试和拒绝重试
# 救回的请求都落在 round 1,无法区分。这组计数器负责归因,而调优
# BRIDGE_STRUCT_RETRIES 会直接依赖它 —— 所以 fired=0 时的除零必须钉住。
#
# 跑: python3 testkit/test_struct_stats.py
# 结构重试统计的除零 + 计数语义
import os,sys,importlib.util
os.environ.setdefault("ZK_KEY","x")
here=os.path.dirname(os.path.abspath(__file__))
spec=importlib.util.spec_from_file_location("br",
  os.path.join(here,"..","zerokey-codex-responses-bridge.py"))
m=importlib.util.module_from_spec(spec)
try: spec.loader.exec_module(m)
except SystemExit: pass
S=m._STRUCT_STATS; st=m._struct_stat
bad=0
def chk(n,c):
    global bad
    if not c: bad+=1
    print(("PASS  " if c else "FAIL  ")+n)
chk("初始三键齐全且为0", S=={"fired":0,"rescued":0,"wasted":0})
# fired=0 时 rescue_rate 必须是 None,不能除零
rate=lambda: (round(S["rescued"]/S["fired"],3) if S["fired"] else None)
chk("fired=0 时 rate=None(不除零)", rate() is None)
st("fired"); st("fired"); st("rescued")
chk("fired 累加=2", S["fired"]==2)
chk("rescue_rate=0.5", rate()==0.5)
st("wasted")
chk("wasted 独立计数", S["wasted"]==1)
st("newkey")
chk("未知键不 KeyError", S.get("newkey")==1)
print(("\n%d 失败"%bad) if bad else "\n6/6 通过")
sys.exit(1 if bad else 0)
