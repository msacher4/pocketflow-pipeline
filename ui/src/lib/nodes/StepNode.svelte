<script>
  import { Handle, Position } from '@xyflow/svelte';

  let { data } = $props();

  let icon = $derived(data.icon || '🔍');
  let label = $derived(data.label || '?');
  let model = $derived(data.model || '');
  let status = $derived(data.status || 'pending');
</script>

<div class="pf-node {status}" role="button" tabindex="0">
  <Handle type="target" position={Position.Top} id="target-top" />
  <Handle type="target" position={Position.Right} id="target-right" />
  <Handle type="target" position={Position.Bottom} id="target-bottom" />
  <Handle type="target" position={Position.Left} id="target-left" />

  <Handle type="source" position={Position.Top} id="source-top" />
  <Handle type="source" position={Position.Right} id="source-right" />
  <Handle type="source" position={Position.Bottom} id="source-bottom" />
  <Handle type="source" position={Position.Left} id="source-left" />

  <div class="pf-node-header">
    <span class="pf-node-icon">{icon}</span>
    <span class="pf-node-label">{label}</span>
  </div>
  {#if model}
    <span class="pf-node-badge">{model}</span>
  {/if}
  <div class="pf-node-status {status}">
    {#if status === 'completed'}✓ Effectué
    {:else if status === 'active'}⟳ En cours…
    {:else if status === 'failed'}✕ Échec
    {:else}○ En attente
    {/if}
  </div>
</div>

<style>
.pf-node {
    background: #ffffff;
    border: 1px solid #d0d7de;
    border-radius: 12px;
    padding: 14px 18px;
    min-width: 130px;
    max-width: 220px;
    text-align: center;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.06);
    transition: box-shadow 0.2s, border-color 0.2s, transform 0.15s;
    position: relative;
    cursor: grab;
  }

  .pf-node:hover {
    box-shadow: 0 4px 12px rgba(0,0,0,0.10);
    border-color: #0969da;
    transform: translateY(-1px);
  }

  .pf-node-header {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }

  .pf-node-icon { font-size: 24px; }

  .pf-node-label {
    font-size: 12px;
    font-weight: 600;
    color: #1f2328;
    line-height: 1.3;
  }

  .pf-node-badge {
    display: inline-block;
    font-size: 9px;
    background: #ddf4ff;
    color: #0550ae;
    border-radius: 8px;
    padding: 1px 6px;
    margin-top: 4px;
  }

  .pf-node-status {
    font-size: 10px;
    color: #656d76;
    margin-top: 4px;
  }

  .pf-node-status.pending { color: #8b949e; }
  .pf-node-status.active { color: #0969da; }
  .pf-node-status.completed { color: #1a7f37; }
  .pf-node-status.failed { color: #cf222e; }

  .pf-node.active {
    border-color: #0969da;
    box-shadow: 0 0 0 2px #0969da33;
  }

  .pf-node.completed {
    border-color: #1a7f37;
  }

  .pf-node.failed {
    border-color: #cf222e;
  }
</style>
