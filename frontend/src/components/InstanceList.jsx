import { useEffect, useState, useCallback } from "react";
import { api } from "../api";
import {
  ALL_MODELS,
  DEFAULT_LITELLM_ROUTE_POLICY,
  DEFAULT_PROVIDER,
  LITELLM_ROUTE_POLICY_OPTIONS,
  PROVIDER_MODELS,
  PROVIDER_OPTIONS,
  getModelAlias,
} from "../models";
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
  const [podMetrics, setPodMetrics] = useState({});
  const [showBatchEdit, setShowBatchEdit] = useState(false);
  const [deployGroups, setDeployGroups] = useState([]);
  const [imageTags, setImageTags] = useState([]);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([api.listInstances(), api.getMetricsPods()])
      .then(([inst, m]) => { setInstances(inst); setPodMetrics(m || {}); })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    api.listDeployGroups().then((g) => setDeployGroups(Array.isArray(g) ? g : [])).catch(() => {});
    api.listImageTags().then((t) => setImageTags(Array.isArray(t) ? t : [])).catch(() => {});
  }, []);

  const filtered = instances.filter((i) => {
    if (statusFilter === "running" && i.status !== "Running") return false;
    if (statusFilter === "stopped" && i.status !== "Stopped" && i.status !== "Paused") return false;
    if (statusFilter === "paused" && i.status !== "Paused") return false;
    if (filter) {
      const q = filter.toLowerCase();
      return (
        String(i.id).includes(q) ||
        (i.name || "").toLowerCase().includes(q) ||
        (i.model_short || "").toLowerCase().includes(q) ||
        getModelAlias(i.provider, i.model_short).toLowerCase().includes(q) ||
        (i.image || "").toLowerCase().includes(q) ||
        (i.deploy_group || "").toLowerCase().includes(q)
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
    const label = { stop: "停止", start: "启动", restart: "重启", delete: "删除", update: "修改" }[action];
    if (action !== "update" && !confirm(`确认${label} ${selected.size} 个实例？`)) return;
    setBatchLoading(true);
    try {
      const res = await api.batchAction([...selected], action, params);
      const errors = (res?.results || []).filter((r) => r.error);
      if (errors.length > 0) {
        alert(`${errors.length} 个实例操作失败:\n${errors.map((e) => `#${e.id}: ${e.error}`).join("\n")}`);
      }
      setSelected(new Set());
      setTimeout(load, 2000);
    } catch (e) {
      alert(`批量操作失败: ${e.message}`);
    } finally {
      setBatchLoading(false);
    }
  };

  const handleBatchUpdate = async (params) => {
    setShowBatchEdit(false);
    await batchAction("update", params);
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
          placeholder="搜索 ID / 名字 / 模型 / 镜像..."
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
            <button className="btn btn-primary" onClick={() => setShowBatchEdit(true)} disabled={batchLoading}>批量修改</button>
            <button className="btn btn-success" onClick={() => batchAction("start")} disabled={batchLoading}>批量启动</button>
            <button className="btn btn-ghost" onClick={() => batchAction("restart")} disabled={batchLoading}>批量重启</button>
            <button className="btn btn-danger" onClick={() => batchAction("stop")} disabled={batchLoading}>批量停止</button>
            <button className="btn btn-danger" onClick={() => batchAction("delete")} disabled={batchLoading}>批量删除</button>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500 text-left whitespace-nowrap">
              <th className="p-3 w-10">
                <input type="checkbox" checked={selected.size === filtered.length && filtered.length > 0} onChange={toggleAll}
                  className="rounded border-gray-600" />
              </th>
              <th className="p-3">ID</th>
              <th className="p-3">名字</th>
              <th className="p-3">模型</th>
              <th className="p-3">镜像</th>
              <th className="p-3">灰度组</th>
              <th className="p-3">状态</th>
              <th className="p-3 text-right">CPU</th>
              <th className="p-3 text-right">内存</th>
              <th className="p-3">节点</th>
              <th className="p-3 text-center">同步</th>
              <th className="p-3 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((inst) => {
              const m = podMetrics[inst.id] || {};
              return (
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
                  <td className="p-3 text-gray-200 truncate">{inst.name || "-"}</td>
                  <td className="p-3">
                    <span className="badge bg-gray-800 text-gray-300 whitespace-nowrap">{getModelAlias(inst.provider, inst.model_short)}</span>
                  </td>
                  <td className="p-3 truncate" title={inst.image}>
                    <span className="font-mono text-xs text-gray-400">{shortImage(inst.image)}</span>
                  </td>
                  <td className="p-3">
                    <DeployGroupBadge group={inst.deploy_group} />
                  </td>
                  <td className="p-3">
                    <StatusBadge status={inst.status} />
                  </td>
                  <td className="p-3 text-right font-mono text-xs text-emerald-400 whitespace-nowrap">
                    {m.cpu_m != null ? formatCpu(m.cpu_m) : "-"}
                  </td>
                  <td className="p-3 text-right font-mono text-xs text-purple-400 whitespace-nowrap">
                    {m.memory_mi != null ? formatMem(m.memory_mi) : "-"}
                  </td>
                  <td className="p-3 text-gray-400 font-mono text-xs truncate">{shortNode(inst.node)}</td>
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
              );
            })}
            {filtered.length === 0 && (
              <tr>
                <td colSpan="12" className="p-8 text-center text-gray-500">
                  {loading ? "加载中..." : "没有找到实例"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="text-xs text-gray-600">共 {instances.length} 个实例，显示 {filtered.length} 个</p>

      {showBatchEdit && (
        <BatchEditModal
          count={selected.size}
          deployGroups={deployGroups}
          imageTags={imageTags}
          onSubmit={handleBatchUpdate}
          onClose={() => setShowBatchEdit(false)}
        />
      )}
    </div>
  );
}

function formatCpu(millicores) {
  if (millicores == null) return "-";
  const cores = millicores / 1000;
  if (cores >= 1) return `${cores.toFixed(1)}核`;
  return `${cores.toFixed(3)}核`;
}

function formatMem(mi) {
  if (!mi) return "0";
  if (mi >= 1024) return `${(mi / 1024).toFixed(1)}G`;
  return `${Math.round(mi)}M`;
}

function shortImage(tag) {
  if (!tag) return "-";
  if (tag.startsWith("dev-")) return tag.slice(0, 12);
  return tag;
}

function shortNode(name) {
  if (!name) return "-";
  const m = name.match(/(\d+\.\d+\.\d+\.\d+)/);
  return m ? m[1] : name.slice(-12);
}

function BatchEditModal({ count, deployGroups, imageTags, onSubmit, onClose }) {
  const [enableProvider, setEnableProvider] = useState(false);
  const [enableModel, setEnableModel] = useState(false);
  const [enableGroup, setEnableGroup] = useState(false);
  const [enableImage, setEnableImage] = useState(false);
  const [enableRoutePolicy, setEnableRoutePolicy] = useState(false);
  const [provider, setProvider] = useState(DEFAULT_PROVIDER);
  const [model, setModel] = useState("opus");
  const [deployGroup, setDeployGroup] = useState("stable");
  const [image, setImage] = useState("");
  const [litellmRoutePolicy, setLitellmRoutePolicy] = useState(DEFAULT_LITELLM_ROUTE_POLICY);

  const modelOptions = enableProvider ? (PROVIDER_MODELS[provider] || ALL_MODELS) : ALL_MODELS;

  const handleProviderChange = (val) => {
    setProvider(val);
    const available = PROVIDER_MODELS[val] || ALL_MODELS;
    if (!available.some((m) => m.value === model)) {
      setModel(available[0].value);
    }
  };

  const groupNames = deployGroups.map((g) => g.name || g).filter(Boolean);

  const handleSubmit = () => {
    const params = {};
    if (enableProvider) params.provider = provider;
    if (enableModel) params.model = model;
    if (enableRoutePolicy) params.litellm_route_policy = litellmRoutePolicy;
    if (enableGroup) params.deploy_group = deployGroup;
    if (enableImage) params.image = image;
    if (!Object.keys(params).length) return;
    onSubmit(params);
  };

  const hasChanges = enableProvider || enableModel || enableRoutePolicy || enableGroup || (enableImage && image);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-[420px] space-y-5" onClick={(e) => e.stopPropagation()}>
        <h3 className="text-lg font-semibold text-gray-100">批量修改 ({count} 个实例)</h3>
        <p className="text-xs text-gray-500">勾选要修改的字段，留空的不会变更。Config 类变更（Provider、模型、LiteLLM 路由、灰度组）热加载生效，镜像变更会触发滚动更新。</p>

        <div className="space-y-4">
          {/* Provider */}
          <label className="flex items-center gap-3">
            <input type="checkbox" checked={enableProvider} onChange={(e) => setEnableProvider(e.target.checked)} className="rounded border-gray-600" />
            <span className="text-sm text-gray-300 w-16">Provider</span>
            <select className="input flex-1" value={provider} onChange={(e) => handleProviderChange(e.target.value)} disabled={!enableProvider}>
              {PROVIDER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>

          {/* Model */}
          <label className="flex items-center gap-3">
            <input type="checkbox" checked={enableModel} onChange={(e) => setEnableModel(e.target.checked)} className="rounded border-gray-600" />
            <span className="text-sm text-gray-300 w-16">模型</span>
            <select className="input flex-1" value={model} onChange={(e) => setModel(e.target.value)} disabled={!enableModel}>
              {modelOptions.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
          </label>

          {/* LiteLLM Route Policy */}
          <label className="flex items-center gap-3">
            <input type="checkbox" checked={enableRoutePolicy} onChange={(e) => setEnableRoutePolicy(e.target.checked)} className="rounded border-gray-600" />
            <span className="text-sm text-gray-300 w-16">LiteLLM</span>
            <select className="input flex-1" value={litellmRoutePolicy} onChange={(e) => setLitellmRoutePolicy(e.target.value)} disabled={!enableRoutePolicy}>
              {LITELLM_ROUTE_POLICY_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>

          {/* Deploy Group */}
          <label className="flex items-center gap-3">
            <input type="checkbox" checked={enableGroup} onChange={(e) => setEnableGroup(e.target.checked)} className="rounded border-gray-600" />
            <span className="text-sm text-gray-300 w-16">灰度组</span>
            {groupNames.length > 0 ? (
              <select className="input flex-1" value={deployGroup} onChange={(e) => setDeployGroup(e.target.value)} disabled={!enableGroup}>
                {groupNames.map((g) => <option key={g} value={g}>{g}</option>)}
              </select>
            ) : (
              <input className="input flex-1" value={deployGroup} onChange={(e) => setDeployGroup(e.target.value)}
                placeholder="stable / canary / ..." disabled={!enableGroup} />
            )}
          </label>

          {/* Image */}
          <label className="flex items-center gap-3">
            <input type="checkbox" checked={enableImage} onChange={(e) => setEnableImage(e.target.checked)} className="rounded border-gray-600" />
            <span className="text-sm text-gray-300 w-16">镜像</span>
            <select className="input flex-1" value={image} onChange={(e) => setImage(e.target.value)} disabled={!enableImage}>
              <option value="">选择镜像版本...</option>
              {imageTags.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button className="btn btn-ghost" onClick={onClose}>取消</button>
          <button className="btn btn-primary" onClick={handleSubmit} disabled={!hasChanges}>
            确认修改
          </button>
        </div>
      </div>
    </div>
  );
}

function DeployGroupBadge({ group }) {
  if (!group) return <span className="text-gray-600 text-xs">-</span>;
  const cls = {
    stable: "bg-emerald-600/20 text-emerald-400",
    canary: "bg-amber-600/20 text-amber-400",
    beta: "bg-blue-600/20 text-blue-400",
  }[group] || "bg-gray-600/20 text-gray-400";
  return <span className={`badge ${cls}`}>{group}</span>;
}

function StatusBadge({ status }) {
  const cls = {
    Running: "bg-emerald-600/20 text-emerald-400",
    Stopped: "bg-yellow-600/20 text-yellow-400",
    Paused: "bg-orange-600/20 text-orange-400",
    Pending: "bg-blue-600/20 text-blue-400",
    Failed: "bg-red-600/20 text-red-400",
  }[status] || "bg-gray-600/20 text-gray-400";

  return <span className={`badge ${cls}`}>{status}</span>;
}
