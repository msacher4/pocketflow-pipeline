<script>
  import { getSmoothStepPath } from '@xyflow/system';
  import { EdgeReconnectAnchor } from '@xyflow/svelte';

  let {
    id,
    sourceX, sourceY, targetX, targetY,
    sourcePosition, targetPosition,
    markerStart, markerEnd,
    style, label, labelStyle,
    selected, animated,
    interactionWidth = 20
  } = $props();

  let [path, labelX, labelY] = $derived(
    getSmoothStepPath({
      sourceX, sourceY, targetX, targetY,
      sourcePosition, targetPosition
    })
  );
</script>

<path
  {id}
  d={path}
  class="svelte-flow__edge-path"
  class:selected
  class:animated
  marker-start={markerStart}
  marker-end={markerEnd}
  fill="none"
  {style}
/>

{#if interactionWidth > 0}
  <path
    d={path}
    stroke-opacity={0}
    stroke-width={interactionWidth}
    fill="none"
    class="svelte-flow__edge-interaction"
  />
{/if}

{#if label}
  <text x={labelX} y={labelY} class="svelte-flow__edge-text" text-anchor="middle" dy={-10}>
    {label}
  </text>
{/if}

<EdgeReconnectAnchor type="source" position={{ x: sourceX, y: sourceY }} />
<EdgeReconnectAnchor type="target" position={{ x: targetX, y: targetY }} />
