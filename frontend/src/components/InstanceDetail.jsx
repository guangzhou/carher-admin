import { useEffect, useState } from "react";
import { api } from "../api";
import {
  DEFAULT_LITELLM_ROUTE_POLICY,
  DEFAULT_PROVIDER,
  LITELLM_ROUTE_POLICY_OPTIONS,
  PROVIDER_MODELS,
  PROVIDER_OPTIONS,
  getLitellmRoutePolicyLabel,
  getModelAlias,
} from "../models";
import LogViewer from "./LogViewer";

export default function InstanceDetail({ id, onBack, onRefresh }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showLogs, setShowLogs] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({});
  const [actionLoading, setActionLoading] = useState(false);
  const [metrics, setMetrics] = useState(null);
  const [metricsHistory, setMetricsHistory] = useState([]);
  const [imageTags, setImageTags] = useState([]);

  const reload = () => {
    api.getInstance(id).then((d) => {
      setData(d);
      setEditForm((prev) => ({
        name: d.name || "",
        model: d.model_short || d.model || "",
        owner: d.owner || "",
        provider: d.provider || DEFAULT_PROVIDER,
        litellm_route_policy: d.litellm_route_policy || DEFAULT_LITELLM_ROUTE_POLICY,
        deploy_group: d.deploy_group || "stable",
        image: d.image || "",
        app_id: prev.app_id ?? d.app_id ?? "",
        app_secret: prev.app_secret ?? "",
      }));
    }).finally(() => setLoading(false));
    api.getInstanceMetrics(id).then(setMetrics).catch(() => {});
    api.getInstanceMetricsHistory(id, 24).then((r) => setMetricsHistory(r.data || [])).catch(() => {});
  };

  useEffect(() => { reload(); }, [id]);
  useEffect(() => { api.listImageTags().then((t) => setImageTags(Array.isArray(t) ? t : [])).catch(() => {}); }, []);

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
      if (editForm.provider !== (data.provider || DEFAULT_PROVIDER)) params.provider = editForm.provider;
      if (editForm.litellm_route_policy !== (data.litellm_route_policy || DEFAULT_LITELLM_ROUTE_POLICY)) {
        params.litellm_route_policy = editForm.litellm_route_policy;
      }
      if (editForm.deploy_group !== (data.deploy_group || "stable")) params.deploy_group = editForm.deploy_group;
      if (editForm.image && editForm.image !== (data.image || "")) params.image = editForm.image;
      if (editForm.app_id !== (data.app_id || "")) params.app_id = editForm.app_id;
      if (editForm.app_secret) params.app_secret = editForm.app_secret;

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
          {!editing && <button className="btn btn-ghost" onClick={() => {
            setEditForm({
              name: data.name || "",
              model: data.model_short || data.model || "",
              owner: data.owner || "",
              provider: data.provider || DEFAULT_PROVIDER,
              litellm_route_policy: data.litellm_route_policy || DEFAULT_LITELLM_ROUTE_POLICY,
              deploy_group: data.deploy_group || "stable",
              image: data.image || "",
              app_id: data.app_id || "",
              app_secret: "",
            });
            setEditing(true);
          }}>编辑配置</button>}
          <button className="btn btn-danger" onClick={() => doAction("delete")} disabled={actionLoading}>删除</button>
        </div>
      </div>

      {/* Resource metrics (real-time) */}
      {metrics && !metrics.error && (
        <div className="grid grid-cols-2 gap-4">
          <div className="card p-4 border border-emerald-600/20">
            <p className="text-xs text-gray-500 uppercase mb-1">CPU</p>
            <p className="text-2xl font-bold text-emerald-400">{(metrics.cpu_m / 1000).toFixed(3)}核</p>
            <p className="text-xs text-gray-600">{metrics.cpu_m}m (millicores)</p>
          </div>
          <div className="card p-4 border border-purple-600/20">
            <p className="text-xs text-gray-500 uppercase mb-1">内存</p>
            <p className="text-2xl font-bold text-purple-400">{formatMemory(metrics.memory_mi)}</p>
            <p className="text-xs text-gray-600">{Math.round(metrics.memory_mi)} MiB</p>
          </div>
        </div>
      )}

      {/* Metrics history mini chart (text-based sparkline) */}
      {metricsHistory.length > 1 && (
        <div className="card p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">24h 资源趋势</h3>
          <div className="grid grid-cols-2 gap-4">
            <Sparkline label="CPU (核)" data={metricsHistory} field="cpu_m" color="emerald" transform={(v) => v / 1000} unit="核" />
            <Sparkline label="内存 (Mi)" data={metricsHistory} field="memory_mi" color="purple" />
          </div>
        </div>
      )}

      {/* Info cards */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="card p-5 space-y-3">
          <h3 className="text-sm font-medium text-gray-400 mb-2">基本信息</h3>
          <InfoRow label="名字" value={data.name} />
          <InfoRow label="模型" value={getModelAlias(data.provider, data.model_short || data.model)} />
          <InfoRow label="Provider" value={data.provider} />
          {data.provider === "litellm" && (
            <InfoRow label="LiteLLM 路由" value={getLitellmRoutePolicyLabel(data.litellm_route_policy)} />
          )}
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
              <label className="block text-xs text-gray-500 mb-1">Provider</label>
              <select className="input w-full" value={editForm.provider} onChange={(e) => {
                const p = e.target.value;
                setField("provider", p);
                const models = PROVIDER_MODELS[p] || PROVIDER_MODELS.openrouter;
                if (!models.some((m) => m.value === editForm.model)) {
                  setField("model", models[0].value);
                }
              }}>
                {PROVIDER_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">模型</label>
              <select className="input w-full" value={editForm.model} onChange={(e) => setField("model", e.target.value)}>
                {(PROVIDER_MODELS[editForm.provider] || PROVIDER_MODELS.openrouter).map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
            </div>
            {editForm.provider === "litellm" && (
              <div>
                <label className="block text-xs text-gray-500 mb-1">LiteLLM 路由策略</label>
                <select className="input w-full" value={editForm.litellm_route_policy} onChange={(e) => setField("litellm_route_policy", e.target.value)}>
                  {LITELLM_ROUTE_POLICY_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </select>
              </div>
            )}
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
              <select className="input w-full" value={editForm.image} onChange={(e) => setField("image", e.target.value)}>
                {imageTags.includes(editForm.image) || !editForm.image ? null : <option value={editForm.image}>{editForm.image}</option>}
                {imageTags.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
          </div>
          <div className="border-t border-gray-700 pt-4 mt-2">
            <h4 className="text-xs text-gray-500 mb-3">飞书机器人配置</h4>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">App ID</label>
                <input className="input w-full font-mono text-xs" value={editForm.app_id} onChange={(e) => setField("app_id", e.target.value)} placeholder="cli_xxxxxxxx" />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">App Secret（留空则不修改）</label>
                <input className="input w-full font-mono text-xs" type="password" value={editForm.app_secret} onChange={(e) => setField("app_secret", e.target.value)} placeholder="不修改请留空" />
              </div>
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

function formatMemory(mi) {
  if (!mi) return "0";
  if (mi >= 1024) return `${(mi / 1024).toFixed(1)} Gi`;
  return `${Math.round(mi)} Mi`;
}

function Sparkline({ label, data, field, color, transform, unit }) {
  if (!data || data.length < 2) return null;
  const raw = data.map((d) => d[field] || 0);
  const values = transform ? raw.map(transform) : raw;
  const max = Math.max(...values, 1);
  const min = Math.min(...values);
  const latest = values[values.length - 1];
  const barCount = Math.min(values.length, 48);
  const step = Math.max(1, Math.floor(values.length / barCount));
  const sampled = [];
  for (let i = 0; i < values.length; i += step) {
    sampled.push(values[i]);
  }

  const colors = { emerald: "bg-emerald-500", purple: "bg-purple-500" };
  const textColors = { emerald: "text-emerald-400", purple: "text-purple-400" };
  const range = max > min ? max - min : 1;

  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-500">{label}</span>
        <span className={textColors[color]}>{unit ? `${latest.toFixed(3)}${unit}` : field === "memory_mi" ? formatMemory(latest) : `${latest.toFixed(1)}m`}</span>
      </div>
      <div className="flex items-end gap-px h-10">
        {sampled.map((v, i) => (
          <div
            key={i}
            className={`flex-1 ${colors[color]} rounded-t opacity-70`}
            style={{ height: `${Math.max((v - min) / range * 100, 4)}%` }}
          />
        ))}
      </div>
      <div className="flex justify-between text-xs text-gray-600 mt-0.5">
        <span>{data.length > 0 ? data[0].ts?.slice(11, 16) : ""}</span>
        <span>{data.length > 0 ? data[data.length - 1].ts?.slice(11, 16) : ""}</span>
      </div>
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
