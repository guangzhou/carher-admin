import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import {
  budgetPercent,
  canEnable,
  canFallback,
  canRecapture,
  canRestore,
  formatMoney,
  formatResetCountdown,
  statusPresentation,
} from "./budgetFallbackViewModel";

const STATE_OPTIONS = [
  ["all", "全部状态"], ["NORMAL", "正常"], ["near", "接近限额"],
  ["FALLBACK_5_3", "5.3 兜底"], ["MANUAL_HOLD", "人工检查"],
];

export default function BudgetFallbackPage() {
  const [data, setData] = useState({ keys: [], fallback_health: null });
  const [selectedId, setSelectedId] = useState("");
  const [events, setEvents] = useState([]);
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const response = await api.listBudgetFallbackKeys();
      setData(response);
      setError("");
      setSelectedId((current) => current && response.keys?.some((row) => row.key_id === current)
        ? current : response.keys?.[0]?.key_id || "");
    } catch (e) {
      setError(e.message);
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(() => document.visibilityState === "visible" && load(true), 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!selectedId) return setEvents([]);
    api.getBudgetFallbackEvents(selectedId)
      .then((response) => setEvents(response.events || []))
      .catch(() => setEvents([]));
  }, [selectedId, data]);

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (data.keys || []).filter((row) => {
      const searchMatch = !needle || row.key_alias.toLowerCase().includes(needle);
      const stateMatch = stateFilter === "all"
        || (stateFilter === "near" && row.state === "NORMAL" && row.utilization_percent >= 90)
        || row.state === stateFilter;
      return searchMatch && stateMatch;
    });
  }, [data.keys, search, stateFilter]);

  const selected = (data.keys || []).find((row) => row.key_id === selectedId) || null;
  const summary = useMemo(() => ({
    eligible: data.keys?.filter((row) => row.eligible).length || 0,
    enabled: data.keys?.filter((row) => row.enabled).length || 0,
    near: data.keys?.filter((row) => row.state === "NORMAL" && row.utilization_percent >= 90).length || 0,
    fallback: data.keys?.filter((row) => row.state === "FALLBACK_5_3").length || 0,
    hold: data.keys?.filter((row) => row.state === "MANUAL_HOLD").length || 0,
  }), [data.keys]);

  const perform = async (label, fn, confirmText = "") => {
    if (confirmText && !window.confirm(confirmText)) return;
    setAction(label);
    try { await fn(); await load(true); }
    catch (e) { window.alert(`操作失败：${e.message}`); }
    finally { setAction(""); }
  };

  return <section className="budget-fallback-page">
    <header className="budget-hero">
      <div><p className="budget-kicker"><span /> LITELLM COST GUARD</p><h2>预算路由</h2>
        <p>Key 达到周期预算 98% 后，无感切换到隔离的零成本 GPT-5.3；周期重置后恢复原模型。</p></div>
      <div className={`budget-health ${data.fallback_health?.zero_cost ? "is-ok" : "is-bad"}`}>
        <span className="budget-health-dot" /><div><strong>{data.fallback_health?.zero_cost ? "5.3 零成本通道就绪" : "5.3 通道未就绪"}</strong>
          <small>{data.fallback_health?.deployment_count || 0} 个注册成员</small></div></div>
    </header>
    <div className="budget-summary">
      <Metric label="可启用" value={summary.eligible} tone="green" /><Metric label="已开启" value={summary.enabled} tone="white" />
      <Metric label="接近限额" value={summary.near} tone="amber" /><Metric label="5.3 兜底" value={summary.fallback} tone="blue" />
      <Metric label="人工检查" value={summary.hold} tone="red" />
    </div>
    <div className="budget-toolbar">
      <label className="budget-search"><span>⌕</span><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="搜索 Key 别名" /></label>
      <select value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>{STATE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
      <button type="button" onClick={() => load()} disabled={loading}>↻ 刷新</button>
    </div>
    {error && <div className="budget-error">无法加载 LiteLLM Key：{error}</div>}
    <div className="budget-layout">
      <div className="budget-table-wrap">
        <div className="budget-table-head"><span>Key / 策略</span><span>状态</span><span>周期消费</span><span>恢复</span><span>自动兜底</span></div>
        {loading && !data.keys?.length ? <div className="budget-empty">正在读取预算状态…</div> : null}
        {!loading && !rows.length ? <div className="budget-empty">没有符合条件的 Key</div> : null}
        {rows.map((row) => <BudgetRow key={row.key_id} row={row} active={row.key_id === selectedId}
          onSelect={() => setSelectedId(row.key_id)} disabled={Boolean(action)} onToggle={() => row.enabled
            ? perform("disable", () => api.disableBudgetFallback(row.key_id, true), "关闭策略并恢复原模型？")
            : perform("enable", () => api.enableBudgetFallback(row.key_id))} />)}
      </div>
      <DetailPanel row={selected} events={events} busy={Boolean(action)} onClose={() => setSelectedId("")} onAction={(kind) => {
        if (!selected) return;
        if (kind === "fallback") perform(kind, () => api.forceBudgetFallback(selected.key_id, "admin console"), "立即把这个 Key 切到 5.3？");
        if (kind === "restore") perform(kind, () => api.restoreBudgetFallback(selected.key_id, "admin console"), "立即恢复原模型和预算？");
        if (kind === "recapture") perform(kind, () => api.recaptureBudgetFallback(selected.key_id, "admin console"), "以当前 LiteLLM 配置覆盖原始快照？");
        if (kind === "pause") perform(kind, () => selected.automation_paused ? api.resumeBudgetFallback(selected.key_id) : api.pauseBudgetFallback(selected.key_id));
        if (kind === "disable-keep") perform(kind, () => api.disableBudgetFallback(selected.key_id, false), "关闭自动策略，但保持当前路由不恢复？之后需要人工处理恢复。此操作仅用于应急。" );
      }} />
    </div>
  </section>;
}

function Metric({ label, value, tone }) { return <div className={`budget-metric tone-${tone}`}><span>{label}</span><strong>{value}</strong></div>; }

function BudgetRow({ row, active, onSelect, onToggle, disabled }) {
  const status = statusPresentation(row); const percent = budgetPercent(row);
  return <article className={`budget-row ${active ? "is-active" : ""}`} onClick={onSelect} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && onSelect()}>
    <div className="budget-key-cell"><strong>{row.key_alias}</strong><small>{row.budget_duration || "无周期"} · 98% 切换</small></div>
    <div><span className={`budget-status tone-${status.tone}`}><i style={{ background: status.dot }} />{status.label}</span></div>
    <div className="budget-spend-cell"><div><strong>{formatMoney(row.spend)}</strong><span>/ {formatMoney(row.max_budget)}</span></div>
      <div className="budget-progress"><span style={{ width: `${Math.min(percent, 100)}%` }} className={percent >= 98 ? "danger" : percent >= 90 ? "warning" : ""} /></div><small>{percent.toFixed(1)}%</small></div>
    <div className="budget-reset-cell"><strong>{formatResetCountdown(row.budget_reset_at)}</strong><small>{row.budget_reset_at ? new Date(row.budget_reset_at).toLocaleString("zh-CN") : "--"}</small></div>
    <div className="budget-toggle-cell" onClick={(e) => e.stopPropagation()}><button type="button" role="switch" aria-checked={row.enabled} aria-label={`${row.key_alias} 预算兜底`}
      className={`budget-switch ${row.enabled ? "is-on" : ""}`} onClick={onToggle} disabled={disabled || (!row.enabled && !canEnable(row))} title={!row.eligible ? row.eligibility_reason : ""}><span /></button></div>
  </article>;
}

function DetailPanel({ row, events, busy, onClose, onAction }) {
  if (!row) return <aside className="budget-detail budget-detail-empty"><span>选择一个 Key 查看预算策略</span></aside>;
  const status = statusPresentation(row);
  return <aside className="budget-detail"><div className="budget-detail-title"><div><p>KEY POLICY</p><h3>{row.key_alias}</h3></div><button type="button" onClick={onClose} aria-label="关闭详情">×</button></div>
    <span className={`budget-status tone-${status.tone}`}><i style={{ background: status.dot }} />{status.label}</span>
    <dl className="budget-facts"><div><dt>周期消费</dt><dd>{formatMoney(row.spend)} / {formatMoney(row.max_budget)}</dd></div><div><dt>预算周期</dt><dd>{row.budget_duration || "--"}</dd></div>
      <div><dt>下次恢复</dt><dd>{formatResetCountdown(row.budget_reset_at)}</dd></div><div><dt>自动控制</dt><dd>{row.automation_paused ? "已暂停" : row.enabled ? "运行中" : "未开启"}</dd></div></dl>
    <div className="budget-route-map"><p>FALLBACK ROUTE</p><div><span>原公开模型名</span><b>→</b><strong>chatgpt-budget-fallback-gpt-5.3</strong></div><small>客户端无需更换 API Key 或修改模型名；fallback 期间付费内部模型不可直达。</small></div>
    {row.eligibility_reason && !row.eligible ? <div className="budget-note danger">{row.eligibility_reason}</div> : null}{row.last_error ? <div className="budget-note danger">{row.last_error}</div> : null}
    <div className="budget-actions"><button disabled={busy || !canFallback(row)} onClick={() => onAction("fallback")}>立即切到 5.3</button><button disabled={busy || !canRestore(row)} onClick={() => onAction("restore")}>恢复主模型</button>
      <button disabled={busy || !canRecapture(row)} onClick={() => onAction("recapture")}>重新采集配置</button><button disabled={busy || !row.enabled} onClick={() => onAction("pause")}>{row.automation_paused ? "恢复自动控制" : "暂停自动控制"}</button>
      {row.state !== "NORMAL" ? <button className="budget-danger-action" disabled={busy || !row.enabled} onClick={() => onAction("disable-keep")}>仅关闭策略，保持当前路由</button> : null}</div>
    <div className="budget-events"><div className="budget-section-label">最近事件</div>{!events.length ? <p className="budget-events-empty">还没有策略事件</p> : events.slice(0, 12).map((event) => <div className="budget-event" key={event.id}><i /><div><strong>{event.event_type}</strong><small>{event.created_at}</small></div></div>)}</div>
  </aside>;
}
