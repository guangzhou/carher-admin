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

export default function DeployPage() {
  const [status, setStatus] = useState(null);
  const [history, setHistory] = useState([]);
  const [instances, setInstances] = useState([]);
  const [deployGroups, setDeployGroups] = useState([]);
  const [imageTag, setImageTag] = useState("");
  const [deployMode, setDeployMode] = useState("normal");
  const [loading, setLoading] = useState("");

  // New group form
  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupPriority, setNewGroupPriority] = useState(50);
  const [newGroupDesc, setNewGroupDesc] = useState("");
  const [showNewGroup, setShowNewGroup] = useState(false);

  const loadFull = useCallback(() => {
    api.getDeployStatus().then(setStatus);
    api.getDeployHistory().then(setHistory);
    api.listInstances().then(setInstances);
    api.listDeployGroups().then(setDeployGroups);
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

  const MODE_LABELS = { normal: "灰度部署", fast: "紧急全量", "canary-only": "仅首组" };
  const startDeploy = async () => {
    if (!imageTag) return alert("请输入镜像 tag");
    const modeLabel = MODE_LABELS[deployMode] || deployMode;
    if (!confirm(`确认 [${modeLabel}] 部署 ${imageTag}？`)) return;
    setLoading("deploy");
    try {
      const r = await api.startDeploy(imageTag, deployMode);
      if (r.error) alert(r.error);
      else if (r.status === "already_deployed") alert("该镜像已部署完成，无需重复部署");
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
      setNewGroupName("");
      setNewGroupPriority(50);
      setNewGroupDesc("");
      setShowNewGroup(false);
      loadFull();
    } catch (e) {
      alert(e.message);
    }
  };

  const deleteGroup = async (name) => {
    if (!confirm(`删除分组 "${name}"？该组实例将移入 stable 组`)) return;
    try {
      await api.deleteDeployGroup(name);
      loadFull();
    } catch (e) {
      alert(e.message);
    }
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

          {/* Progress bar */}
          <div>
            <div className="flex justify-between text-xs text-gray-500 mb-1">
              <span>{deploy.done}/{deploy.total} 完成</span>
              <span>{status.progress_pct}%</span>
            </div>
            <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
              <div
                className="bg-blue-600 h-full rounded-full transition-all duration-500"
                style={{ width: `${status.progress_pct}%` }}
              />
            </div>
            {deploy.failed > 0 && (
              <p className="text-xs text-red-400 mt-1">{deploy.failed} 个失败</p>
            )}
          </div>

          {/* Wave status */}
          <div className="flex gap-4 overflow-x-auto">
            {waveOrder.map((g) => {
              const count = status.waves?.[g] || 0;
              const isCurrent = deploy.current_wave === g;
              const currentIdx = deploy.current_wave ? waveOrder.indexOf(deploy.current_wave) : -1;
              const thisIdx = waveOrder.indexOf(g);
              const isDone = currentIdx >= 0 && thisIdx < currentIdx;
              return (
                <div key={g} className={`flex-1 min-w-[120px] p-3 rounded-lg border ${isCurrent ? "border-blue-500 bg-blue-600/10" : isDone ? "border-emerald-600/30 bg-emerald-600/10" : "border-gray-700"}`}>
                  <p className="text-xs text-gray-500">{g}</p>
                  <p className="text-lg font-bold">{count}</p>
                  <p className="text-xs">{isDone ? "Done" : isCurrent ? "Rolling" : "Waiting"}</p>
                </div>
              );
            })}
          </div>

          {deploy.error && (
            <div className="text-sm text-red-400 bg-red-600/10 p-3 rounded">{deploy.error}</div>
          )}
        </div>
      )}

      {/* Start new deploy */}
      {!isActive && (
        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-medium text-gray-400">发起新部署</h3>
          <div className="flex gap-2">
            <input
              className="input flex-1"
              placeholder="镜像 tag (如 v20260329-abc1234)"
              value={imageTag}
              onChange={(e) => setImageTag(e.target.value)}
            />
            <select
              className="input w-36"
              value={deployMode}
              onChange={(e) => setDeployMode(e.target.value)}
            >
              <option value="normal">灰度部署</option>
              <option value="fast">紧急全量</option>
              <option value="canary-only">仅首组</option>
            </select>
            <button className="btn btn-primary px-6" onClick={startDeploy} disabled={!!loading}>
              {loading === "deploy" ? "部署中..." : "开始部署"}
            </button>
          </div>
          <div className="text-xs text-gray-600 space-y-1">
            {waveDesc && <p><strong>灰度部署:</strong> {waveDesc}，每批后自动健康检查</p>}
            <p><strong>紧急全量:</strong> 跳过灰度，所有实例直接更新（用于 hotfix）</p>
            <p><strong>仅首组:</strong> 只更新优先级最高的分组，手动确认后再继续</p>
          </div>
        </div>
      )}

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
            return (
              <div key={g.name} className={`rounded-lg border p-3 ${groupColor(idx)}`}>
                <div className="flex items-center justify-between mb-1">
                  <span className="text-sm font-medium">{g.name}</span>
                  <div className="flex items-center gap-1">
                    <span className="text-xs opacity-60">P{g.priority}</span>
                    {g.name !== "stable" && (
                      <button className="text-xs opacity-40 hover:opacity-100 ml-1" onClick={() => deleteGroup(g.name)} title="删除分组">x</button>
                    )}
                  </div>
                </div>
                {g.description && <p className="text-xs opacity-60 mb-2">{g.description}</p>}
                <div className="text-xs mb-1">{groupInstances.length} 个实例</div>
                <div className="space-y-1 max-h-48 overflow-auto">
                  {groupInstances.map((inst) => (
                    <div key={inst.id} className="flex items-center justify-between text-xs bg-gray-900/50 rounded px-2 py-1">
                      <span>#{inst.id} {inst.name}</span>
                      <select
                        className="bg-transparent text-xs border-none cursor-pointer"
                        value={g.name}
                        onChange={(e) => setGroup(inst.id, e.target.value)}
                      >
                        {groupNames.map((opt) => <option key={opt} value={opt}>{opt}</option>)}
                      </select>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Deploy history */}
      <div className="card p-5 space-y-3">
        <h3 className="text-sm font-medium text-gray-400">部署历史</h3>
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500">
              <th className="p-2 text-left">#</th>
              <th className="p-2 text-left">镜像</th>
              <th className="p-2 text-left">状态</th>
              <th className="p-2 text-left">进度</th>
              <th className="p-2 text-left">时间</th>
            </tr>
          </thead>
          <tbody>
            {history.map((d) => (
              <tr key={d.id} className="border-b border-gray-800/50">
                <td className="p-2 font-mono">{d.id}</td>
                <td className="p-2 font-mono">{d.image_tag}</td>
                <td className="p-2"><StatusBadge status={d.status} /></td>
                <td className="p-2">{d.done}/{d.total}{d.failed > 0 && <span className="text-red-400 ml-1">({d.failed} failed)</span>}</td>
                <td className="p-2 text-gray-500">{d.created_at}</td>
              </tr>
            ))}
            {history.length === 0 && (
              <tr><td colSpan="5" className="p-4 text-center text-gray-600">暂无部署记录</td></tr>
            )}
          </tbody>
        </table>
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
