import test from "node:test";
import assert from "node:assert/strict";

import {
  budgetPercent,
  canEnable,
  canFallback,
  canRecapture,
  canRestore,
  formatResetCountdown,
  statusPresentation,
} from "./budgetFallbackViewModel.js";


test("budgetPercent handles missing and over-budget values", () => {
  assert.equal(budgetPercent({ spend: 98, max_budget: 100 }), 98);
  assert.equal(budgetPercent({ spend: 120, max_budget: 100 }), 120);
  assert.equal(budgetPercent({ spend: 10, max_budget: null }), 0);
});


test("near-limit normal keys use the warning presentation", () => {
  assert.equal(
    statusPresentation({ state: "NORMAL", utilization_percent: 93 }).label,
    "接近限额",
  );
  assert.equal(statusPresentation({ state: "FALLBACK_5_3" }).label, "5.3 兜底");
});


test("all controller states have stable labels", () => {
  assert.equal(statusPresentation({ state: "FALLBACK_PENDING" }).label, "切换中");
  assert.equal(statusPresentation({ state: "RESTORING" }).label, "恢复中");
  assert.equal(statusPresentation({ state: "MANUAL_HOLD" }).label, "人工检查");
});


test("enablement requires backend eligibility and disabled policy", () => {
  assert.equal(canEnable({ eligible: true, enabled: false }), true);
  assert.equal(canEnable({ eligible: false, enabled: false }), false);
  assert.equal(canEnable({ eligible: true, enabled: true }), false);
});


test("manual actions follow controller state guards", () => {
  assert.equal(canFallback({ enabled: true, state: "NORMAL" }), true);
  assert.equal(canFallback({ enabled: true, state: "FALLBACK_5_3" }), false);
  assert.equal(canRestore({ enabled: true, state: "NORMAL" }), false);
  assert.equal(canRestore({ enabled: true, state: "FALLBACK_5_3" }), true);
  assert.equal(canRecapture({ enabled: true, state: "NORMAL" }), true);
  assert.equal(canRecapture({ enabled: true, state: "MANUAL_HOLD" }), true);
  assert.equal(canRecapture({ enabled: true, state: "FALLBACK_5_3" }), false);
});


test("countdown never renders a negative duration", () => {
  assert.equal(
    formatResetCountdown(
      "2026-07-13T00:00:00Z",
      new Date("2026-07-14T00:00:00Z"),
    ),
    "等待恢复",
  );
});


test("countdown formats hours and minutes", () => {
  assert.equal(
    formatResetCountdown(
      "2026-07-14T02:30:00Z",
      new Date("2026-07-14T00:00:00Z"),
    ),
    "2 小时 30 分",
  );
});
