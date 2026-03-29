import { useEffect, useState } from "react";
import { api } from "../api";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.getStatus(), api.getMetricsOverview()])
      .then(([s, m]) => { setStatus(s); setMetrics(m); })
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <Skeleton />;
  if (!status) return <p className="text-gray-500">加载失败</p>;

  const cluster = metrics?.cluster || {};
  const her = metrics?.her_totals || {};
  const nodes = metrics?.nodes || [];
  const storage = metrics?.storage || {};

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">集群概览</h2>

      {/* Top stats */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        <StatCard label="运行中" value={status.running} color="emerald" />
        <StatCard label="已停止" value={status.stopped} color="yellow" />
        <StatCard label="Pod 总数" value={status.total_pods} color="blue" />
        <StatCard label="节点" value={cluster.node_count || 0} color="purple" />
        <StatCard label="PVC" value={`${storage.bound || 0}/${storage.total_pvcs || 0}`} color="blue" text />
        <StatCard label="Tunnel" value={status.tunnel_status || "active"} color="purple" text />
      </div>

      {/* Cluster CPU & Memory */}
      {cluster.cpu_capacity_m > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <ResourceCard
            title="集群 CPU"
            used={cluster.cpu_used_m}
            capacity={cluster.cpu_capacity_m}
            percent={cluster.cpu_percent}
            unit="m"
            color="blue"
          />
          <ResourceCard
            title="集群内存"
            used={cluster.memory_used_mi}
            capacity={cluster.memory_capacity_mi}
            percent={cluster.memory_percent}
            unit="Mi"
            color="purple"
          />
        </div>
      )}

      {/* Her instance resource summary */}
      {her.instance_count > 0 && (
        <div className="card p-5">
          <h3 className="text-sm font-medium text-gray-400 mb-3">Her 实例资源汇总</h3>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 text-sm">
            <div>
              <span className="text-gray-500">实例数</span>
              <p className="text-lg font-bold text-blue-400">{her.instance_count}</p>
            </div>
            <div>
              <span className="text-gray-500">总 CPU</span>
              <p className="text-lg font-bold text-emerald-400">{her.cpu_m}m</p>
            </div>
            <div>
              <span className="text-gray-500">总内存</span>
              <p className="text-lg font-bold text-purple-400">{formatMemory(her.memory_mi)}</p>
            </div>
            <div>
              <span className="text-gray-500">平均/实例</span>
              <p className="text-lg font-bold text-gray-300">
                {her.avg_cpu_m}m / {Math.round(her.avg_memory_mi)}Mi
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Node distribution with resource bars */}
      <div className="card p-6">
        <h3 className="text-sm font-medium text-gray-400 mb-4">节点资源</h3>
        <div className="space-y-4">
          {nodes.map((n) => (
            <NodeRow key={n.name} node={n} />
          ))}
          {nodes.length === 0 && (status.nodes || []).map((n) => (
            <div key={n.name} className="flex items-center gap-3">
              <span className="text-sm text-gray-300 w-48 truncate font-mono">{n.name}</span>
              <span className="text-sm text-gray-400">{n.pods} pods</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function formatMemory(mi) {
  if (mi >= 1024) return `${(mi / 1024).toFixed(1)}Gi`;
  return `${Math.round(mi)}Mi`;
}

function ResourceCard({ title, used, capacity, percent, unit, color }) {
  const barColor = percent > 80 ? "bg-red-500" : percent > 60 ? "bg-yellow-500" : `bg-${color}-500`;
  return (
    <div className="card p-5">
      <div className="flex justify-between items-center mb-2">
        <h3 className="text-sm font-medium text-gray-400">{title}</h3>
        <span className={`text-lg font-bold ${percent > 80 ? "text-red-400" : percent > 60 ? "text-yellow-400" : `text-${color}-400`}`}>
          {percent}%
        </span>
      </div>
      <div className="w-full bg-gray-800 rounded-full h-3 mb-2">
        <div className={`${barColor} h-full rounded-full transition-all`} style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
      <p className="text-xs text-gray-500">
        {unit === "Mi" ? formatMemory(used) : `${Math.round(used)}${unit}`}
        {" / "}
        {unit === "Mi" ? formatMemory(capacity) : `${Math.round(capacity)}${unit}`}
      </p>
    </div>
  );
}

function NodeRow({ node }) {
  return (
    <div className="border border-gray-800 rounded-lg p-3 space-y-2">
      <div className="flex justify-between items-center">
        <span className="text-sm text-gray-300 font-mono">{node.name}</span>
        <span className="text-xs text-gray-500">{node.pod_count} pods</span>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <MiniBar label="CPU" percent={node.cpu_percent} detail={`${Math.round(node.cpu_used_m)}m / ${Math.round(node.cpu_capacity_m)}m`} />
        <MiniBar label="MEM" percent={node.memory_percent} detail={`${formatMemory(node.memory_used_mi)} / ${formatMemory(node.memory_capacity_mi)}`} />
      </div>
    </div>
  );
}

function MiniBar({ label, percent, detail }) {
  const color = percent > 80 ? "bg-red-500" : percent > 60 ? "bg-yellow-500" : "bg-blue-500";
  return (
    <div>
      <div className="flex justify-between text-xs text-gray-500 mb-1">
        <span>{label}</span>
        <span>{percent}%</span>
      </div>
      <div className="w-full bg-gray-800 rounded-full h-2">
        <div className={`${color} h-full rounded-full`} style={{ width: `${Math.min(percent, 100)}%` }} />
      </div>
      <p className="text-xs text-gray-600 mt-0.5">{detail}</p>
    </div>
  );
}

function StatCard({ label, value, color, text }) {
  const colors = {
    emerald: "text-emerald-400 bg-emerald-600/10 border-emerald-600/20",
    yellow: "text-yellow-400 bg-yellow-600/10 border-yellow-600/20",
    blue: "text-blue-400 bg-blue-600/10 border-blue-600/20",
    purple: "text-purple-400 bg-purple-600/10 border-purple-600/20",
  };
  return (
    <div className={`card p-4 border ${colors[color]}`}>
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`${text ? "text-lg" : "text-2xl"} font-bold ${colors[color].split(" ")[0]}`}>{value}</p>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="h-8 bg-gray-800 rounded w-32" />
      <div className="grid grid-cols-6 gap-3">
        {[1, 2, 3, 4, 5, 6].map((i) => <div key={i} className="h-20 bg-gray-800 rounded-xl" />)}
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div className="h-32 bg-gray-800 rounded-xl" />
        <div className="h-32 bg-gray-800 rounded-xl" />
      </div>
    </div>
  );
}
