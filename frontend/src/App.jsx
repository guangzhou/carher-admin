import { useState, useEffect, useCallback } from "react";
import Dashboard from "./components/Dashboard";
import InstanceList from "./components/InstanceList";
import AddInstance from "./components/AddInstance";
import BatchImport from "./components/BatchImport";
import HealthCheck from "./components/HealthCheck";
import DeployPage from "./components/DeployPage";
import AdminPanel from "./components/AdminPanel";
import SettingsPage from "./components/SettingsPage";
import LoginPage from "./components/LoginPage";
import { setAuthFailureHandler } from "./api";

const TABS = [
  { id: "dashboard", label: "仪表盘", icon: "◐" },
  { id: "instances", label: "实例管理", icon: "☰" },
  { id: "deploy", label: "部署", icon: "▶" },
  { id: "admin", label: "系统", icon: "⚙" },
  { id: "settings", label: "设置", icon: "⚡" },
];

const SUB_TABS = {
  instances: [
    { id: "list", label: "实例列表" },
    { id: "add", label: "新增" },
    { id: "import", label: "批量导入" },
    { id: "health", label: "健康检查" },
  ],
};

function getInitialTab() {
  const params = new URLSearchParams(window.location.search);
  return params.get("tab") || "instances";
}

function getInitialDetail() {
  const params = new URLSearchParams(window.location.search);
  const d = params.get("detail");
  return d ? Number(d) : null;
}

function getInitialSub() {
  const params = new URLSearchParams(window.location.search);
  return params.get("sub") || "list";
}

export default function App() {
  const [user, setUser] = useState(() => localStorage.getItem("carher_user") || "");
  const [tab, setTab] = useState(getInitialTab);
  const [subTab, setSubTab] = useState(getInitialSub);
  const [detailId, setDetailId] = useState(getInitialDetail);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const handleLogout = useCallback(() => {
    localStorage.removeItem("carher_token");
    localStorage.removeItem("carher_user");
    setUser("");
  }, []);

  useEffect(() => {
    setAuthFailureHandler(() => setUser(""));
    return () => setAuthFailureHandler(null);
  }, []);

  const updateURL = useCallback((t, d, s) => {
    const params = new URLSearchParams();
    if (t && t !== "instances") params.set("tab", t);
    if (d) params.set("detail", d);
    if (s && s !== "list") params.set("sub", s);
    const qs = params.toString();
    const url = qs ? `?${qs}` : window.location.pathname;
    window.history.replaceState(null, "", url);
  }, []);

  useEffect(() => {
    updateURL(tab, detailId, subTab);
  }, [tab, detailId, subTab, updateURL]);

  const switchTab = (t) => {
    setTab(t);
    setDetailId(null);
    setSubTab("list");
    setMobileMenuOpen(false);
  };

  const openDetail = (id) => {
    setDetailId(id);
    setTab("instances");
    setSubTab("list");
  };

  if (!user) {
    return <LoginPage onLogin={(u) => setUser(u)} />;
  }

  const subs = SUB_TABS[tab];

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-gray-800 bg-gray-900/80 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-2.5 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-blue-700 flex items-center justify-center text-white font-bold text-sm shadow-lg shadow-blue-600/20">H</div>
            <h1 className="text-lg font-semibold text-white hidden sm:block">CarHer Admin</h1>
          </div>

          {/* Desktop nav */}
          <nav className="hidden md:flex items-center gap-0.5">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => switchTab(t.id)}
                className={`px-3.5 py-2 rounded-lg text-sm font-medium transition-all ${
                  tab === t.id
                    ? "bg-blue-600/20 text-blue-400 shadow-sm"
                    : "text-gray-400 hover:text-gray-200 hover:bg-gray-800/80"
                }`}
              >
                <span className="mr-1.5">{t.icon}</span>
                {t.label}
              </button>
            ))}
          </nav>

          {/* Mobile menu button */}
          <button
            className="md:hidden text-gray-400 hover:text-white p-2"
            onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          >
            {mobileMenuOpen ? "✕" : "☰"}
          </button>

          <div className="hidden md:flex items-center gap-2 ml-3 pl-3 border-l border-gray-700">
            <span className="text-gray-500 text-sm">{user}</span>
            <button
              onClick={handleLogout}
              className="text-gray-500 hover:text-gray-300 text-sm transition-colors"
            >
              退出
            </button>
          </div>
        </div>

        {/* Mobile nav dropdown */}
        {mobileMenuOpen && (
          <div className="md:hidden border-t border-gray-800 px-4 py-2 space-y-1 bg-gray-900">
            {TABS.map((t) => (
              <button
                key={t.id}
                onClick={() => switchTab(t.id)}
                className={`block w-full text-left px-3 py-2 rounded-lg text-sm ${
                  tab === t.id ? "bg-blue-600/20 text-blue-400" : "text-gray-400"
                }`}
              >
                <span className="mr-2">{t.icon}</span>{t.label}
              </button>
            ))}
            <div className="border-t border-gray-800 pt-2 mt-2 flex items-center justify-between px-3">
              <span className="text-gray-500 text-sm">{user}</span>
              <button onClick={handleLogout} className="text-gray-500 hover:text-gray-300 text-sm">退出</button>
            </div>
          </div>
        )}

        {/* Sub-tabs */}
        {subs && (
          <div className="border-t border-gray-800/50 bg-gray-900/40">
            <div className="max-w-7xl mx-auto px-4 sm:px-6 flex gap-0.5 overflow-x-auto">
              {subs.map((s) => (
                <button
                  key={s.id}
                  onClick={() => { setSubTab(s.id); setDetailId(null); }}
                  className={`px-3 py-2 text-xs font-medium border-b-2 transition-colors whitespace-nowrap ${
                    subTab === s.id
                      ? "border-blue-500 text-blue-400"
                      : "border-transparent text-gray-500 hover:text-gray-300"
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </header>

      {/* Content */}
      <main className="flex-1 max-w-7xl mx-auto px-4 sm:px-6 py-6 w-full">
        {tab === "dashboard" && <Dashboard />}
        {tab === "instances" && subTab === "list" && <InstanceList detailId={detailId} setDetailId={setDetailId} />}
        {tab === "instances" && subTab === "add" && <AddInstance onCreated={(id) => openDetail(id)} />}
        {tab === "instances" && subTab === "import" && <BatchImport onDone={() => { setTab("instances"); setSubTab("list"); }} />}
        {tab === "instances" && subTab === "health" && <HealthCheck />}
        {tab === "deploy" && <DeployPage />}
        {tab === "admin" && <AdminPanel />}
        {tab === "settings" && <SettingsPage />}
      </main>
    </div>
  );
}
