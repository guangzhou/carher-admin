import { useState, useEffect } from "react";
import { api } from "../api";
import {
  DEFAULT_LITELLM_ROUTE_POLICY,
  DEFAULT_PROVIDER,
  LITELLM_ROUTE_POLICY_OPTIONS,
  PROVIDER_MODELS,
  PROVIDER_OPTIONS,
} from "../models";

export default function AddInstance({ onCreated }) {
  const [form, setForm] = useState({
    id: "",
    name: "",
    model: "opus",
    app_id: "",
    app_secret: "",
    prefix: "s1",
    owner: "",
    provider: DEFAULT_PROVIDER,
    litellm_route_policy: DEFAULT_LITELLM_ROUTE_POLICY,
  });
  const [nextId, setNextId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);

  useEffect(() => {
    api.getNextId().then((d) => {
      setNextId(d.next_id);
      setForm((f) => ({ ...f, id: String(d.next_id) }));
    });
  }, []);

  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    if (!form.name || !form.app_id || !form.app_secret) {
      alert("请填写名字、App ID 和 App Secret");
      return;
    }
    setLoading(true);
    setResult(null);
    try {
      const r = await api.addInstance({
        ...form,
        id: form.id ? Number(form.id) : null,
      });
      setResult(r);
    } catch (e) {
      setResult({ error: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-2xl space-y-6">
      <h2 className="text-xl font-semibold">新增 Her 实例</h2>

      <form onSubmit={submit} className="card p-6 space-y-5">
        <div className="grid grid-cols-2 gap-4">
          <Field label="ID" hint={nextId ? `建议: ${nextId}` : ""}>
            <input className="input w-full" value={form.id} onChange={set("id")} placeholder="自动分配" />
          </Field>
          <Field label="名字" required>
            <input className="input w-full" value={form.name} onChange={set("name")} placeholder="张三" />
          </Field>
          <Field label="飞书 App ID" required>
            <input className="input w-full" value={form.app_id} onChange={set("app_id")} placeholder="cli_xxx" />
          </Field>
          <Field label="飞书 App Secret" required>
            <input className="input w-full" type="password" value={form.app_secret} onChange={set("app_secret")} placeholder="xxx" />
          </Field>
          <Field label="Provider">
            <select className="input w-full" value={form.provider} onChange={(e) => {
              const p = e.target.value;
              const models = PROVIDER_MODELS[p] || PROVIDER_MODELS.openrouter;
              const next = { ...form, provider: p };
              if (!models.some((m) => m.value === form.model)) next.model = models[0].value;
              setForm(next);
            }}>
              {PROVIDER_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </Field>
          <Field label="模型">
            <select className="input w-full" value={form.model} onChange={set("model")}>
              {(PROVIDER_MODELS[form.provider] || PROVIDER_MODELS.openrouter).map((m) => (
                <option key={m.value} value={m.value}>{m.label}</option>
              ))}
            </select>
          </Field>
          {form.provider === "litellm" && (
            <Field label="LiteLLM 路由策略" hint="仅影响 gpt / sonnet / opus / gemini">
              <select className="input w-full" value={form.litellm_route_policy} onChange={set("litellm_route_policy")}>
                {LITELLM_ROUTE_POLICY_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </Field>
          )}
          <Field label="域名前缀">
            <select className="input w-full" value={form.prefix} onChange={set("prefix")}>
              <option value="s1">s1</option>
              <option value="s2">s2</option>
              <option value="s3">s3</option>
            </select>
          </Field>
          <Field label="Owner (open_id)">
            <input className="input w-full" value={form.owner} onChange={set("owner")} placeholder="ou_xxx (可选)" />
          </Field>
        </div>

        <div className="flex justify-end">
          <button type="submit" className="btn btn-primary px-6" disabled={loading}>
            {loading ? "创建中..." : "创建实例"}
          </button>
        </div>
      </form>

      {/* Result */}
      {result && (
        <div className={`card p-5 ${result.error ? "border-red-600/30" : "border-emerald-600/30"} border`}>
          {result.error ? (
            <p className="text-red-400">创建失败: {result.error}</p>
          ) : (
            <div className="space-y-3">
              <p className="text-emerald-400 font-medium">carher-{result.id} 创建成功!</p>
              {result.oauth_url && (
                <div>
                  <p className="text-xs text-gray-500 mb-1">IT 需要配置的 OAuth 回调 URL:</p>
                  <div className="flex items-center gap-2">
                    <code className="text-sm text-blue-400 bg-gray-800 px-3 py-2 rounded flex-1 font-mono">{result.oauth_url}</code>
                    <button className="btn btn-ghost text-xs" onClick={() => navigator.clipboard.writeText(result.oauth_url)}>复制</button>
                  </div>
                </div>
              )}
              <button className="btn btn-primary text-xs" onClick={() => onCreated(result.id)}>查看详情</button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, hint, required, children }) {
  return (
    <div>
      <label className="block text-xs text-gray-500 mb-1">
        {label} {required && <span className="text-red-400">*</span>} {hint && <span className="text-gray-600 ml-1">({hint})</span>}
      </label>
      {children}
    </div>
  );
}
