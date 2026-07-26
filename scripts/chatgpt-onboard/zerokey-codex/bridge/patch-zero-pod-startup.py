import json,subprocess,sys
APP=sys.argv[1]
DRY = "--apply" not in sys.argv
def kc(*a):
    return subprocess.run(["sudo","kubectl","-n","litellm-product"]+list(a),
                          capture_output=True,text=True,input="Hn8#mKLp3QxZ\n")
r=kc("get","deploy",APP,"-o","json")
d=json.loads(r.stdout)
c=d["spec"]["template"]["spec"]["containers"][0]
args=c.get("args") or []
# args may be ["-c", script] or [script]
idx = 1 if (len(args)>1 and args[0]=="-c") else 0
script=args[idx]
if "cp /patch/responses.js" in script:
    print("  %s already patched, skip"%APP); sys.exit(0)
NEED=["cp /patch/web-tools.js /app/routes/web-tools.js",
      "cp /patch/raw.js /app/routes/raw.js",
      "cp /patch/responses.js /app/routes/responses.js"]
lines=script.split("\n")
# insert the new cp lines immediately BEFORE the exec line, so any init gating
# (e.g. zero-81 waits for users.json) is preserved verbatim.
out=[]
for ln in lines:
    if ln.startswith("exec ") :
        for n in NEED:
            if n not in script: out.append(n)
    out.append(ln)
new="\n".join(out)
print("  %s new script:\n%s"%(APP,"\n".join("      "+l for l in new.split("\n"))))
if DRY:
    print("  (dry-run, not applied)"); sys.exit(0)
args[idx]=new
patch={"spec":{"template":{"spec":{"containers":[{"name":c["name"],"args":args}]}}}}
r2=kc("patch","deploy",APP,"--type","strategic","-p",json.dumps(patch))
print("  patch:",(r2.stdout or r2.stderr).strip()[:120])
