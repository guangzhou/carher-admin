import { useEffect, useState, useCallback } from "react";
import { api } from "../api";

const PALETTE = [
  "bg-orange-600/20 text-orange-400 border-orange-600/30",
  "bg-blue-600/20 text-blue-400 border-blue-600/30",
  "bg-emerald-600/20 text-emerald-400 border-emerald-600/30",
  "bg-purple-600/20 text-purple-400 border-purple-600/30",
  "bg-pink-600/20 text-pink-400 border-pink-600/30",
  "bg-cyan-600/20 text-cyan-400 border-cyan-600/30",
  "bg-yellow-600/20 text-yellow-400 border-yellow-600/30",
  "bg-gray-600/20 text-gray-300 border-gray-600/30",
];

function groupColor(idx) {
  return PALETTE[idx % PALETTE.length];
}

const MODE_LABELS = {
  normal: "灰度部署",
  fast: "紧急全量",
  "canary-only": "仅首组",
};

function modeLabel(mode) {
  if (!mode) return "-";
  if (mode.startsWith("group:")) return `指定: ${mode.slice(6)}`;
  return MODE_LABELS[mode] || mode;
}

export default function DeployPage() {
  const [status, setStatus] = useState(null);
  const [history, setHistory] = useState([]);
  const [instances, setInstances] = useState([]);
  const [deployGroups, setDeployGroups] = useState([]);
  const [branchRules, setBranchRules] = useState([]);
  const [imageTag, setImageTag] = useState("");
  const [deployMode, setDeployMode] = useState("normal");
  const [targetGroup, setTargetGroup] = useState("");
  const [forceDeploy, setForceDeploy] = useState(false);
  const [loading, setLoading] = useState("");

  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupPriority, setNewGroupPriority] = useState(50);
  const [newGroupDesc, setNewGroupDesc] = useState("");
  const [showNewGroup, setShowNewGroup] = useState(false);

  const [editingGroup, setEditingGroup] = useState(null);
  const [editPriority, setEditPriority] = useState(0);
  const [editDesc, setEditDesc] = useState("");

  // Branch rule form
  const [showNewRule, setShowNewRule] = useState(false);
  const [ruleForm, setRuleForm] = useState({ pattern: "", deploy_mode: "normal", target_group: "", auto_deploy: true, description: "" });
  const [editingRule, setEditingRule] = useState(null);
  const [testBranch, setTestBranch] = useState("");
  const [testResult, setTestResult] = useState(null);

  // Build trigger
  const [buildRepo, setBuildRepo] = useState("guangzhou/CarHer");
  const [buildBranch, setBuildBranch] = useState("main");

  const loadFull = useCallback(() => {
    api.getDeployStatus().then(setStatus);
    api.getDeployHistory().then(setHistory);
    api.listInstances().then(setInstances);
    api.listDeployGroups().then(setDeployGroups);
    api.listBranchRules().then(setBranchRules).catch(() => {});
  }, []);

  const loadStatusOnly = useCallback(() => {
    api.getDeployStatus().then(setStatus);
    api.listDeployGroups().then(setDeployGroups);
  }, []);

  useEffect(() => { loadFull(); }, [loadFull]);

  useEffect(() => {
    if (!status?.active) return;
    const t = setInterval(loadStatusOnly, 5000);
    return () => clearInterval(t);
  }, [status?.active, loadStatusOnly]);

  const groupNames = deployGroups.map((g) => g.name);

  // Collect unique image tags from history + running instances for autocomplete
  const knownTags = [...new Set([
    ...history.map((d) => d.image_tag).filter(Boolean),
    ...instances.map((i) => i.image || i.image_tag).filter(Boolean),
  ])];

  // Collect branch names from rules (strip glob chars) + common defaults
  const knownBranches = [...new Set([
    "main",
    ...branchRules.map((r) => r.pattern.replace(/\*/g, "")).filter((b) => b && !b.endsWith("/")),
    ...history.map((d) => d.branch).filter(Boolean),
  ])];

  const startDeploy = async () => {
    if (!imageTag) return alert("请输入镜像 tag");
    let mode = deployMode;
    if (mode === "group" && targetGroup) {
      mode = `group:${targetGroup}`;
    } else if (mode === "group") {
      return alert("请选择目标分组");
    }
    const label = modeLabel(mode);
    if (!confirm(`确认 [${label}] 部署 ${imageTag}？${forceDeploy ? "\n(强制模式)" : ""}`)) return;
    setLoading("deploy");
    try {
      const r = await api.startDeploy(imageTag, mode, forceDeploy);
      if (r.error) alert(r.error);
      else if (r.status === "already_deployed") alert("该镜像已部署完成。如需重新部署，请勾选「强制部署」");
      else loadFull();
    } catch (e) {
      alert(`部署失败: ${e.message}`);
    } finally {
      setLoading("");
    }
  };

  const doAction = async (action) => {
    const labels = { continue: "继续", rollback: "回滚", abort: "中止" };
    if (!confirm(`确认${labels[action]}？`)) return;
    setLoading(action);
    try {
      if (action === "continue") await api.continueDeploy();
      else if (action === "rollback") await api.rollbackDeploy();
      else if (action === "abort") await api.abortDeploy();
      setTimeout(loadFull, 1000);
    } finally {
      setLoading("");
    }
  };

  const setGroup = async (uid, group) => {
    try {
      await api.setDeployGroup(uid, group);
      loadFull();
    } catch (e) {
      alert(`分组变更失败: ${e.message}`);
    }
  };

  const createGroup = async () => {
    if (!newGroupName.trim()) return;
    try {
      await api.createDeployGroup(newGroupName.trim().toLowerCase(), newGroupPriority, newGroupDesc);
      setNewGroupName(""); setNewGroupPriority(50); setNewGroupDesc(""); setShowNewGroup(false);
      loadFull();
    } catch (e) { alert(e.message); }
  };

  const startEditGroup = (g) => { setEditingGroup(g.name); setEditPriority(g.priority); setEditDesc(g.description || ""); };
  const saveEditGroup = async () => {
    if (!editingGroup) return;
    try {
      await api.updateDeployGroup(editingGroup, { priority: editPriority, description: editDesc });
      setEditingGroup(null); loadFull();
    } catch (e) { alert(e.message); }
  };

  const deleteGroup = async (name) => {
    if (!confirm(`删除分组 "${name}"？该组实例将移入 stable 组`)) return;
    try { await api.deleteDeployGroup(name); loadFull(); } catch (e) { alert(e.message); }
  };

  // Branch rules handlers
  const saveRule = async () => {
    if (!ruleForm.pattern.trim()) return alert("请输入分支模式");
    try {
      if (editingRule) {
        await api.updateBranchRule(editingRule, ruleForm);
      } else {
        await api.createBranchRule(ruleForm);
      }
      setRuleForm({ pattern: "", deploy_mode: "normal", target_group: "", auto_deploy: true, description: "" });
      setEditingRule(null); setShowNewRule(false); loadFull();
    } catch (e) { alert(e.message); }
  };

  const deleteRule = async (id) => {
    if (!confirm("删除此规则？")) return;
    try { await api.deleteBranchRule(id); loadFull(); } catch (e) { alert(e.message); }
  };

  const doTestBranch = async () => {
    if (!testBranch) return;
    try { const r = await api.testBranchRule(testBranch); setTestResult(r); } catch (e) { alert(e.message); }
  };

  const triggerBuild = async () => {
    if (!confirm(`确认触发构建？\n仓库: ${buildRepo}\n分支: ${buildBranch}`)) return;
    setLoading("build");
    try {
      const r = await api.triggerBuild({ repo: buildRepo, branch: buildBranch });
      alert(r.status === "triggered" ? "构建已触发，请在 GitHub Actions 查看进度" : JSON.stringify(r));
    } catch (e) { alert(`触发失败: ${e.message}`); }
    finally { setLoading(""); }
  };

  const deploy = status?.deploy;
  const isActive = status?.active;
  const waveOrder = status?.wave_order || groupNames;

  const waveDesc = waveOrder.length > 0
    ? waveOrder.map((g) => {
        const dg = deployGroups.find((x) => x.name === g);
        return `${dg?.description || g}(${status?.waves?.[g] || 0})`;
      }).join(" → ")
    : "";

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">部署管理</h2>

      {/* Active deploy */}
      {isActive && deploy && (
        <div className="card p-5 border border-blue-600/30 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-sm font-medium text-blue-400">
                Deploy #{deploy.id} — {deploy.image_tag}
              </h3>
              <p className="text-xs text-gray-500 mt-1">
                状态: <StatusBadge status={deploy.status} />
                {deploy.mode && deploy.mode !== "normal" && (
                  <span className="ml-2 badge bg-gray-600/20 text-gray-300">{modeLabel(deploy.mode)}</span>
                )}
                {deploy.branch && <span className="ml-2 font-mono text-blue-400">{deploy.branch}</span>}
                {deploy.current_wave && <span className="ml-2">当前波次: {deploy.current_wave}</span>}
              </p>
            </div>
            <div className="flex gap-2">
              {deploy.status === "paused" && (
                <>
                  <button className="btn btn-success" onClick={() => doAction("continue")} disabled={!!loading}>继续</button>
                  <button className="btn btn-danger" onClick={() => doAction("rollback")} disabled={!!loading}>回滚</button>
                </>
              )}
              {["pending", "canary", "rolling"].includes(deploy.status) && (
                <button className="btn btn-danger" onClick={() => doAction("abort")} disabled={!!loading}>中止</button>
              )}
            </div>
          </div>

          <div>
            <div className="flex justify-between text-xs text-gray-500 mb-1">
              <span>{deploy.done}/{deploy.total} 完成</span>
              <span>{status.progress_pct}%</span>
            </div>
            <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
              <div className="bg-blue-600 h-full rounded-full transition-all duration-500" style={{ width: `${status.progress_pct}%` }} />
            </div>
            {deploy.failed > 0 && <p className="text-xs text-red-400 mt-1">{deploy.failed} 个失败</p>}
          </div>

          <div className="flex gap-4 overflow-x-auto">
            {waveOrder.map((g) => {
              const count = status.waves?.[g] || 0;
              const isCurrent = deploy.current_wave === g;
              const currentIdx = deploy.current_wave ? waveOrder.indexOf(deploy.current_wave) : -1;
              const isDone = currentIdx >= 0 && waveOrder.indexOf(g) < currentIdx;
              return (
                <div key={g} className={`flex-1 min-w-[120px] p-3 rounded-lg border ${isCurrent ? "border-blue-500 bg-blue-600/10" : isDone ? "border-emerald-600/30 bg-emerald-600/10" : "border-gray-700"}`}>
                  <p className="text-xs text-gray-500">{g}</p>
                  <p className="text-lg font-bold">{count}</p>
                  <p className="text-xs">{isDone ? "Done" : isCurrent ? "Rolling" : "Waiting"}</p>
                </div>
              );
            })}
          </div>

          {deploy.error && !deploy.error.startsWith("mode:") && (
            <div className="text-sm text-red-400 bg-red-600/10 p-3 rounded">{deploy.error}</div>
          )}
        </div>
      )}

      {/* Start new deploy / Trigger build */}
      {!isActive && (
        <div className="card p-5 space-y-4">
          <h3 className="text-sm font-medium text-gray-400">发起新部署</h3>
          <div className="flex gap-2 flex-wrap items-end">
            <input className="input flex-1 min-w-[200px]" placeholder="镜像 tag" list="tag-options"
              value={imageTag} onChange={(e) => setImageTag(e.target.value)} />
            <datalist id="tag-options">
              {knownTags.map((t) => <option key={t} value={t} />)}
            </datalist>
            <select className="input w-36" value={deployMode}
              onChange={(e) => { setDeployMode(e.target.value); if (e.target.value !== "group") setTargetGroup(""); }}>
              <option value="normal">灰度部署</option>
              <option value="fast">紧急全量</option>
              <option value="canary-only">仅首组</option>
              <option value="group">指定分组</option>
            </select>
            {deployMode === "group" && (
              <select className="input w-32" value={targetGroup} onChange={(e) => setTargetGroup(e.target.value)}>
                <option value="">选择分组</option>
                {groupNames.map((g) => <option key={g} value={g}>{g}</option>)}
              </select>
            )}
            <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer">
              <input type="checkbox" checked={forceDeploy} onChange={(e) => setForceDeploy(e.target.checked)} className="accent-blue-500" />
              强制
            </label>
            <button className="btn btn-primary px-6" onClick={startDeploy} disabled={!!loading}>
              {loading === "deploy" ? "部署中..." : "开始部署"}
            </button>
          </div>

          {/* Trigger GitHub build */}
          <div className="border-t border-gray-800 pt-3">
            <p className="text-xs text-gray-500 mb-2">或从 GitHub 触发构建（构建完成后自动部署）</p>
            <div className="flex gap-2 items-end">
              <input className="input w-48" placeholder="仓库 owner/name" list="repo-options" value={buildRepo} onChange={(e) => setBuildRepo(e.target.value)} />
              <datalist id="repo-options">
                <option value="guangzhou/CarHer" />
                <option value="guangzhou/carher-admin" />
              </datalist>
              <input className="input w-36" placeholder="分支" list="branch-options" value={buildBranch} onChange={(e) => setBuildBranch(e.target.value)} />
              <datalist id="branch-options">
                {knownBranches.map((b) => <option key={b} value={b} />)}
              </datalist>
              <button className="btn btn-sm" onClick={triggerBuild} disabled={loading === "build"}>
                {loading === "build" ? "触发中..." : "触发构建"}
              </button>
            </div>
          </div>

          <div className="text-xs text-gray-600 space-y-1">
            {waveDesc && <p><strong>灰度部署:</strong> {waveDesc}，每批后自动健康检查</p>}
            <p><strong>紧急全量:</strong> 跳过灰度，所有实例直接更新</p>
            <p><strong>仅首组 / 指定分组:</strong> 精确控制部署范围</p>
          </div>
        </div>
      )}

      {/* Branch rules */}
      <div className="card p-5 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium text-gray-400">分支规则 (CI/CD)</h3>
            <p className="text-xs text-gray-600">定义分支 → 部署模式的映射，webhook 自动匹配</p>
          </div>
          <button className="btn btn-sm" onClick={() => { setShowNewRule(!showNewRule); setEditingRule(null); }}>
            {showNewRule ? "取消" : "+ 新增规则"}
          </button>
        </div>

        {showNewRule && (
          <div className="bg-gray-800/50 p-3 rounded space-y-2">
            <div className="grid grid-cols-5 gap-2">
              <div>
                <label className="text-[10px] text-gray-500">分支模式</label>
                <input className="input w-full text-xs" placeholder="main, hotfix/*, feature/*"
                  value={ruleForm.pattern} onChange={(e) => setRuleForm({ ...ruleForm, pattern: e.target.value })} />
              </div>
              <div>
                <label className="text-[10px] text-gray-500">部署模式</label>
                <select className="input w-full text-xs" value={ruleForm.deploy_mode}
                  onChange={(e) => setRuleForm({ ...ruleForm, deploy_mode: e.target.value })}>
                  <option value="normal">灰度</option>
                  <option value="fast">全量</option>
                  <option value="canary-only">仅首组</option>
                  <option value="group:canary">指定: canary</option>
                  {groupNames.filter(g => g !== "canary").map(g => <option key={g} value={`group:${g}`}>指定: {g}</option>)}
                </select>
              </div>
              <div>
                <label className="text-[10px] text-gray-500">描述</label>
                <input className="input w-full text-xs" placeholder="规则描述"
                  value={ruleForm.description} onChange={(e) => setRuleForm({ ...ruleForm, description: e.target.value })} />
              </div>
              <div>
                <label className="text-[10px] text-gray-500">自动部署</label>
                <select className="input w-full text-xs" value={ruleForm.auto_deploy ? "true" : "false"}
                  onChange={(e) => setRuleForm({ ...ruleForm, auto_deploy: e.target.value === "true" })}>
                  <option value="true">是 - 自动</option>
                  <option value="false">否 - 仅构建</option>
                </select>
              </div>
              <div className="flex items-end">
                <button className="btn btn-primary btn-sm w-full" onClick={saveRule}>{editingRule ? "保存" : "创建"}</button>
              </div>
            </div>
          </div>
        )}

        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500">
              <th className="p-2 text-left">分支模式</th>
              <th className="p-2 text-left">部署模式</th>
              <th className="p-2 text-left">自动部署</th>
              <th className="p-2 text-left">描述</th>
              <th className="p-2 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {branchRules.map((r) => (
              <tr key={r.id} className="border-b border-gray-800/50">
                <td className="p-2 font-mono text-blue-400">{r.pattern}</td>
                <td className="p-2">{modeLabel(r.deploy_mode)}</td>
                <td className="p-2">{r.auto_deploy ? <span className="text-emerald-400">自动</span> : <span className="text-yellow-400">手动</span>}</td>
                <td className="p-2 text-gray-500">{r.description}</td>
                <td className="p-2 text-right">
                  <button className="text-xs text-gray-500 hover:text-white mr-2" onClick={() => {
                    setEditingRule(r.id); setShowNewRule(true);
                    setRuleForm({ pattern: r.pattern, deploy_mode: r.deploy_mode, target_group: r.target_group || "", auto_deploy: !!r.auto_deploy, description: r.description || "" });
                  }}>编辑</button>
                  <button className="text-xs text-red-400/60 hover:text-red-400" onClick={() => deleteRule(r.id)}>删除</button>
                </td>
              </tr>
            ))}
            {branchRules.length === 0 && (
              <tr><td colSpan="5" className="p-4 text-center text-gray-600">暂无规则</td></tr>
            )}
          </tbody>
        </table>

        {/* Branch test */}
        <div className="flex gap-2 items-center">
          <input className="input w-48 text-xs" placeholder="输入分支名测试匹配" value={testBranch} onChange={(e) => setTestBranch(e.target.value)} />
          <button className="btn btn-sm text-xs" onClick={doTestBranch}>测试</button>
          {testResult && (
            <span className="text-xs">
              {testResult.matched_rule
                ? <span className="text-emerald-400">匹配: {testResult.matched_rule.pattern} → {modeLabel(testResult.matched_rule.deploy_mode)}</span>
                : <span className="text-yellow-400">无匹配规则，将使用默认模式 (normal)</span>}
            </span>
          )}
        </div>
      </div>

      {/* Deploy group management */}
      <div className="card p-5 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-medium text-gray-400">实例分组管理</h3>
            <p className="text-xs text-gray-600">按 priority 从小到大部署，可自定义分组</p>
          </div>
          <button className="btn btn-sm" onClick={() => setShowNewGroup(!showNewGroup)}>
            {showNewGroup ? "取消" : "+ 新建分组"}
          </button>
        </div>

        {showNewGroup && (
          <div className="flex gap-2 items-end bg-gray-800/50 p-3 rounded">
            <div className="flex-1">
              <label className="text-xs text-gray-500">名称</label>
              <input className="input w-full" placeholder="如 vip, test, team-a" value={newGroupName} onChange={(e) => setNewGroupName(e.target.value)} />
            </div>
            <div className="w-24">
              <label className="text-xs text-gray-500">优先级</label>
              <input type="number" className="input w-full" value={newGroupPriority} onChange={(e) => setNewGroupPriority(+e.target.value)} />
            </div>
            <div className="flex-1">
              <label className="text-xs text-gray-500">描述</label>
              <input className="input w-full" placeholder="描述" value={newGroupDesc} onChange={(e) => setNewGroupDesc(e.target.value)} />
            </div>
            <button className="btn btn-primary" onClick={createGroup}>创建</button>
          </div>
        )}

        <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${Math.min(deployGroups.length || 3, 4)}, minmax(0, 1fr))` }}>
          {deployGroups.map((g, idx) => {
            const groupInstances = instances.filter((i) => (i.deploy_group || "stable") === g.name && i.status !== "Deleted");
            const isEditing = editingGroup === g.name;
            return (
              <div key={g.name} className={`rounded-lg border p-3 ${groupColor(idx)}`}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium">{g.name}</span>
                  <div className="flex items-center gap-1">
                    {!isEditing && (
                      <>
                        <span className="text-xs opacity-60">P{g.priority}</span>
                        <button className="text-xs opacity-40 hover:opacity-100 ml-1" onClick={() => startEditGroup(g)} title="编辑分组">✎</button>
                        {g.name !== "stable" && (
                          <button className="text-xs opacity-40 hover:opacity-100 ml-1" onClick={() => deleteGroup(g.name)} title="删除分组">x</button>
                        )}
                      </>
                    )}
                  </div>
                </div>
                {isEditing ? (
                  <div className="space-y-2 mb-2">
                    <div className="flex gap-2 items-center">
                      <label className="text-[10px] text-gray-500 w-12">优先级</label>
                      <input type="number" className="input w-16 text-xs py-1" value={editPriority} onChange={(e) => setEditPriority(+e.target.value)} />
                    </div>
                    <div className="flex gap-2 items-center">
                      <label className="text-[10px] text-gray-500 w-12">描述</label>
                      <input className="input flex-1 text-xs py-1" value={editDesc} onChange={(e) => setEditDesc(e.target.value)} />
                    </div>
                    <div className="flex gap-1">
                      <button className="btn btn-sm btn-primary text-[10px] py-0.5 px-2" onClick={saveEditGroup}>保存</button>
                      <button className="btn btn-sm text-[10px] py-0.5 px-2" onClick={() => setEditingGroup(null)}>取消</button>
                    </div>
                  </div>
                ) : (
                  g.description && <p className="text-xs opacity-60 mb-2">{g.description}</p>
                )}
                <div className="text-xs mb-1">{groupInstances.length} 个实例</div>
                <div className="space-y-1 max-h-48 overflow-auto">
                  {groupInstances.map((inst) => (
                    <div key={inst.id} className="flex items-center justify-between text-xs bg-gray-900/50 rounded px-2 py-1">
                      <span className="flex items-center gap-2">
                        <span>#{inst.id} {inst.name}</span>
                        {inst.image && <span className="text-gray-600 font-mono text-[10px]">{inst.image}</span>}
                      </span>
                      <select className="bg-transparent text-xs border-none cursor-pointer" value={g.name}
                        onChange={(e) => setGroup(inst.id, e.target.value)}>
                        {groupNames.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
                      </select>
                    </div>
                  ))}
                  {groupInstances.length === 0 && (
                    <p className="text-[10px] text-gray-600 italic py-1">暂无实例，可从其他分组拖入</p>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Deploy history with CI metadata */}
      <div className="card p-5 space-y-3">
        <h3 className="text-sm font-medium text-gray-400">部署历史</h3>
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500">
              <th className="p-2 text-left">#</th>
              <th className="p-2 text-left">镜像</th>
              <th className="p-2 text-left">分支</th>
              <th className="p-2 text-left">模式</th>
              <th className="p-2 text-left">状态</th>
              <th className="p-2 text-left">进度</th>
              <th className="p-2 text-left">时间</th>
            </tr>
          </thead>
          <tbody>
            {history.map((d) => (
              <tr key={d.id} className="border-b border-gray-800/50 group">
                <td className="p-2 font-mono">{d.id}</td>
                <td className="p-2 font-mono">{d.image_tag}</td>
                <td className="p-2">
                  {d.branch ? (
                    <span className="flex items-center gap-1">
                      <span className="text-blue-400 font-mono">{d.branch}</span>
                      {d.commit_sha && <span className="text-gray-600 font-mono">{d.commit_sha.slice(0, 7)}</span>}
                    </span>
                  ) : <span className="text-gray-600">-</span>}
                </td>
                <td className="p-2">{modeLabel(d.mode)}</td>
                <td className="p-2"><StatusBadge status={d.status} /></td>
                <td className="p-2">{d.done}/{d.total}{d.failed > 0 && <span className="text-red-400 ml-1">({d.failed}F)</span>}</td>
                <td className="p-2 text-gray-500">{d.created_at}</td>
              </tr>
            ))}
            {history.length === 0 && (
              <tr><td colSpan="7" className="p-4 text-center text-gray-600">暂无部署记录</td></tr>
            )}
          </tbody>
        </table>

        {/* Expanded detail for selected deploy (show commit msg, author, run_url) */}
        {history.length > 0 && history[0].commit_msg && (
          <div className="text-xs text-gray-500 bg-gray-800/30 p-3 rounded space-y-1">
            <p className="font-medium text-gray-400">最近部署详情</p>
            {history[0].author && <p>作者: {history[0].author}</p>}
            {history[0].commit_msg && <p>提交: {history[0].commit_msg}</p>}
            {history[0].run_url && <p>构建: <a href={history[0].run_url} target="_blank" rel="noreferrer" className="text-blue-400 hover:underline">{history[0].run_url}</a></p>}
          </div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }) {
  const cls = {
    complete: "bg-emerald-600/20 text-emerald-400",
    canary: "bg-orange-600/20 text-orange-400",
    rolling: "bg-blue-600/20 text-blue-400",
    pending: "bg-gray-600/20 text-gray-400",
    paused: "bg-yellow-600/20 text-yellow-400",
    failed: "bg-red-600/20 text-red-400",
    rolled_back: "bg-purple-600/20 text-purple-400",
  }[status] || "bg-gray-600/20 text-gray-400";
  return <span className={`badge ${cls}`}>{status}</span>;
}
