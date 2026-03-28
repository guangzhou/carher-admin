import { useEffect, useState, useCallback } from "react";
import { api } from "../api";

const GROUPS = ["canary", "early", "stable"];
const GROUP_LABELS = { canary: "金丝雀", early: "先行者", stable: "稳定" };
const GROUP_COLORS = {
  canary: "bg-orange-600/20 text-orange-400 border-orange-600/30",
  early: "bg-blue-600/20 text-blue-400 border-blue-600/30",
  stable: "bg-gray-600/20 text-gray-300 border-gray-600/30",
};

export default function DeployPage() {
  const [status, setStatus] = useState(null);
  const [history, setHistory] = useState([]);
  const [instances, setInstances] = useState([]);
  const [imageTag, setImageTag] = useState("");
  const [loading, setLoading] = useState("");

  const load = useCallback(() => {
    api.getDeployStatus().then(setStatus);
    api.getDeployHistory().then(setHistory);
    api.listInstances().then(setInstances);
  }, []);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh during active deploy
  useEffect(() => {
    if (!status?.active) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [status?.active, load]);

  const startDeploy = async () => {
    if (!imageTag) return alert("请输入镜像 tag");
    if (!confirm(`确认部署 ${imageTag}？将按 金丝雀 → 先行者 → 稳定 顺序灰度发布`)) return;
    setLoading("deploy");
    try {
      const r = await api.startDeploy(imageTag);
      if (r.error) alert(r.error);
      else load();
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
      setTimeout(load, 1000);
    } finally {
      setLoading("");
    }
  };

  const setGroup = async (uid, group) => {
    await api.setDeployGroup(uid, group);
    load();
  };

  const deploy = status?.deploy;
  const isActive = status?.active;

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
                {deploy.current_wave && <span className="ml-2">当前波次: {GROUP_LABELS[deploy.current_wave]}</span>}
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
          <div className="flex gap-4">
            {GROUPS.map((g) => {
              const count = status.waves?.[g] || 0;
              const isCurrent = deploy.current_wave === g;
              const isDone = GROUPS.indexOf(g) < GROUPS.indexOf(deploy.current_wave);
              return (
                <div key={g} className={`flex-1 p-3 rounded-lg border ${isCurrent ? "border-blue-500 bg-blue-600/10" : isDone ? "border-emerald-600/30 bg-emerald-600/10" : "border-gray-700"}`}>
                  <p className="text-xs text-gray-500">{GROUP_LABELS[g]}</p>
                  <p className="text-lg font-bold">{count}</p>
                  <p className="text-xs">{isDone ? "✅ 完成" : isCurrent ? "🔄 进行中" : "⏳ 等待"}</p>
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
              placeholder="镜像 tag (如 v20260329)"
              value={imageTag}
              onChange={(e) => setImageTag(e.target.value)}
            />
            <button className="btn btn-primary px-6" onClick={startDeploy} disabled={!!loading}>
              {loading === "deploy" ? "部署中..." : "开始灰度部署"}
            </button>
          </div>
          <p className="text-xs text-gray-600">
            部署顺序: 金丝雀({status?.waves?.canary || 0}) → 先行者({status?.waves?.early || 0}) → 稳定({status?.waves?.stable || 0})，
            每批 {10} 个，每批完成后自动健康检查
          </p>
        </div>
      )}

      {/* Deploy group management */}
      <div className="card p-5 space-y-3">
        <h3 className="text-sm font-medium text-gray-400">实例分组管理</h3>
        <p className="text-xs text-gray-600">拖动实例到不同分组，金丝雀组最先更新，稳定组最后更新</p>

        <div className="grid grid-cols-3 gap-4">
          {GROUPS.map((g) => {
            const groupInstances = instances.filter((i) => (i.deploy_group || "stable") === g && i.status !== "Deleted");
            return (
              <div key={g} className={`rounded-lg border p-3 ${GROUP_COLORS[g]}`}>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm font-medium">{GROUP_LABELS[g]}</span>
                  <span className="text-xs">{groupInstances.length}</span>
                </div>
                <div className="space-y-1 max-h-48 overflow-auto">
                  {groupInstances.map((inst) => (
                    <div key={inst.id} className="flex items-center justify-between text-xs bg-gray-900/50 rounded px-2 py-1">
                      <span>#{inst.id} {inst.name}</span>
                      <select
                        className="bg-transparent text-xs border-none cursor-pointer"
                        value={g}
                        onChange={(e) => setGroup(inst.id, e.target.value)}
                      >
                        {GROUPS.map((opt) => <option key={opt} value={opt}>{GROUP_LABELS[opt]}</option>)}
                      </select>
                    </div>
                  ))}
                  {groupInstances.length === 0 && <p className="text-xs text-gray-600">空</p>}
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
