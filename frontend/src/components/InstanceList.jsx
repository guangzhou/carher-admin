import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import InstanceDetail from "./InstanceDetail";
import LogViewer from "./LogViewer";

export default function InstanceList({ detailId, setDetailId }) {
  const [instances, setInstances] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(new Set());
  const [filter, setFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [showLogs, setShowLogs] = useState(null);
  const [batchLoading, setBatchLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api.listInstances().then(setInstances).finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const filtered = instances.filter((i) => {
    if (statusFilter === "running" && i.status !== "Running") return false;
    if (statusFilter === "stopped" && i.status !== "Stopped" && i.status !== "Paused") return false;
    if (statusFilter === "paused" && i.status !== "Paused") return false;
    if (filter) {
      const q = filter.toLowerCase();
      return (
        String(i.id).includes(q) ||
        (i.name || "").toLowerCase().includes(q) ||
        (i.model_short || "").toLowerCase().includes(q)
      );
    }
    return true;
  });

  const toggleSelect = (id) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    if (selected.size === filtered.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(filtered.map((i) => i.id)));
    }
  };

  const batchAction = async (action, params) => {
    if (!selected.size) return;
    const label = { stop: "停止", start: "启动", restart: "重启", delete: "删除" }[action];
    if (!confirm(`确认${label} ${selected.size} 个实例？`)) return;
    setBatchLoading(true);
    try {
      await api.batchAction([...selected], action, params);
      setSelected(new Set());
      setTimeout(load, 2000);
    } finally {
      setBatchLoading(false);
    }
  };

  const singleAction = async (id, action) => {
    try {
      if (action === "stop") await api.stopInstance(id);
      else if (action === "start") await api.startInstance(id);
      else if (action === "restart") await api.restartInstance(id);
      setTimeout(load, 2000);
    } catch (e) {
      alert(`操作失败: ${e.message}`);
    }
  };

  if (detailId != null) {
    return <InstanceDetail id={detailId} onBack={() => setDetailId(null)} onRefresh={load} />;
  }

  if (showLogs != null) {
    return (
      <div>
        <button className="btn btn-ghost mb-4" onClick={() => setShowLogs(null)}>← 返回列表</button>
        <LogViewer id={showLogs} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <input
          className="input w-64"
          placeholder="搜索 ID / 名字 / 模型..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <select className="input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="all">全部状态</option>
          <option value="running">运行中</option>
          <option value="stopped">已停止</option>
          <option value="paused">已暂停</option>
        </select>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          {loading ? "加载中..." : "刷新"}
        </button>

        {selected.size > 0 && (
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-sm text-gray-400">已选 {selected.size} 个</span>
            <button className="btn btn-success" onClick={() => batchAction("start")} disabled={batchLoading}>批量启动</button>
            <button className="btn btn-ghost" onClick={() => batchAction("restart")} disabled={batchLoading}>批量重启</button>
            <button className="btn btn-danger" onClick={() => batchAction("stop")} disabled={batchLoading}>批量停止</button>
            <button className="btn btn-danger" onClick={() => batchAction("delete")} disabled={batchLoading}>批量删除</button>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500 text-left">
              <th className="p-3 w-10">
                <input type="checkbox" checked={selected.size === filtered.length && filtered.length > 0} onChange={toggleAll}
                  className="rounded border-gray-600" />
              </th>
              <th className="p-3">ID</th>
              <th className="p-3">名字</th>
              <th className="p-3">模型</th>
              <th className="p-3">状态</th>
              <th className="p-3">Pod IP</th>
              <th className="p-3">运行时长</th>
              <th className="p-3">同步</th>
              <th className="p-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((inst) => (
              <tr
                key={inst.id}
                className="border-b border-gray-800/50 hover:bg-gray-800/30 transition-colors"
              >
                <td className="p-3">
                  <input type="checkbox" checked={selected.has(inst.id)} onChange={() => toggleSelect(inst.id)}
                    className="rounded border-gray-600" />
                </td>
                <td className="p-3">
                  <button className="text-blue-400 hover:underline font-mono" onClick={() => setDetailId(inst.id)}>
                    {inst.id}
                  </button>
                </td>
                <td className="p-3 text-gray-200">{inst.name || "-"}</td>
                <td className="p-3">
                  <span className="badge bg-gray-800 text-gray-300">{inst.model_short || "-"}</span>
                </td>
                <td className="p-3">
                  <StatusBadge status={inst.status} />
                </td>
                <td className="p-3 text-gray-400 font-mono text-xs">{inst.pod_ip || "-"}</td>
                <td className="p-3 text-gray-400 text-xs">{inst.age || "-"}</td>
                <td className="p-3">
                  {inst.sync_status === "operator" ? (
                    <span className="text-blue-400 text-xs" title="Operator 管理">⚙</span>
                  ) : inst.sync_status === "synced" ? (
                    <span className="text-emerald-400 text-xs">●</span>
                  ) : inst.sync_status === "pending" ? (
                    <span className="text-yellow-400 text-xs" title="ConfigMap 同步待重试">◐</span>
                  ) : (
                    <span className="text-gray-600 text-xs">-</span>
                  )}
                </td>
                <td className="p-3 text-right">
                  <div className="flex items-center justify-end gap-1">
                    {inst.status === "Running" ? (
                      <>
                        <button className="btn btn-ghost text-xs" onClick={() => setShowLogs(inst.id)}>日志</button>
                        <button className="btn btn-ghost text-xs" onClick={() => singleAction(inst.id, "restart")}>重启</button>
                        <button className="btn btn-danger text-xs" onClick={() => singleAction(inst.id, "stop")}>停止</button>
                      </>
                    ) : (
                      <button className="btn btn-success text-xs" onClick={() => singleAction(inst.id, "start")}>
                        {inst.status === "Paused" ? "恢复" : "启动"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan="9" className="p-8 text-center text-gray-500">
                  {loading ? "加载中..." : "没有找到实例"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-gray-600">共 {instances.length} 个实例，显示 {filtered.length} 个</p>
    </div>
  );
}

function StatusBadge({ status }) {
  const cls = {
    Running: "bg-emerald-600/20 text-emerald-400",
    Stopped: "bg-yellow-600/20 text-yellow-400",
    Paused: "bg-orange-600/20 text-orange-400",
    Pending: "bg-blue-600/20 text-blue-400",
    Failed: "bg-red-600/20 text-red-400",
    Unknown: "bg-gray-600/20 text-gray-400",
  }[status] || "bg-gray-600/20 text-gray-400";

  return <span className={`badge ${cls}`}>{status}</span>;
}
