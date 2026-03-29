import { useState, useEffect, useCallback } from "react";
import Dashboard from "./components/Dashboard";
import InstanceList from "./components/InstanceList";
import AddInstance from "./components/AddInstance";
import BatchImport from "./components/BatchImport";
import HealthCheck from "./components/HealthCheck";
import DeployPage from "./components/DeployPage";
import AdminPanel from "./components/AdminPanel";
import LoginPage from "./components/LoginPage";
import { setAuthFailureHandler } from "./api";

const TABS = [
  { id: "dashboard", label: "仪表盘" },
  { id: "instances", label: "实例管理" },
  { id: "deploy", label: "部署" },
  { id: "add", label: "新增" },
  { id: "import", label: "批量导入" },
  { id: "health", label: "健康检查" },
  { id: "admin", label: "系统管理" },
];

function getInitialTab() {
  const params = new URLSearchParams(window.location.search);
  return params.get("tab") || "instances";
}

function getInitialDetail() {
  const params = new URLSearchParams(window.location.search);
  const d = params.get("detail");
  return d ? Number(d) : null;
}

export default function App() {
  const [user, setUser] = useState(() => localStorage.getItem("carher_user") || "");
  const [tab, setTab] = useState(getInitialTab);
  const [detailId, setDetailId] = useState(getInitialDetail);

  const handleLogout = useCallback(() => {
    localStorage.removeItem("carher_token");
    localStorage.removeItem("carher_user");
    setUser("");
  }, []);

  useEffect(() => {
    setAuthFailureHandler(() => setUser(""));
    return () => setAuthFailureHandler(null);
  }, []);

  const updateURL = useCallback((t, d) => {
    const params = new URLSearchParams();
    if (t && t !== "instances") params.set("tab", t);
    if (d) params.set("detail", d);
    const qs = params.toString();
    const url = qs ? `?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, []);

  useEffect(() => {
    updateURL(tab, detailId);
  }, [tab, detailId, updateURL]);

  const openDetail = (id) => {
    setDetailId(id);
    setTab("instances");
  };

  if (!user) {
    return <LoginPage onLogin={(u) => setUser(u)} />;
  }

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-bold text-sm">H</div>
            <h1 className="text-lg font-semibold text-white">CarHer Admin</h1>
          </div>
          <div className="flex items-center gap-2">
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
            <div className="ml-3 pl-3 border-l border-gray-700 flex items-center gap-2">
              <span className="text-gray-500 text-sm">{user}</span>
              <button
                onClick={handleLogout}
                className="text-gray-500 hover:text-gray-300 text-sm transition-colors"
              >
                退出
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="flex-1 max-w-7xl mx-auto px-4 sm:px-6 py-6 w-full">
        {tab === "dashboard" && <Dashboard />}
        {tab === "instances" && <InstanceList detailId={detailId} setDetailId={setDetailId} />}
        {tab === "add" && <AddInstance onCreated={(id) => openDetail(id)} />}
        {tab === "import" && <BatchImport onDone={() => setTab("instances")} />}
        {tab === "deploy" && <DeployPage />}
        {tab === "health" && <HealthCheck />}
        {tab === "admin" && <AdminPanel />}
      </main>
    </div>
  );
}
