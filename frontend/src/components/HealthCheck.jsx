import { useEffect, useState } from "react";
import { api } from "../api";

export default function HealthCheck() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    api.getHealth().then(setItems).finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  const ok = items.filter((i) => i.feishu_ws && i.memory_db && i.model_ok).length;
  const warn = items.length - ok;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">健康检查</h2>
        <button className="btn btn-ghost" onClick={load} disabled={loading}>
          {loading ? "检查中..." : "重新检查"}
        </button>
      </div>

      {/* Summary */}
      <div className="flex gap-4">
        <div className="card p-4 border border-emerald-600/20 flex-1">
          <p className="text-xs text-gray-500">正常</p>
          <p className="text-2xl font-bold text-emerald-400">{ok}</p>
        </div>
        <div className="card p-4 border border-yellow-600/20 flex-1">
          <p className="text-xs text-gray-500">异常</p>
          <p className="text-2xl font-bold text-yellow-400">{warn}</p>
        </div>
        <div className="card p-4 border border-blue-600/20 flex-1">
          <p className="text-xs text-gray-500">总计</p>
          <p className="text-2xl font-bold text-blue-400">{items.length}</p>
        </div>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-800 text-gray-500 text-left">
              <th className="p-3">ID</th>
              <th className="p-3">名字</th>
              <th className="p-3 text-center">飞书 WS</th>
              <th className="p-3 text-center">记忆库</th>
              <th className="p-3 text-center">模型加载</th>
              <th className="p-3 text-center">综合</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const allOk = item.feishu_ws && item.memory_db && item.model_ok;
              return (
                <tr key={item.id} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                  <td className="p-3 font-mono">{item.id}</td>
                  <td className="p-3 text-gray-200">{item.name || "-"}</td>
                  <td className="p-3 text-center"><Check ok={item.feishu_ws} /></td>
                  <td className="p-3 text-center"><Check ok={item.memory_db} /></td>
                  <td className="p-3 text-center"><Check ok={item.model_ok} /></td>
                  <td className="p-3 text-center">
                    <span className={`badge ${allOk ? "bg-emerald-600/20 text-emerald-400" : "bg-yellow-600/20 text-yellow-400"}`}>
                      {allOk ? "正常" : "异常"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {items.length === 0 && (
          <p className="p-8 text-center text-gray-500">{loading ? "检查中..." : "没有运行中的实例"}</p>
        )}
      </div>
    </div>
  );
}

function Check({ ok }) {
  return ok ? (
    <span className="text-emerald-400 text-lg">●</span>
  ) : (
    <span className="text-yellow-400 text-lg">○</span>
  );
}
