#!/usr/bin/env bash
# 看 Claude 桌面 App 现在的模型菜单从哪来(10-27 沉淀,只读)。
#
# App 的菜单不读 ~/.claude/settings.json:App 配置(configLibrary/<appliedId>.json)写了 inferenceModels ⇒ 菜单 = 这份;
# 没写 ⇒ App 启动时自己拉一次网关 /v1/models(只在内存里)。判断「App 里看不到某模型」先看这两样,别先猜「App 太旧」。
# 用法: app-model-menu.sh            (mac;Windows 看 %LOCALAPPDATA%\Claude-3p 同名文件)
set -euo pipefail
D="$HOME/Library/Application Support/Claude-3p"; L="$D/configLibrary"
[ -f "$L/_meta.json" ] || { echo "没有 App 三方配置($L)⇒ 没跑过一键配置"; exit 1; }
python3 - "$L" <<'PY'
import json,sys
L=sys.argv[1]; m=json.load(open(L+"/_meta.json")); aid=m.get("appliedId")
names={e.get("id"):e.get("name") for e in m.get("entries",[])}
c=json.load(open(f"{L}/{aid}.json"))
k=c.get("inferenceGatewayApiKey","")
print(f"当前生效配置 : {names.get(aid)} ({aid[:8]}…)  provider={c.get('inferenceProvider')}  key末4位={k[-4:] if k else '-'}")
im=c.get("inferenceModels")
print("菜单来源     :", f"inferenceModels({len(im)} 个) = {im}" if im else "App 自己拉网关 /v1/models(没写 inferenceModels)")
print("外网放行     :", c.get("coworkEgressAllowedHosts"))
PY
LOG="$HOME/Library/Logs/Claude-3p/main.log"
[ -f "$LOG" ] && { echo "最近一次发现 :"; grep -h "Model discovery" "$LOG" | tail -1 | cut -c1-200; }
echo "⚠ 改了配置要完全退出 App(Cmd+Q)再开,App 才重新读"
