const BASE = "/api";
const REQUEST_TIMEOUT_MS = 30000;

function getToken() {
  return localStorage.getItem("carher_token") || "";
}

let onAuthFailure = null;

export function setAuthFailureHandler(handler) {
  onAuthFailure = handler;
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  const token = getToken();
  const headers = { "Content-Type": "application/json", ...options.headers };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  try {
    const res = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
      signal: controller.signal,
    });
    if (res.status === 401) {
      localStorage.removeItem("carher_token");
      localStorage.removeItem("carher_user");
      if (onAuthFailure) onAuthFailure();
      throw new Error("登录已过期，请重新登录");
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
      return {};
    }
    return res.json();
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  listInstances: async () => {
    const data = await request("/instances");
    return Array.isArray(data) ? data : (data.instances || []);
  },
  getInstance: (id) => request(`/instances/${id}`),
  addInstance: (data) => request("/instances", { method: "POST", body: JSON.stringify(data) }),
  batchImport: (instances) => request("/instances/batch-import", { method: "POST", body: JSON.stringify({ instances }) }),
  batchAction: (ids, action, params) => request("/instances/batch", { method: "POST", body: JSON.stringify({ ids, action, params }) }),
  stopInstance: (id) => request(`/instances/${id}/stop`, { method: "POST" }),
  startInstance: (id) => request(`/instances/${id}/start`, { method: "POST" }),
  restartInstance: (id) => request(`/instances/${id}/restart`, { method: "POST" }),
  updateInstance: (id, data) => request(`/instances/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteInstance: (id, purge = false) => request(`/instances/${id}?purge=${purge}`, { method: "DELETE" }),
  getLogs: (id, tail = 200) => request(`/instances/${id}/logs?tail=${tail}`),
  getStatus: () => request("/status"),
  getHealth: () => request("/health"),
  getNextId: () => request("/next-id"),
  // Sync & Admin
  forceSync: () => request("/sync/force", { method: "POST" }),
  consistencyCheck: () => request("/sync/check"),
  getAuditLog: (instanceId, limit = 50) => request(`/audit?${instanceId ? `instance_id=${instanceId}&` : ""}limit=${limit}`),
  importFromK8s: () => request("/import-from-k8s", { method: "POST" }),
  // Deploy pipeline
  startDeploy: (imageTag, mode = "normal", force = false) => request("/deploy", { method: "POST", body: JSON.stringify({ image_tag: imageTag, mode, force }) }),
  getDeployStatus: () => request("/deploy/status"),
  continueDeploy: () => request("/deploy/continue", { method: "POST" }),
  rollbackDeploy: () => request("/deploy/rollback", { method: "POST" }),
  abortDeploy: () => request("/deploy/abort", { method: "POST" }),
  getDeployHistory: (limit = 20) => request(`/deploy/history?limit=${limit}`),
  setDeployGroup: (uid, group) => request(`/instances/${uid}/deploy-group`, { method: "PUT", body: JSON.stringify({ group }) }),
  batchSetDeployGroup: (ids, group) => request("/instances/batch-deploy-group", { method: "POST", body: JSON.stringify({ ids, group }) }),
  // Metrics
  getMetricsOverview: () => request("/metrics/overview"),
  getMetricsNodes: () => request("/metrics/nodes"),
  getMetricsPods: () => request("/metrics/pods"),
  getInstanceMetrics: (id) => request(`/instances/${id}/metrics`),
  getInstanceMetricsHistory: (id, hours = 24) => request(`/instances/${id}/metrics/history?hours=${hours}`),
  getNodeMetricsHistory: (hours = 24) => request(`/metrics/history/nodes?hours=${hours}`),
  getMetricsStorage: () => request("/metrics/storage"),
  // Deploy groups
  listDeployGroups: () => request("/deploy-groups"),
  createDeployGroup: (name, priority, description) => request("/deploy-groups", { method: "POST", body: JSON.stringify({ name, priority, description }) }),
  updateDeployGroup: (name, data) => request(`/deploy-groups/${name}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteDeployGroup: (name) => request(`/deploy-groups/${name}`, { method: "DELETE" }),
  // Branch rules (CI/CD)
  listBranchRules: () => request("/branch-rules"),
  createBranchRule: (data) => request("/branch-rules", { method: "POST", body: JSON.stringify(data) }),
  updateBranchRule: (id, data) => request(`/branch-rules/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteBranchRule: (id) => request(`/branch-rules/${id}`, { method: "DELETE" }),
  testBranchRule: (branch) => request(`/branch-rules/test?branch=${encodeURIComponent(branch)}`),
  triggerBuild: (data) => request("/ci/trigger-build", { method: "POST", body: JSON.stringify(data) }),
  listBranches: (repo) => request(`/ci/branches?repo=${encodeURIComponent(repo)}`),
  listWorkflows: (repo) => request(`/ci/workflows?repo=${encodeURIComponent(repo)}`),
  listRuns: (repo = "", perPage = 10) => request(`/ci/runs?repo=${encodeURIComponent(repo)}&per_page=${perPage}`),
  // Settings
  getSettings: () => request("/settings"),
  updateSettings: (updates) => request("/settings", { method: "PUT", body: JSON.stringify(updates) }),
  getRepos: () => request("/settings/repos"),
};
