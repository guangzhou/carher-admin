import { useState } from "react";
import Dashboard from "./components/Dashboard";
import InstanceList from "./components/InstanceList";
import AddInstance from "./components/AddInstance";
import BatchImport from "./components/BatchImport";
import HealthCheck from "./components/HealthCheck";

const TABS = [
  { id: "dashboard", label: "仪表盘" },
  { id: "instances", label: "实例管理" },
  { id: "add", label: "新增" },
  { id: "import", label: "批量导入" },
  { id: "health", label: "健康检查" },
];

export default function App() {
  const [tab, setTab] = useState("instances");
  const [detailId, setDetailId] = useState(null);

  const openDetail = (id) => {
    setDetailId(id);
    setTab("instances");
  };

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm">H</div>
            <h1 className="text-lg font-semibold text-white">CarHer Admin</h1>
          </div>
          <nav className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => { setTab(t.id); setDetailId(null); }}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  tab === t.id
                    ? "bg-blue-600/20 text-blue-400"
                    : "text-gray-400 hover:text-gray-200 hover:bg-gray-800"
                }`}
              >
                {t.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* Content */}
      <main className="flex-1 max-w-7xl mx-auto px-4 sm:px-6 py-6 w-full">
        {tab === "dashboard" && <Dashboard />}
        {tab === "instances" && <InstanceList detailId={detailId} setDetailId={setDetailId} />}
        {tab === "add" && <AddInstance onCreated={(id) => openDetail(id)} />}
        {tab === "import" && <BatchImport onDone={() => setTab("instances")} />}
        {tab === "health" && <HealthCheck />}
      </main>
    </div>
  );
}
