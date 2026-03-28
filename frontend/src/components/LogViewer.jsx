import { useEffect, useState, useRef } from "react";
import { api } from "../api";

export default function LogViewer({ id }) {
  const [logs, setLogs] = useState("");
  const [tail, setTail] = useState(200);
  const [loading, setLoading] = useState(true);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const ref = useRef(null);

  const load = () => {
    setLoading(true);
    api.getLogs(id, tail).then((d) => {
      setLogs(d.logs || "");
      setTimeout(() => ref.current?.scrollTo(0, ref.current.scrollHeight), 50);
    }).finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, [id, tail]);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [autoRefresh, id, tail]);

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between p-3 border-b border-gray-800">
        <h3 className="text-sm font-medium text-gray-400">carher-{id} 日志</h3>
        <div className="flex items-center gap-2">
          <select className="input text-xs py-1" value={tail} onChange={(e) => setTail(Number(e.target.value))}>
            <option value={100}>100 行</option>
            <option value={200}>200 行</option>
            <option value={500}>500 行</option>
            <option value={1000}>1000 行</option>
          </select>
          <label className="flex items-center gap-1 text-xs text-gray-400 cursor-pointer">
            <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
            自动刷新
          </label>
          <button className="btn btn-ghost text-xs py-1" onClick={load} disabled={loading}>刷新</button>
        </div>
      </div>
      <pre
        ref={ref}
        className="p-4 text-xs font-mono text-gray-300 bg-gray-950 overflow-auto max-h-[500px] leading-5 whitespace-pre-wrap"
      >
        {logs || (loading ? "加载中..." : "暂无日志")}
      </pre>
    </div>
  );
}
