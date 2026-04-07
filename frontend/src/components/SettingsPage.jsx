import { useEffect, useState } from "react";
import { api } from "../api";

const SECRET_KEYS = new Set([
  "github_token",
  "webhook_secret",
  "feishu_webhook",
  "agent_api_key",
  "acr_username",
  "acr_password",
]);

const SETTING_META = {
  github_token: { label: "GitHub Token (PAT)", desc: "用于触发构建、读取分支和 Workflow (需 Contents:read + Actions:write 权限)" },
  github_repos: { label: "GitHub 仓库列表", desc: "JSON 数组格式，如 [\"owner/repo1\", \"owner/repo2\"]" },
  webhook_secret: { label: "Deploy Webhook Secret", desc: "GitHub Actions webhook 验证密钥 (需与 GitHub Secrets 一致)" },
  feishu_webhook: { label: "飞书群 Webhook URL", desc: "部署通知推送到飞书群" },
  agent_api_key: { label: "AI Agent LLM API Key", desc: "OpenRouter / OpenAI API Key" },
  acr_registry: { label: "ACR 镜像仓库地址", desc: "阿里云容器镜像服务地址（如 cltx-her-ck-registry.ap-southeast-1.cr.aliyuncs.com）" },
  acr_username: { label: "ACR 用户名", desc: "Docker Registry 登录用户名（与 K8s acr-secret 一致）" },
  acr_password: { label: "ACR 密码", desc: "Docker Registry 登录密码（与 K8s acr-secret 一致）" },
};

export default function SettingsPage() {
  const [settings, setSettings] = useState({});
  const [edits, setEdits] = useState({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showSecrets, setShowSecrets] = useState({});
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    api.getSettings()
      .then((s) => { setSettings(s); setEdits({}); })
      .finally(() => setLoading(false));
  }, []);

  const handleChange = (key, value) => {
    setEdits((prev) => ({ ...prev, [key]: value }));
  };

  const getValue = (key) => {
    return key in edits ? edits[key] : (settings[key] || "");
  };

  const isDirty = Object.keys(edits).length > 0;

  const handleSave = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const updated = await api.updateSettings(edits);
      setSettings(updated);
      setEdits({});
      setMsg({ type: "success", text: "保存成功" });
    } catch (e) {
      setMsg({ type: "error", text: `保存失败: ${e.message}` });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = () => {
    setEdits({});
    setMsg(null);
  };

  if (loading) {
    return (
      <div className="space-y-4 animate-pulse">
        <div className="h-8 bg-gray-800 rounded w-32" />
        {[1, 2, 3, 4].map((i) => <div key={i} className="h-24 bg-gray-800 rounded-xl" />)}
      </div>
    );
  }

  const orderedKeys = [
    "github_token",
    "github_repos",
    "webhook_secret",
    "feishu_webhook",
    "agent_api_key",
    "acr_registry",
    "acr_username",
    "acr_password",
  ];

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">系统设置</h2>
        <div className="flex gap-2">
          {isDirty && (
            <button className="btn btn-ghost text-xs" onClick={handleReset}>撤销更改</button>
          )}
          <button className="btn btn-primary" onClick={handleSave} disabled={!isDirty || saving}>
            {saving ? "保存中..." : "保存设置"}
          </button>
        </div>
      </div>

      {msg && (
        <div className={`text-sm px-4 py-2 rounded-lg ${msg.type === "success" ? "bg-emerald-600/20 text-emerald-400" : "bg-red-600/20 text-red-400"}`}>
          {msg.text}
        </div>
      )}

      <div className="space-y-4">
        {orderedKeys.map((key) => {
          const meta = SETTING_META[key] || { label: key, desc: "" };
          const isSecret = SECRET_KEYS.has(key);
          const value = getValue(key);
          const isEdited = key in edits;
          const isRepoList = key === "github_repos";

          return (
            <div key={key} className={`card p-5 space-y-2 ${isEdited ? "border-blue-500/50" : ""}`}>
              <div className="flex items-center justify-between">
                <div>
                  <label className="text-sm font-medium text-gray-200">{meta.label}</label>
                  <p className="text-xs text-gray-500 mt-0.5">{meta.desc}</p>
                </div>
                {isEdited && <span className="badge bg-blue-600/20 text-blue-400 text-[10px]">已修改</span>}
              </div>

              {isRepoList ? (
                <RepoListEditor value={value} onChange={(v) => handleChange(key, v)} />
              ) : isSecret ? (
                <div className="relative">
                  <input
                    type={showSecrets[key] ? "text" : "password"}
                    className="input w-full pr-20 font-mono text-sm"
                    placeholder={`输入${meta.label}...`}
                    value={value}
                    onChange={(e) => handleChange(key, e.target.value)}
                  />
                  <button
                    className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-500 hover:text-gray-300"
                    onClick={() => setShowSecrets((p) => ({ ...p, [key]: !p[key] }))}
                  >
                    {showSecrets[key] ? "隐藏" : "显示"}
                  </button>
                </div>
              ) : (
                <input
                  className="input w-full font-mono text-sm"
                  placeholder={`输入${meta.label}...`}
                  value={value}
                  onChange={(e) => handleChange(key, e.target.value)}
                />
              )}
            </div>
          );
        })}
      </div>

      <div className="card p-5 space-y-2">
        <h3 className="text-sm font-medium text-gray-400">说明</h3>
        <ul className="text-xs text-gray-500 space-y-1 list-disc list-inside">
          <li>Settings 保存在 SQLite 数据库中，重启不丢失</li>
          <li>Secret 类型的值保存后会脱敏显示 (••••xxxx)，如需修改请输入完整新值</li>
          <li>GitHub Token 和 Webhook Secret 同时支持环境变量配置 (DB 优先)</li>
          <li>修改仓库列表后，部署页面的仓库下拉会自动更新</li>
        </ul>
      </div>
    </div>
  );
}

function RepoListEditor({ value, onChange }) {
  let repos = [];
  try {
    repos = JSON.parse(value);
    if (!Array.isArray(repos)) repos = [];
  } catch { repos = []; }

  const [newRepo, setNewRepo] = useState("");

  const addRepo = () => {
    const r = newRepo.trim();
    if (!r || repos.includes(r)) return;
    onChange(JSON.stringify([...repos, r]));
    setNewRepo("");
  };

  const removeRepo = (idx) => {
    const next = repos.filter((_, i) => i !== idx);
    onChange(JSON.stringify(next));
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        {repos.map((r, i) => (
          <span key={i} className="inline-flex items-center gap-1 px-3 py-1 rounded-full bg-gray-800 border border-gray-700 text-sm font-mono">
            {r}
            <button className="text-gray-500 hover:text-red-400 ml-1" onClick={() => removeRepo(i)}>×</button>
          </span>
        ))}
        {repos.length === 0 && <span className="text-xs text-gray-600">暂无仓库</span>}
      </div>
      <div className="flex gap-2">
        <input
          className="input flex-1 text-sm font-mono"
          placeholder="owner/repo-name"
          value={newRepo}
          onChange={(e) => setNewRepo(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addRepo()}
        />
        <button className="btn btn-ghost text-sm" onClick={addRepo}>添加</button>
      </div>
    </div>
  );
}
