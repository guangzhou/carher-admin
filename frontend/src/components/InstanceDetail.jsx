import { useEffect, useState } from "react";
import { api } from "../api";
import LogViewer from "./LogViewer";

export default function InstanceDetail({ id, onBack, onRefresh }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showLogs, setShowLogs] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editModel, setEditModel] = useState("");
  const [editOwner, setEditOwner] = useState("");
  const [actionLoading, setActionLoading] = useState(false);

  useEffect(() => {
    api.getInstance(id).then((d) => {
      setData(d);
      setEditModel(d.model_short || "");
      setEditOwner(d.owner || "");
    }).finally(() => setLoading(false));
  }, [id]);

  const doAction = async (action) => {
    const labels = { stop: "停止", start: "启动", restart: "重启", delete: "删除" };
    if (!confirm(`确认${labels[action]} carher-${id}？`)) return;
    setActionLoading(true);
    try {
      if (action === "stop") await api.stopInstance(id);
      else if (action === "start") await api.startInstance(id);
      else if (action === "restart") await api.restartInstance(id);
      else if (action === "delete") { await api.deleteInstance(id); onBack(); return; }
      setTimeout(() => {
        api.getInstance(id).then(setData);
        onRefresh();
      }, 3000);
    } catch (e) {
      alert(e.message);
    } finally {
      setActionLoading(false);
    }
  };

  const saveEdit = async () => {
    setActionLoading(true);
    try {
      const params = {};
      if (editModel !== data.model_short) params.model = editModel;
      if (editOwner !== data.owner) params.owner = editOwner;
      await api.updateInstance(id, params);
      setEditing(false);
      setTimeout(() => api.getInstance(id).then(setData), 3000);
    } catch (e) {
      alert(e.message);
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) return <div className="animate-pulse h-96 bg-gray-800 rounded-xl" />;
  if (!data) return <p className="text-gray-500">加载失败</p>;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button className="btn btn-ghost" onClick={onBack}>← 返回</button>
          <h2 className="text-xl font-semibold">carher-{id}</h2>
          <StatusBadge status={data.status} />
        </div>
        <div className="flex gap-2">
          {data.status === "Running" ? (
            <>
              <button className="btn btn-ghost" onClick={() => setShowLogs(!showLogs)}>{showLogs ? "隐藏日志" : "查看日志"}</button>
              <button className="btn btn-ghost" onClick={() => doAction("restart")} disabled={actionLoading}>重启</button>
              <button className="btn btn-danger" onClick={() => doAction("stop")} disabled={actionLoading}>停止</button>
            </>
          ) : (
            <button className="btn btn-success" onClick={() => doAction("start")} disabled={actionLoading}>启动</button>
          )}
          {!editing && <button className="btn btn-ghost" onClick={() => setEditing(true)}>编辑配置</button>}
          <button className="btn btn-danger" onClick={() => doAction("delete")} disabled={actionLoading}>删除</button>
        </div>
      </div>

      {/* Info cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-medium text-gray-400 mb-2">基本信息</h3>
          <InfoRow label="名字" value={data.name} />
          <InfoRow label="模型" value={data.model} />
          <InfoRow label="App ID" value={data.app_id} />
          <InfoRow label="Owner" value={data.owner} />
          <InfoRow label="knownBots" value={`${data.known_bots_count || 0} 个`} />
        </div>

        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-medium text-gray-400 mb-2">运行状态</h3>
          <InfoRow label="Pod IP" value={data.pod_ip} mono />
          <InfoRow label="节点" value={data.node} mono />
          <InfoRow label="重启次数" value={data.restarts} />
          <InfoRow label="运行时长" value={data.age} />
          <InfoRow label="PVC" value={data.pvc_status} />
          <InfoRow label="镜像" value={data.image} mono />
        </div>
      </div>

      {/* OAuth URL */}
      {data.oauth_url && (
        <div className="card p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-2">OAuth 回调 URL</h3>
          <div className="flex items-center gap-2">
            <code className="text-sm text-blue-400 bg-gray-800 px-3 py-1.5 rounded flex-1 font-mono">{data.oauth_url}</code>
            <button className="btn btn-ghost text-xs" onClick={() => navigator.clipboard.writeText(data.oauth_url)}>复制</button>
          </div>
        </div>
      )}

      {/* Edit form */}
      {editing && (
        <div className="card p-5 space-y-4 border-blue-600/30">
          <h3 className="text-sm font-medium text-blue-400">编辑配置</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">模型</label>
              <select className="input w-full" value={editModel} onChange={(e) => setEditModel(e.target.value)}>
                <option value="gpt">GPT-5.4</option>
                <option value="sonnet">Claude Sonnet 4.6</option>
                <option value="opus">Claude Opus 4.6</option>
                <option value="gemini">Gemini 3.1 Pro</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Owner (open_id)</label>
              <input className="input w-full" value={editOwner} onChange={(e) => setEditOwner(e.target.value)} placeholder="ou_xxx" />
            </div>
          </div>
          <div className="flex gap-2 justify-end">
            <button className="btn btn-ghost" onClick={() => setEditing(false)}>取消</button>
            <button className="btn btn-primary" onClick={saveEdit} disabled={actionLoading}>保存并重启</button>
          </div>
        </div>
      )}

      {/* Logs */}
      {showLogs && <LogViewer id={id} />}
    </div>
  );
}

function InfoRow({ label, value, mono }) {
  return (
    <div className="flex justify-between items-center text-sm">
      <span className="text-gray-500">{label}</span>
      <span className={`text-gray-200 ${mono ? "font-mono text-xs" : ""}`}>{value || "-"}</span>
    </div>
  );
}

function StatusBadge({ status }) {
  const cls = {
    Running: "bg-emerald-600/20 text-emerald-400",
    Stopped: "bg-yellow-600/20 text-yellow-400",
  }[status] || "bg-gray-600/20 text-gray-400";
  return <span className={`badge ${cls}`}>{status}</span>;
}
