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
};
