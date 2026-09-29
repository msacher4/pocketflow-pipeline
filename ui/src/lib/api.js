const BASE = '';

export async function fetchState() {
  const r = await fetch(`${BASE}/api/pocketflow/json/state`);
  return r.ok ? r.json() : {};
}

export async function fetchFlow() {
  const r = await fetch(`${BASE}/api/pocketflow/json/flow`);
  if (!r.ok) return { nodes: [], edges: [], nodeTypes: {} };
  const data = await r.json();
  return {
    nodes: data.nodes || [],
    edges: data.edges || [],
    nodeTypes: data.nodeTypes || {},
  };
}

export async function fetchSubShared(name) {
  const r = await fetch(`${BASE}/api/pocketflow/json/sub/${name}`);
  return r.ok ? r.json() : {};
}

export async function fetchTraces(step) {
  const r = await fetch(`${BASE}/api/pocketflow/json/traces/${step}`);
  return r.ok ? r.json() : {};
}

export async function fetchShared() {
  const r = await fetch(`${BASE}/api/pocketflow/json/shared`);
  return r.ok ? r.json() : {};
}

export async function triggerPipeline(topic) {
  const r = await fetch(`${BASE}/api/pocketflow/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ topic }),
  });
  return r.json();
}

export async function cancelPipeline() {
  const r = await fetch(`${BASE}/api/pocketflow/cancel`, { method: 'POST' });
  return r.json();
}

export async function fetchPositions() {
  try {
    const r = await fetch(`${BASE}/api/pocketflow/positions`);
    return r.ok ? r.json() : { nodes: {}, edges: {} };
  } catch { return { nodes: {}, edges: {} }; }
}

export async function savePositions(data) {
  try {
    await fetch(`${BASE}/api/pocketflow/positions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
  } catch (e) { console.error('Failed to save positions:', e); }
}
