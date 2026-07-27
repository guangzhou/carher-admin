# test_decay.py -- _decay() 单元测试(统一后的半衰期衰减)
#
# 为什么需要:原先 _decayed_err / _decayed_refuse 是两份近乎相同的实现,
# 任何修补(如 dt<=0 时钟回拨守卫)都要改两处,漏一处就会静默漂移。
# 合并成 _decay 后用这些 case 钉住行为,含 halflife=0 的除零守卫。
#
# 跑: python3 testkit/test_decay.py   (从 bridge/ 目录)
import importlib.util,sys,types,os
os.environ.setdefault("ZK_KEY","x")
spec=importlib.util.spec_from_file_location("br",os.path.join(os.path.dirname(os.path.abspath(__file__)),"..","zerokey-codex-responses-bridge.py"))
m=importlib.util.module_from_spec(spec)
try: spec.loader.exec_module(m)
except SystemExit: pass
except Exception as e:
    print("import failed:",type(e).__name__,str(e)[:100]); sys.exit(0)
d=m._decay
S={"a":0.8}; T={"a":1000.0}
cases=[
 ("零分立即返回0", d({"a":0.0},T,"a",300,1000.0), 0.0),
 ("无 seen 记录返回原值", d(S,{},"a",300,1000.0), 0.8),
 ("dt=0 返回原值", d(S,T,"a",300,1000.0), 0.8),
 ("dt<0 时钟回拨返回原值", d(S,T,"a",300,999.0), 0.8),
 ("一个半衰期减半", d(S,T,"a",300,1300.0), 0.4),
 ("两个半衰期四分之一", d(S,T,"a",300,1600.0), 0.2),
 ("halflife=0 不炸(返回0)", d(S,T,"a",0,1600.0), 0.0),
 ("未知key返回0", d(S,T,"zz",300,1600.0), 0.0),
]
bad=0
for n,got,want in cases:
    ok=abs(got-want)<1e-9
    if not ok: bad+=1
    print(("PASS  " if ok else "FAIL  ")+n+"  got=%.4f want=%.4f"%(got,want))
print(("\n%d 条失败"%bad) if bad else "\n8/8 通过")
sys.exit(1 if bad else 0)
