# 实践案例：Cursor 内置浏览器是怎么在网页里执行 JS 的（2026-10-20）

> 目标：`/Applications/Cursor.app`（Cursor 3.24.12，Electron），只读本机安装包，不运行、不修改、不注入。
> 工具：REA 6.7.0 `analyze-javascript-application`（静态，不执行目标代码）。完整 Evidence：`~/codes/rea-deps/case-cursor/analyze.json`（210 MB）。

## 结论

Cursor 的 Agent 能操控内置浏览器，靠的是一条**4 跳链路**：workbench 里的命令 `cursor.browserView.executeJavaScriptInIsolatedWorld`
→ 宿主对 `<webview>` 发 IPC `cursor-isolated-eval`（带 requestId）→ webview 的 preload 把代码包成 `eval(...)`，
在**隔离世界**（不和网页自己的 JS 共享全局）里执行 → 结果经 `sendToHost("cursor-isolated-eval-result")` 回传，按 requestId 结算 Promise。
REA 一次静态分析就把链路两端（preload 里的通道、bridge、窗口）定位到了行列号；中间「谁调用」这一跳需要人顺着 REA 给的通道名去 grep。

## 第 1 步：选对目录

| 尝试 | 结果 |
|---|---|
| `Contents/Resources/app/node_modules.asar` | 只有 28 字节，是占位，没东西 |
| `Contents/Resources/app/out/`（121 MB，46 个 JS） | ✅ 主进程 `main.js` + workbench + preload 都在这 |

经验：Electron 应用先 `ls Contents/Resources/app/`，看 `package.json` 的 `main` 指向哪，就分析那个目录。

## 第 2 步：跑 REA

```bash
cli-anything-rea run analyze-js /Applications/Cursor.app/Contents/Resources/app/out --record-integrity
# 等价于：source ~/codes/rea-deps/rea.env && node ~/codes/rea/scripts/rea.mjs analyze-javascript-application <out> --integrity-policy record-and-continue --format json
```

321 秒，峰值内存 1.6 GB，exit 0。摘要：

| 项 | 数 | 说明 |
|---|---|---|
| 解析的 JS | 45 / 跳过 4 | 跳过的是 `extensionHostProcess.js`、`extensionHostWorkerMain.js`、`pdf.worker`、`xai-commerce-prepaid.js`：关系展开超出结果预算，进了 limitations |
| BrowserWindow | 7 | 全在 `main.js`（69/77/144/310/311 行） |
| IPC 操作 | 21（字面通道 8，动态 13） | 动态的是 VS Code 通用 `ipcRenderer.send(e)` 包装，REA **不按字面相似度乱配对** |
| contextBridge API | 4 | `vscode`（主世界）、`cursorPasskey`（主世界）、`cursorBrowser`（**隔离世界**）… |
| utility 进程 | 1 | `main.js:65` |
| semantic graph 节点 | **100000（正好撞上限）** | ⚠ 语义图被截断，coverage=partial。别拿它当完整调用图 |

## 第 3 步：从 REA 结果里挑线索

Electron 边界节点里，Cursor 自己（非 VS Code 继承）的字面通道全集中在一个文件：

```
vs/workbench/contrib/composer/browser/preload-webview-browser.js
  :253  renderer on           cursor-isolated-eval          ← 收到要执行的代码
  :253  renderer send-to-host cursor-isolated-eval-result   ← 回传结果
  :310  renderer send-to-host open-url-side-group / keyboard-shortcut / did-keydown / did-pointerdown
  :253  contextBridge cursorBrowser  world=isolated  members=[forwardKeydown, send]   (inferred/high)
  :253  contextBridge cursorPasskey  world=main      members=[notify]                 (observed/exact)
```

`composer` = Cursor 的 Agent 面板；一个叫 `eval` 的 IPC 通道是最值得追的。

## 第 4 步：按 REA 给的行列号读原文（验证 REA 没说错）

`preload-webview-browser.js:253`（sha256 `2c21aaf3a03a…`，与 REA Evidence 里的 artifact_sha256 一致）：

```js
a.on("cursor-isolated-eval", async (e, t, o) => {           // t = requestId, o = 代码字符串
  if (typeof t != "string" || typeof o != "string") return;
  const n = `(() => { try { const __result = eval(${JSON.stringify(o)}); ... })()`;
  const r = await s.executeJavaScriptInIsolatedWorld(u, [{ code: n }]);   // u = 隔离世界 id
  ...
  a.sendToHost("cursor-isolated-eval-result", i);           // i = {requestId, result | error}
});
l.exposeInMainWorld("cursorPasskey", { notify: e => { m(e) && a.sendToHost(e) } });
l.exposeInIsolatedWorld(u, "cursorBrowser", v);
```

REA 报的列 1764 正落在 `a.sendToHost("cursor-isolated-eval-result", i)` 上。✅

## 第 5 步：顺通道名找另一端（REA 没做的这一跳，人来做）

`grep -l cursor-isolated-eval out/` 命中 `workbench.desktop.main.js` 与 `workbench.glass.main.js`：

```js
// 宿主侧：发起
async executeJavaScriptInIsolatedWorld(e) {
  const n = zn();                                   // requestId
  ... this.pendingIsolatedEvals.set(n, s), t.send("cursor-isolated-eval", n, e) ...
}
// 宿主侧：收结果
if (N === "cursor-isolated-eval-result") { this.settleIsolatedEval(O[0]); return }
// 暴露成命令（Agent/扩展可调）
ki.registerCommand("cursor.browserView.executeJavaScriptInIsolatedWorld", async (e, t, n, i) => { ...r.executeJavaScriptInIsolatedWorld(t) })
```

调用方还有两个：浏览器编辑器的覆盖层 `window.__cursorOverlayCommand?.(...)`（选元素高亮、链接预览），
和响应式模式读 `window.visualViewport.scale`。

## 学到的设计（可以抄的）

1. **隔离世界执行**：自动化代码跑在 `executeJavaScriptInIsolatedWorld` 里，网页拿不到、也改不了 Agent 注入的全局；网页的 JS 也伪造不了回传。
2. **requestId + pending 表**：把单向 IPC 变成 Promise；超时/销毁时 `deleteAndLeak` 清掉。
3. **主世界只暴露一个 `notify`**：`cursorPasskey.notify` 经 `m(e)` 白名单过滤后才 `sendToHost`，而 `m` 只放行两个值
   `passkey-request-stalled` / `passkey-hybrid-unsupported`（同文件 offset ≈9309；它是 WebAuthn polyfill 的回报口）。网页能调的面最小。
4. **键盘/鼠标事件只认 `isTrusted`**：`keydown` 里 `if (!e.isTrusted) return;`，`pointerdown` 里 `e.isTrusted && e.pointerType==="mouse"`，网页用脚本合成的事件触发不了 Cursor 快捷键。

## REA 在这个案例里的表现

| 好 | 不足 |
|---|---|
| 不跑目标、5 分钟出全局 Electron 边界图，行列号精确 | 语义图在 10 万节点处截断；4 个大文件被跳过（最大的 extensionHost 正好是插件宿主） |
| 每条带 observed/inferred，`cursorBrowser` 正确标为 inferred（成员来自变量 `v`） | 动态通道（13 个）不解析，VS Code 风格的通用 IPC 包装全成 `channel_expression: "e"` |
| 字面通道名直接给出「该 grep 什么」 | 跨文件「谁调用了它」没自动连上，要人顺通道名补 |
| Evidence 带 sha256，能复核是同一份文件 | 210 MB 原始输出，不过滤没法读——用 harness 摘要或 jq |

## 复现

```bash
cli-anything-rea run analyze-js /Applications/Cursor.app/Contents/Resources/app/out --record-integrity
python3 - <<'EOF'   # 列出 Electron 边界：kind / 文件:行 / 通道或 API
import json,glob,os
f=sorted(glob.glob(os.path.expanduser('~/codes/rea-deps/runs/*analyze-js.json')))[-1]
g=json.load(open(f))['normalized_result']['graph']
for n in g['nodes']:
    if n['kind'] in ('ipc-channel','context-bridge-api','browser-window','electron-utility'):
        for o in n['observations']:
            v=(o['evidence'].get('location') or {}).get('value') or {}
            p=o['properties']
            print(n['kind'], f"{v.get('source')}:{(v.get('start') or {}).get('line')}", p.get('channel') or p.get('api_key') or '')
EOF
```
