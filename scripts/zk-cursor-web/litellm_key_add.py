#!/usr/bin/env python3
"""litellm_key_add.py —— 给一把 key 追加 models / aliases（读-合并-写，不动其余），带备份与回读核对。

09-03 给 carher-13 照 carher-1 补 `claude-fable-5.1` / `claude-opus-5` 时写的，通用化保留。
    python3 litellm_key_add.py --alias carher-13 --models claude-fable-5.1,claude-opus-5 \
        --aliases '{"claude-fable-5.1":"claude-fable-5.1","claude-opus-5":"claude-opus-5"}' [--apply]
    python3 litellm_key_add.py --alias carher-13 --copy-from carher-1 --models claude-fable-5.1,claude-opus-5 [--apply]
        # --copy-from: aliases 里这些模型名对应的映射从参照 key 抄（参照 key 没有就不加）

规矩：
  · `/key/update` 的 models 与 aliases 都是**整表覆盖**，所以先 /key/info 读旧表、本地合并、整份写回。
  · 改前把旧 models+aliases 存到 198 `/Data/backups/key-<alias>-<ts>-pre-add.json`，回滚 = 整份写回。
  · 写后 GET 回读逐键核对；管理 API 对不认识的字段返 200，http 码不是判据。
  · token(hash) 只在 ssh 内和 pod 环境变量里流转，不进本地 argv。
  · 目标名字是否在模型表里能解析，脚本用 /model/info 查一遍，查不到就红（授权了也是空的）。
"""
import json, os, subprocess, sys, time

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SUDO = "sudo -n k3s kubectl -n %s " % NS


def _require_env(name):
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：写死一个真 PG 口令等于把凭据提交进仓库，而且口令轮转后
    老默认值还会静默生效，打出来的认证失败看不出是"忘了设 env"还是"口令真的换了"。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<litellm-db-0 的 PG 口令>（别写进文件/命令行历史）"
            % (name, name)
        )
    return v


PG_PW = _require_env("LITELLM_PG_PW")

POD_PROG = r'''
import json,os,sys,urllib.request,urllib.error
MK=os.environ['LITELLM_MASTER_KEY']; TOK=os.environ['TOK']; REF=os.environ.get('REF_TOK') or ''
ADD_M=[m for m in os.environ.get('ADD_M','').split(',') if m]; ADD_A=json.loads(os.environ.get('ADD_A') or '{}'); APPLY=os.environ.get('APPLY')=='1'
def call(m,p,d=None):
    r=urllib.request.Request('http://localhost:4000'+p,data=json.dumps(d).encode() if d is not None else None,method=m,headers={'Authorization':'Bearer '+MK,'Content-Type':'application/json'})
    try: return json.load(urllib.request.urlopen(r,timeout=60))
    except urllib.error.HTTPError as e: return {'HTTP_ERROR':e.code,'body':e.read()[:300].decode()}
known={r['model_name'] for r in call('GET','/model/info')['data']}
bad=[m for m in ADD_M if m not in known]
if bad: print('❌ 模型表里解析不到:',bad); sys.exit(2)
if REF:
    ref=(call('GET','/key/info?key='+REF)['info'].get('aliases') or {})
    for m in ADD_M:
        if m in ref and m not in ADD_A: ADD_A[m]=ref[m]
info=call('GET','/key/info?key='+TOK)['info']
old_m=list(info['models'] or []); old_a=dict(info.get('aliases') or {})
print('BACKUP '+json.dumps({'key_alias':info.get('key_alias'),'models':old_m,'aliases':old_a},ensure_ascii=False))
add_m=[m for m in ADD_M if m not in old_m]; add_a={k:v for k,v in ADD_A.items() if old_a.get(k)!=v}
print('现有 models %d, aliases %d;将加 models %s, aliases %s'%(len(old_m),len(old_a),add_m,add_a))
if not add_m and not add_a: print('已全有,无事可做'); sys.exit(0)
if not APPLY: print('dry-run'); sys.exit(0)
new_m=old_m+add_m; new_a={**old_a,**add_a}
payload={'key':TOK,'models':new_m}
if add_a: payload['aliases']=new_a
r=call('POST','/key/update',payload)
if 'HTTP_ERROR' in r: print('❌ update',r); sys.exit(1)
back=call('GET','/key/info?key='+TOK)['info']; ba=back.get('aliases') or {}
ok=all(m in back['models'] for m in new_m) and len(back['models'])==len(new_m) and all(ba.get(k)==v for k,v in new_a.items()) and len(ba)==len(new_a)
print('回读: models %d→%d, aliases %d→%d, 逐键核对 %s'%(len(old_m),len(back['models']),len(old_a),len(ba),'✅' if ok else '❌'))
sys.exit(0 if ok else 1)
'''

def sh_stdin(script, timeout=240):
    return subprocess.run(SSH + ["bash", "-s"], input=script, capture_output=True, text=True, timeout=timeout)

def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default

def main():
    alias = arg("--alias"); models = arg("--models", ""); aliases = arg("--aliases", "{}"); ref = arg("--copy-from", "")
    apply = "--apply" in sys.argv
    if not alias or not models:
        sys.exit(__doc__)
    json.loads(aliases)
    for v in (alias, models, ref):
        if any(c in v for c in "'\"$`;\n"): sys.exit("参数含非法字符: %r" % v)
    ts = time.strftime("%Y%m%d-%H%M%S")
    # 整段脚本经 ssh stdin 喂 bash -s:token 只在远端 shell 变量里,不进本地 argv;pod 程序用 heredoc 原样传。
    script = f"""set -e
K() {{ {SUDO} "$@" 2>/dev/null; }}
Q() {{ K exec litellm-db-0 -- env PGPASSWORD='{PG_PW}' psql -U litellm -d litellm -At -c "$1"; }}
TOK=$(Q "select token from \\"LiteLLM_VerificationToken\\" where key_alias='{alias}'")
[ ${{#TOK}} = 64 ] || {{ echo "❌ 按 alias {alias} 查不到唯一 token(${{#TOK}} 字符)"; exit 3; }}
REF=
if [ -n '{ref}' ]; then REF=$(Q "select token from \\"LiteLLM_VerificationToken\\" where key_alias='{ref}'"); [ ${{#REF}} = 64 ] || {{ echo '❌ 参照 key {ref} 查不到'; exit 3; }}; fi
PX=$(K get pod -l app=litellm-proxy -o jsonpath='{{.items[0].metadata.name}}')
T=/tmp/kadd_$$; cat > $T.py <<'PYEOF'
{POD_PROG}
PYEOF
K cp $T.py $PX:/tmp/kadd.py
set +e
K exec $PX -- env TOK="$TOK" REF_TOK="$REF" ADD_M='{models}' ADD_A='{aliases.replace("'", "")}' APPLY={1 if apply else 0} python3 /tmp/kadd.py 2>&1 | grep -v sitecustomize > $T.out
rc=${{PIPESTATUS[0]}}
K exec $PX -- rm -f /tmp/kadd.py; rm -f $T.py
if [ {1 if apply else 0} = 1 ] && grep -q '^BACKUP ' $T.out; then
  grep '^BACKUP ' $T.out | sed 's/^BACKUP //' > $T.bk
  sudo -n cp $T.bk /Data/backups/key-{alias}-{ts}-pre-add.json && echo "备份 /Data/backups/key-{alias}-{ts}-pre-add.json"
fi
grep -v '^BACKUP ' $T.out; rm -f $T.out $T.bk; exit $rc
"""
    r = sh_stdin(script)
    print(r.stdout.strip())
    if r.stderr.strip(): print(r.stderr.strip()[-500:], file=sys.stderr)
    return r.returncode

if __name__ == "__main__":
    sys.exit(main())
