const BASE = "/api";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

export const api = {
  listInstances: () => request("/instances"),
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
  startDeploy: (imageTag, mode = "normal") => request("/deploy", { method: "POST", body: JSON.stringify({ image_tag: imageTag, mode }) }),
  getDeployStatus: () => request("/deploy/status"),
  continueDeploy: () => request("/deploy/continue", { method: "POST" }),
  rollbackDeploy: () => request("/deploy/rollback", { method: "POST" }),
  abortDeploy: () => request("/deploy/abort", { method: "POST" }),
  getDeployHistory: (limit = 20) => request(`/deploy/history?limit=${limit}`),
  setDeployGroup: (uid, group) => request(`/instances/${uid}/deploy-group`, { method: "PUT", body: JSON.stringify({ group }) }),
  batchSetDeployGroup: (ids, group) => request("/instances/batch-deploy-group", { method: "POST", body: JSON.stringify({ ids, group }) }),
  // Deploy groups
  listDeployGroups: () => request("/deploy-groups"),
  createDeployGroup: (name, priority, description) => request("/deploy-groups", { method: "POST", body: JSON.stringify({ name, priority, description }) }),
  updateDeployGroup: (name, data) => request(`/deploy-groups/${name}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteDeployGroup: (name) => request(`/deploy-groups/${name}`, { method: "DELETE" }),
};
