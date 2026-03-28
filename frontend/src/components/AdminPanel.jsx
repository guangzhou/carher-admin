import { useState } from "react";
import { api } from "../api";

export default function AdminPanel() {
  const [syncResult, setSyncResult] = useState(null);
  const [checkResult, setCheckResult] = useState(null);
  const [importResult, setImportResult] = useState(null);
  const [auditLog, setAuditLog] = useState(null);
  const [loading, setLoading] = useState("");

  const doAction = async (key, fn) => {
    setLoading(key);
    try {
      const r = await fn();
      if (key === "sync") setSyncResult(r);
      if (key === "check") setCheckResult(r);
      if (key === "import") setImportResult(r);
      if (key === "audit") setAuditLog(r);
    } catch (e) {
      alert(`操作失败: ${e.message}`);
    } finally {
      setLoading("");
    }
  };

  return (
    <div className="space-y-6 max-w-4xl">
      <h2 className="text-xl font-semibold">系统管理</h2>

      {/* Sync */}
      <div className="card p-5 space-y-3">
        <h3 className="text-sm font-medium text-gray-400">DB → ConfigMap 同步</h3>
        <p className="text-xs text-gray-500">强制将 DB 中所有实例的配置重新生成并写入 K8s ConfigMap。后台 worker 每 60s 自动重试 pending 状态的同步。</p>
        <div className="flex gap-2">
          <button className="btn btn-primary" onClick={() => doAction("sync", api.forceSync)} disabled={!!loading}>
            {loading === "sync" ? "同步中..." : "强制全量同步"}
          </button>
          <button className="btn btn-ghost" onClick={() => doAction("check", api.consistencyCheck)} disabled={!!loading}>
            {loading === "check" ? "检查中..." : "一致性检查"}
          </button>
        </div>
        {syncResult && (
          <div className="text-sm text-emerald-400">
            同步完成: {syncResult.synced} 成功, {syncResult.failed} 失败
          </div>
        )}
        {checkResult && (
          <div className="space-y-1">
            {checkResult.length === 0 ? (
              <p className="text-sm text-emerald-400">DB 与 K8s 完全一致</p>
            ) : (
              checkResult.map((issue, i) => (
                <div key={i} className="text-sm flex gap-2">
                  <span className="text-yellow-400 font-mono">#{issue.id}</span>
                  <span className="text-gray-300">{issue.detail}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      {/* Import from K8s */}
      <div className="card p-5 space-y-3">
        <h3 className="text-sm font-medium text-gray-400">从 K8s 导入</h3>
        <p className="text-xs text-gray-500">一次性迁移：扫描所有 carher-*-user-config ConfigMap，将用户信息提取并导入到数据库。已存在的 ID 会跳过。</p>
        <button className="btn btn-ghost" onClick={() => doAction("import", api.importFromK8s)} disabled={!!loading}>
          {loading === "import" ? "导入中..." : "扫描并导入"}
        </button>
        {importResult && (
          <div className="text-sm">
            <span className="text-emerald-400">导入: {importResult.imported}</span>
            <span className="text-gray-500 ml-3">跳过: {importResult.skipped}</span>
            <span className="text-gray-500 ml-3">总扫描: {importResult.total}</span>
          </div>
        )}
      </div>

      {/* Audit log */}
      <div className="card p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-gray-400">操作审计日志</h3>
          <button className="btn btn-ghost text-xs" onClick={() => doAction("audit", () => api.getAuditLog(null, 100))} disabled={!!loading}>
            {loading === "audit" ? "加载中..." : "加载最近 100 条"}
          </button>
        </div>
        {auditLog && (
          <div className="max-h-80 overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-gray-800 text-gray-500">
                  <th className="p-2 text-left">时间</th>
                  <th className="p-2 text-left">ID</th>
                  <th className="p-2 text-left">操作</th>
                  <th className="p-2 text-left">详情</th>
                </tr>
              </thead>
              <tbody>
                {auditLog.map((log) => (
                  <tr key={log.id} className="border-b border-gray-800/50">
                    <td className="p-2 text-gray-500 font-mono whitespace-nowrap">{log.created_at}</td>
                    <td className="p-2 text-blue-400 font-mono">{log.instance_id || "-"}</td>
                    <td className="p-2">
                      <ActionBadge action={log.action} />
                    </td>
                    <td className="p-2 text-gray-400 truncate max-w-xs" title={log.detail}>{log.detail || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

function ActionBadge({ action }) {
  const colors = {
    created: "bg-emerald-600/20 text-emerald-400",
    updated: "bg-blue-600/20 text-blue-400",
    deleted: "bg-red-600/20 text-red-400",
    purged: "bg-red-600/20 text-red-400",
  };
  const cls = Object.entries(colors).find(([k]) => action.includes(k))?.[1] || "bg-gray-600/20 text-gray-400";
  return <span className={`badge ${cls}`}>{action}</span>;
}
