import { useState } from "react";
import { api } from "../api";

const CSV_TEMPLATE = `# name,model,app_id,app_secret,prefix,owner,provider
# 例:
# 张三,gpt,cli_axxxx,secret_xxx,s1,ou_xxx,openrouter
# 李四,sonnet,cli_bxxxx,secret_yyy,s2,,wangsu`;

const FIELDS = ["name", "model", "app_id", "app_secret", "prefix", "owner", "provider"];

export default function BatchImport({ onDone }) {
  const [mode, setMode] = useState("csv"); // csv | form
  const [csv, setCsv] = useState("");
  const [parsed, setParsed] = useState([]);
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState(null);

  const parseCSV = () => {
    const lines = csv.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#"));
    const items = lines.map((line) => {
      const cols = line.split(",").map((c) => c.trim());
      return {
        name: cols[0] || "",
        model: cols[1] || "gpt",
        app_id: cols[2] || "",
        app_secret: cols[3] || "",
        prefix: cols[4] || "s1",
        owner: cols[5] || "",
        provider: cols[6] || "openrouter",
      };
    });
    setParsed(items);
  };

  const handleFile = (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      setCsv(ev.target.result);
    };
    reader.readAsText(file);
  };

  const submit = async () => {
    if (!parsed.length) {
      alert("请先解析 CSV");
      return;
    }
    const invalid = parsed.filter((p) => !p.name || !p.app_id || !p.app_secret);
    if (invalid.length) {
      alert(`${invalid.length} 行缺少必填字段 (name, app_id, app_secret)`);
      return;
    }
    if (!confirm(`确认导入 ${parsed.length} 个 Her 实例？`)) return;
    setLoading(true);
    try {
      const r = await api.batchImport(parsed);
      setResults(r.results);
    } catch (e) {
      alert(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-4xl space-y-6">
      <h2 className="text-xl font-semibold">批量导入</h2>

      {/* CSV Input */}
      <div className="card p-6 space-y-4">
        <div className="flex items-center gap-3">
          <h3 className="text-sm font-medium text-gray-400">CSV 数据</h3>
          <label className="btn btn-ghost text-xs cursor-pointer">
            上传 CSV 文件
            <input type="file" accept=".csv,.txt" className="hidden" onChange={handleFile} />
          </label>
        </div>

        <pre className="text-xs text-gray-600 bg-gray-800/50 p-3 rounded">{CSV_TEMPLATE}</pre>

        <textarea
          className="input w-full h-40 font-mono text-xs"
          placeholder="粘贴 CSV 内容，或上传文件..."
          value={csv}
          onChange={(e) => setCsv(e.target.value)}
        />

        <div className="flex justify-between">
          <button className="btn btn-ghost" onClick={parseCSV} disabled={!csv.trim()}>
            解析预览
          </button>
        </div>
      </div>

      {/* Preview table */}
      {parsed.length > 0 && (
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between p-3 border-b border-gray-800">
            <h3 className="text-sm font-medium text-gray-400">预览 ({parsed.length} 个实例)</h3>
            <button className="btn btn-primary" onClick={submit} disabled={loading}>
              {loading ? `导入中... (0/${parsed.length})` : `确认导入 ${parsed.length} 个`}
            </button>
          </div>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-800 text-gray-500">
                <th className="p-2 text-left">#</th>
                {FIELDS.map((f) => <th key={f} className="p-2 text-left">{f}</th>)}
                <th className="p-2 text-left">状态</th>
              </tr>
            </thead>
            <tbody>
              {parsed.map((row, i) => {
                const valid = row.name && row.app_id && row.app_secret;
                const res = results?.[i];
                return (
                  <tr key={i} className="border-b border-gray-800/50">
                    <td className="p-2 text-gray-500">{i + 1}</td>
                    {FIELDS.map((f) => (
                      <td key={f} className={`p-2 ${!row[f] && (f === "name" || f === "app_id" || f === "app_secret") ? "text-red-400" : "text-gray-300"}`}>
                        {f === "app_secret" ? "***" : (row[f] || "-")}
                      </td>
                    ))}
                    <td className="p-2">
                      {res ? (
                        res.error
                          ? <span className="text-red-400 text-xs">失败</span>
                          : <span className="text-emerald-400 text-xs">ID: {res.id}</span>
                      ) : (
                        valid
                          ? <span className="text-gray-500">待导入</span>
                          : <span className="text-red-400">缺字段</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Results */}
      {results && (
        <div className="card p-5 border border-emerald-600/20">
          <p className="text-emerald-400 font-medium mb-3">
            导入完成: {results.filter((r) => !r.error).length} 成功, {results.filter((r) => r.error).length} 失败
          </p>
          {results.filter((r) => r.oauth_url).length > 0 && (
            <div>
              <p className="text-xs text-gray-500 mb-2">OAuth 回调 URL 列表（IT 需要配置到飞书后台）:</p>
              <div className="space-y-1 max-h-60 overflow-auto">
                {results.filter((r) => r.oauth_url).map((r) => (
                  <div key={r.id} className="flex items-center gap-2 text-xs">
                    <span className="text-gray-400 w-12">#{r.id}</span>
                    <code className="text-blue-400 font-mono">{r.oauth_url}</code>
                  </div>
                ))}
              </div>
            </div>
          )}
          <button className="btn btn-primary mt-4 text-xs" onClick={onDone}>返回实例列表</button>
        </div>
      )}
    </div>
  );
}
