export function budgetPercent(row) {
  const budget = Number(row?.max_budget || 0);
  return budget > 0 ? (Number(row?.spend || 0) / budget) * 100 : 0;
}

const STATUS = {
  NORMAL: { label: "正常", tone: "neutral", dot: "#16a36a" },
  FALLBACK_PENDING: { label: "切换中", tone: "cyan", dot: "#22d3ee" },
  FALLBACK_5_3: { label: "5.3 兜底", tone: "blue", dot: "#3b82f6" },
  RESTORING: { label: "恢复中", tone: "cyan", dot: "#22d3ee" },
  MANUAL_HOLD: { label: "人工检查", tone: "danger", dot: "#ef4444" },
};

export function statusPresentation(row) {
  if ((row?.state || "NORMAL") === "NORMAL" && Number(row?.utilization_percent || 0) >= 90) {
    return { label: "接近限额", tone: "warning", dot: "#f59e0b" };
  }
  return STATUS[row?.state] || STATUS.NORMAL;
}

export function canEnable(row) {
  return Boolean(row?.eligible) && !row?.enabled;
}

export function formatResetCountdown(resetAt, now = new Date()) {
  if (!resetAt) return "未提供";
  const target = new Date(resetAt);
  if (Number.isNaN(target.getTime())) return "时间无效";
  const milliseconds = target.getTime() - now.getTime();
  if (milliseconds <= 0) return "等待恢复";
  const totalMinutes = Math.ceil(milliseconds / 60000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分`;
}

export function formatMoney(value) {
  return value == null ? "--" : `$${Number(value).toFixed(2)}`;
}
