import { useEffect, useState } from "react";
import { api } from "../api";

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getStatus().then(setStatus).finally(() => setLoading(false));
  }, []);

  if (loading) return <Skeleton />;
  if (!status) return <p className="text-gray-500">加载失败</p>;

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">集群概览</h2>

      {/* Stats cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="运行中" value={status.running} color="emerald" />
        <StatCard label="已停止" value={status.stopped} color="yellow" />
        <StatCard label="Pod 总数" value={status.total_pods} color="blue" />
        <StatCard label="Tunnel" value={status.tunnel_status || "active"} color="purple" text />
      </div>

      {/* Node distribution */}
      <div className="card p-6">
        <h3 className="text-sm font-medium text-gray-400 mb-4">节点分布</h3>
        <div className="space-y-3">
          {(status.nodes || []).map((n) => (
            <div key={n.name} className="flex items-center gap-3">
              <span className="text-sm text-gray-300 w-48 truncate font-mono">{n.name}</span>
              <div className="flex-1 bg-gray-800 rounded-full h-5 overflow-hidden">
                <div
                  className="bg-blue-600 h-full rounded-full flex items-center justify-end pr-2"
                  style={{ width: `${Math.min((n.pods / Math.max(status.total_pods, 1)) * 100, 100)}%`, minWidth: "2rem" }}
                >
                  <span className="text-xs text-white font-medium">{n.pods}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
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
    <div className={`card p-5 border ${colors[color]}`}>
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`${text ? "text-lg" : "text-3xl"} font-bold ${colors[color].split(" ")[0]}`}>{value}</p>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-6 animate-pulse">
      <div className="h-8 bg-gray-800 rounded w-32" />
      <div className="grid grid-cols-4 gap-4">
        {[1, 2, 3, 4].map((i) => <div key={i} className="h-24 bg-gray-800 rounded-xl" />)}
      </div>
    </div>
  );
}
