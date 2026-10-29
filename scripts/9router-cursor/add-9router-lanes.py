#!/usr/bin/env python3
"""把 9router 已经能出活的 Cursor 模型，批量反代成 198 prod LiteLLM 的 `cursor-fc-*` 车道。

为什么要先探针再建车道
----------------------
`/model/new` 返 200 只证明**写了一行配置**，不证明那个名字在上游存在、更不证明
它能出活。9router 侧一个不存在/没权限的模型名，表现是**首帧 45s 死线 → 504**，
而 `AvailableModels` 里它照样列着（见 skill 9router-cursor-fc-deploy 的坏尺子表）。

所以本脚本的顺序是硬的：

    真打一发带 nonce 的非流式请求(在 pod 内直连 9router) → 只有 nonce 回显的才建 lane

判据 = `HTTP 200 且 body 里有内容`。**不要求 nonce 严格回显**：实测 `grok-4.7`
会拒绝"只回这个 token"的指令而改说人话——那是模型脾气，不是通道故障。
nonce 只用来证明"这一发是我这次打的、不是缓存/串台"，所以两个判据分开记：
`ok`(通道活) 和 `echo`(听话),报表里都印出来。

车道形状(照抄线上 `cursor-fc-fable-5.1`,2026-09-22 实测)
--------------------------------------------------------
    model_name  = cursor-fc-<slug>
    model       = openai/cu/<cursor 模型名>
    api_base    = http://router9.litellm-product.svc.cluster.local:20128/v1
    api_key     = 9router 的 sk-*（从 pod sqlite `apiKeys` 现取，**全程不落盘不打印**）
    model_info.id = 9router/<cursor 模型名>
    单价 0/0（这条线不计费，计费在 Cursor 账号那头）

用法
----
    scripts/9router-cursor/add-9router-lanes.py --probe    # 只探针，不写任何东西
    scripts/9router-cursor/add-9router-lanes.py --apply    # 探针过的才建 lane（幂等：已存在的跳过）
    scripts/9router-cursor/add-9router-lanes.py --verify   # 建完走 litellm 真打验收

⛔ `--verify` **不用 master key 直打**：master key 打 cursor 线假绿假红都造得出来。
   它会 `/key/generate` 一把只含本批车道的临时 key、用它打、打完 `/key/delete` 删掉,
   走的是和真实用户完全同一条路。

回滚
----
    DELETE /model/delete  {"id": "9router/<cursor 模型名>"}
    本脚本 `--apply` 前会把**当前 cursor-fc-* 车道清单**打到 stdout 当快照。
"""

import argparse
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
NS = "litellm-product"
API_BASE = "http://router9.litellm-product.svc.cluster.local:20128/v1"

# lane 名 -> 9router/Cursor 侧的真实模型名。
# ⚠️ Cursor 的 grok 有两套前缀：`grok-4.7` 是裸名，4.6/4.5 全族带 `cursor-` 前缀。
#    照 AvailableModels 抄，别按规律猜——猜错的表现是 45s 死线，不是 404。
LANES = [
    ("cursor-fc-grok-4.7",           "grok-4.7"),
    ("cursor-fc-grok-4.6-low",       "cursor-grok-4.6-low"),
    ("cursor-fc-grok-4.6",           "cursor-grok-4.6-medium"),
    ("cursor-fc-grok-4.6-high",      "cursor-grok-4.6-high"),
    ("cursor-fc-grok-4.6-xhigh",     "cursor-grok-4.6-xhigh"),
    ("cursor-fc-grok-4.6-fast",      "cursor-grok-4.6-medium-fast"),
    ("cursor-fc-grok-4.5-low",       "cursor-grok-4.5-low"),
    ("cursor-fc-grok-4.5",           "cursor-grok-4.5-medium"),
    ("cursor-fc-grok-4.5-high",      "cursor-grok-4.5-high"),
    ("cursor-fc-grok-4.5-high-fast", "cursor-grok-4.5-high-fast"),
    ("cursor-fc-composer-2.5",       "composer-2.5"),
    ("cursor-fc-composer-2.5-fast",  "composer-2.5-fast"),
]


def _acct():
    """借 add-cursor-account.py 的 jms_bash / in_pod，别重写一遍 ssh 重试逻辑。"""
    spec = importlib.util.spec_from_file_location("acct", HERE / "add-cursor-account.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 步骤 1：在 pod 内直打 9router,判每个模型能不能出活 ──────────────────────
PROBE_SRC = r"""
const fs=require("fs"),initSql=require("/app/node_modules/sql.js");
const NAMES=__NAMES__;
(async()=>{
 const SQL=await initSql();
 const db=new SQL.Database(new Uint8Array(fs.readFileSync("/app/data/db/data.sqlite")));
 const key=db.exec("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1")[0].values[0][0];
 const nonce="P"+Math.random().toString(36).slice(2,8).toUpperCase();
 const run=async n=>{const t0=Date.now();
  try{
   const r=await fetch("http://127.0.0.1:20128/v1/chat/completions",{method:"POST",
     headers:{authorization:"Bearer "+key,"content-type":"application/json"},
     body:JSON.stringify({model:"cu/"+n,stream:false,messages:[
       {role:"user",content:"Reply with exactly this token and nothing else: "+nonce}]})});
   const txt=await r.text(); let out="";
   try{out=JSON.parse(txt).choices?.[0]?.message?.content||"";}catch(e){}
   // ok = 通道活(200 且有内容);echo = 模型听话。两个判据分开,别合成一个。
   return {model:n,http:r.status,ms:Date.now()-t0,ok:r.status===200&&out.trim().length>0,
           echo:out.includes(nonce),sample:out.slice(0,60)};
  }catch(e){return {model:n,http:0,ms:Date.now()-t0,ok:false,echo:false,sample:"ERR "+e.message}}};
 const out=[];
 for(let i=0;i<NAMES.length;i+=3)                       // 并发 3:再高会被上游当机器指纹
   out.push(...await Promise.all(NAMES.slice(i,i+3).map(run)));
 console.log("PROBE_JSON "+JSON.stringify(out));
})().catch(e=>{console.log("ERR "+e.message);process.exit(1);});
"""


def probe(mod, models):
    src = PROBE_SRC.replace("__NAMES__", json.dumps(models))
    out = mod.in_pod(src, timeout=900)
    for line in out.splitlines():
        if line.startswith("PROBE_JSON "):
            return json.loads(line[len("PROBE_JSON "):])
    raise SystemExit("probe produced no PROBE_JSON:\n" + out[-1500:])


# ── 步骤 2/3：198 侧的 litellm 操作 ────────────────────────────────────────
# 所有 curl 都在 198 上跑。master key / 9router key 只在远端 shell 变量里存在,
# 不回传、不打印。9router key 用 `kubectl exec` 取进变量(命令替换,不经 stdout)。
PREAMBLE = f"""set -eu
NS={NS}
MK=$(sudo kubectl -n $NS get secret litellm-secrets-r2 -o jsonpath='{{.data.LITELLM_MASTER_KEY}}' | base64 -d)
PORT=$(sudo kubectl -n $NS get svc litellm-proxy-nodeport -o jsonpath='{{.spec.ports[0].nodePort}}')
L="http://127.0.0.1:$PORT"
RP=$(sudo kubectl -n $NS get pods -l app=9router -o jsonpath='{{.items[0].metadata.name}}')
RK=$(sudo kubectl -n $NS exec "$RP" -- node -e '
const fs=require("fs"),initSql=require("/app/node_modules/sql.js");
initSql().then(S=>{{const d=new S.Database(new Uint8Array(fs.readFileSync("/app/data/db/data.sqlite")));
process.stdout.write(String(d.exec("SELECT key FROM apiKeys WHERE isActive=1 LIMIT 1")[0].values[0][0]));}});')
"""


def list_lanes(mod):
    out = mod.jms_bash(PREAMBLE + r"""
curl -s -H "Authorization: Bearer $MK" "$L/model/info" | python3 -c '
import json,sys
d=json.load(sys.stdin)["data"]
r=[(m["model_name"],m["litellm_params"].get("model"),m.get("model_info",{}).get("id"))
   for m in d if m["model_name"].startswith("cursor-fc-")]
print("LANES_JSON "+json.dumps(sorted(r)))'
""", timeout=600)
    for line in out.splitlines():
        if line.startswith("LANES_JSON "):
            return json.loads(line[len("LANES_JSON "):])
    raise SystemExit("could not read current lanes:\n" + out[-1500:])


def add_lanes(mod, lanes):
    """lanes = [(lane_name, cursor_model)]；api_key 在远端用 $RK 注入。"""
    body_tpl = {
        "model_name": None,
        "litellm_params": {"model": None, "api_base": API_BASE,
                           "api_key": "__RK__",
                           "input_cost_per_token": 0, "output_cost_per_token": 0},
        "model_info": {"id": None},
    }
    cmds = []
    for lane, cm in lanes:
        b = json.loads(json.dumps(body_tpl))
        b["model_name"] = lane
        b["litellm_params"]["model"] = f"openai/cu/{cm}"
        b["model_info"]["id"] = f"9router/{cm}"
        payload = json.dumps(b).replace('"__RK__"', '"\'"$RK"\'"')
        cmds.append(
            f'printf "%s " "{lane}"; '
            f'''curl -s -o /dev/null -w "%{{http_code}}\\n" -X POST "$L/model/new" '''
            f'-H "Authorization: Bearer $MK" -H "Content-Type: application/json" '
            f"-d '{payload}'")
    return mod.jms_bash(PREAMBLE + "\n".join(cmds) + "\n", timeout=900)


VERIFY = r"""
# 临时 key：只含本批车道,用完即删。⛔ 不用 master key —— 它打 cursor 线假绿假红都造。
KJ=$(curl -s -X POST "$L/key/generate" -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" -d '{"models":__MODELS__,"duration":"20m","key_alias":"__ALIAS__"}')
TK=$(echo "$KJ" | python3 -c 'import json,sys;print(json.load(sys.stdin)["key"])')
for m in __SHELL_LIST__; do
  S=$(curl -s -o /tmp/_v.json -w "%{http_code}" -X POST "$L/v1/chat/completions" \
      -H "Authorization: Bearer $TK" -H "Content-Type: application/json" \
      --max-time 120 -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"__NONCE__\"}]}")
  C=$(python3 -c '
import json,sys
try:
  d=json.load(open("/tmp/_v.json")); c=d["choices"][0]["message"]["content"] or ""
  print(("echo" if "__NONCE__".split()[-1] in c else "nocho")+" len="+str(len(c))+" "+c[:50].replace("\n"," "))
except Exception as e: print("noparse "+open("/tmp/_v.json").read()[:120].replace("\n"," "))')
  printf "%-32s http=%s %s\n" "$m" "$S" "$C"
done
rm -f /tmp/_v.json
curl -s -X POST "$L/key/delete" -H "Authorization: Bearer $MK" \
  -H "Content-Type: application/json" -d "{\"keys\":[\"$TK\"]}" -o /dev/null -w "key/delete %{http_code}\n"
"""


def verify(mod, lane_names):
    import random
    import string
    nonce = "V" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    script = (VERIFY
              .replace("__MODELS__", json.dumps(lane_names))
              .replace("__SHELL_LIST__", " ".join(lane_names))
              .replace("__ALIAS__", "tmp-9router-lane-verify")
              .replace("__NONCE__", f"Reply with exactly this token and nothing else: {nonce}"))
    return mod.jms_bash(PREAMBLE + script, timeout=1800)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--probe", action="store_true", help="只在 pod 内真打 9router,不写任何配置")
    g.add_argument("--apply", action="store_true", help="探针过的才建 lane（已存在的跳过）")
    g.add_argument("--verify", action="store_true", help="用临时 key 走 litellm 真打验收")
    ap.add_argument("--only", help="逗号分隔,只处理这些 lane 名")
    a = ap.parse_args()

    mod = _acct()
    lanes = LANES
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        lanes = [x for x in LANES if x[0] in want]
        if not lanes:
            raise SystemExit("--only matched no lane in LANES")

    if a.verify:
        print(verify(mod, [ln for ln, _ in lanes]))
        return

    results = probe(mod, [cm for _, cm in lanes])
    by_model = {r["model"]: r for r in results}
    for lane, cm in lanes:
        r = by_model[cm]
        print(f"  {'OK ' if r['ok'] else 'BAD'} {lane:<30} cu/{cm:<30} "
              f"http={r['http']} {r['ms']}ms echo={r['echo']} {r['sample']!r}")
    good = [(lane, cm) for lane, cm in lanes if by_model[cm]["ok"]]
    print(f"probe: {len(good)}/{len(lanes)} usable")
    if a.probe:
        return
    if not good:
        raise SystemExit("nothing passed the probe -- refusing to write any lane")

    existing = {n for n, _, _ in list_lanes(mod)}
    print("snapshot of cursor-fc-* lanes BEFORE (rollback reference):")
    for n in sorted(existing):
        print("   " + n)
    todo = [(lane, cm) for lane, cm in good if lane not in existing]
    print(f"to create: {len(todo)}  (skipping {len(good) - len(todo)} already present)")
    if not todo:
        return
    print(add_lanes(mod, todo))


if __name__ == "__main__":
    main()
