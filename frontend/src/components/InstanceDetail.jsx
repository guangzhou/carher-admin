import { useEffect, useState } from "react";
import { api } from "../api";
import LogViewer from "./LogViewer";

export default function InstanceDetail({ id, onBack, onRefresh }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showLogs, setShowLogs] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({});
  const [actionLoading, setActionLoading] = useState(false);

  const reload = () => {
    api.getInstance(id).then((d) => {
      setData(d);
      setEditForm({
        name: d.name || "",
        model: d.model_short || d.model || "",
        owner: d.owner || "",
        provider: d.provider || "openrouter",
        deploy_group: d.deploy_group || "stable",
        image: d.image || "",
      });
    }).finally(() => setLoading(false));
  };

  useEffect(() => { reload(); }, [id]);

  const doAction = async (action) => {
    const labels = { stop: "停止", start: "启动", restart: "重启", delete: "删除" };
    if (!confirm(`确认${labels[action]} carher-${id}？`)) return;
    setActionLoading(true);
    try {
      if (action === "stop") await api.stopInstance(id);
      else if (action === "start") await api.startInstance(id);
      else if (action === "restart") await api.restartInstance(id);
      else if (action === "delete") { await api.deleteInstance(id); onBack(); return; }
      setTimeout(() => { reload(); onRefresh(); }, 3000);
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
      if (editForm.name !== (data.name || "")) params.name = editForm.name;
      if (editForm.model !== (data.model_short || data.model || "")) params.model = editForm.model;
      if (editForm.owner !== (data.owner || "")) params.owner = editForm.owner;
      if (editForm.provider !== (data.provider || "openrouter")) params.provider = editForm.provider;
      if (editForm.deploy_group !== (data.deploy_group || "stable")) params.deploy_group = editForm.deploy_group;
      if (editForm.image && editForm.image !== (data.image || "")) params.image = editForm.image;

      if (Object.keys(params).length === 0) {
        setEditing(false);
        return;
      }

      await api.updateInstance(id, params);
      setEditing(false);
      setTimeout(() => { reload(); onRefresh(); }, 3000);
    } catch (e) {
      alert(e.message);
    } finally {
      setActionLoading(false);
    }
  };

  const setField = (k, v) => setEditForm((f) => ({ ...f, [k]: v }));

  if (loading) return <div className="animate-pulse h-96 bg-gray-800 rounded-xl" />;
  if (!data) return <p className="text-gray-500">加载失败</p>;

  const isCRD = data.managed_by === "operator";

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button className="btn btn-ghost" onClick={onBack}>← 返回</button>
          <h2 className="text-xl font-semibold">carher-{id}</h2>
          <StatusBadge status={data.paused ? "Paused" : data.status} />
          {isCRD && <span className="text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">Operator</span>}
        </div>
        <div className="flex gap-2">
          {data.status === "Running" && !data.paused ? (
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
          <InfoRow label="Provider" value={data.provider} />
          <InfoRow label="App ID" value={data.app_id} mono />
          <InfoRow label="Bot Open ID" value={data.bot_open_id} mono />
          <InfoRow label="Owner" value={data.owner} mono />
          <InfoRow label="部署组" value={data.deploy_group} />
        </div>

        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-medium text-gray-400 mb-2">运行状态</h3>
          <InfoRow label="Pod IP" value={data.pod_ip} mono />
          <InfoRow label="节点" value={data.node} mono />
          <InfoRow label="重启次数" value={data.restarts} />
          <InfoRow label="PVC" value={data.pvc_status} />
          <InfoRow label="镜像" value={data.image} mono />
          <InfoRow label="飞书 WS" value={data.feishu_ws} />
          <InfoRow label="Config Hash" value={data.config_hash} mono />
          {data.last_health_check && <InfoRow label="最后检查" value={data.last_health_check} />}
          {data.message && <InfoRow label="消息" value={data.message} />}
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
          <h3 className="text-sm font-medium text-blue-400">编辑配置{isCRD ? "（Operator 将自动 reconcile）" : ""}</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs text-gray-500 mb-1">名字</label>
              <input className="input w-full" value={editForm.name} onChange={(e) => setField("name", e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">模型</label>
              <select className="input w-full" value={editForm.model} onChange={(e) => setField("model", e.target.value)}>
                <option value="gpt">GPT-5.4</option>
                <option value="sonnet">Claude Sonnet 4.6</option>
                <option value="opus">Claude Opus 4.6</option>
                <option value="gemini">Gemini 3.1 Pro</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Provider</label>
              <select className="input w-full" value={editForm.provider} onChange={(e) => setField("provider", e.target.value)}>
                <option value="openrouter">OpenRouter</option>
                <option value="anthropic">Anthropic (直连)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">部署组</label>
              <input className="input w-full" value={editForm.deploy_group} onChange={(e) => setField("deploy_group", e.target.value)} placeholder="stable / canary / vip" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Owner (open_id)</label>
              <input className="input w-full" value={editForm.owner} onChange={(e) => setField("owner", e.target.value)} placeholder="ou_xxx" />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">镜像版本</label>
              <input className="input w-full" value={editForm.image} onChange={(e) => setField("image", e.target.value)} placeholder="v20260329" />
            </div>
          </div>
          <div className="flex gap-2 justify-end">
            <button className="btn btn-ghost" onClick={() => setEditing(false)}>取消</button>
            <button className="btn btn-primary" onClick={saveEdit} disabled={actionLoading}>
              {actionLoading ? "保存中..." : "保存"}
            </button>
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
    Paused: "bg-purple-600/20 text-purple-400",
    Failed: "bg-red-600/20 text-red-400",
  }[status] || "bg-gray-600/20 text-gray-400";
  return <span className={`badge ${cls}`}>{status}</span>;
}
