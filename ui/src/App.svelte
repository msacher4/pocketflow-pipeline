<script>
  import { tick } from 'svelte';
  import { SvelteFlow, Background, Controls, MiniMap } from '@xyflow/svelte';
  import StepNode from './lib/nodes/StepNode.svelte';
  import CustomStepEdge from './lib/nodes/CustomStepEdge.svelte';
  import { computeLayout } from './lib/dagre-layout.js';
  import { fetchState, fetchFlow, fetchSubShared, fetchTraces, fetchShared, fetchPositions, savePositions as apiSavePositions, triggerPipeline, triggerTestPipeline, cancelPipeline } from './lib/api.js';
  import { NODE_TYPES } from './lib/pipeline-data.js';

  const nodeTypes = { step: StepNode };
  const edgeTypes = { smoothstep: CustomStepEdge };

  let nodes = $state([]);
  let edges = $state([]);
  let allNodeTypes = $state({});
  let showModal = $state(false);
  let modalContent = $state({ title: '', html: '' });
  let showSharedStore = $state(false);
  let sharedStoreData = $state({});
  let selectedEdgeId = $state(null);
  let topic = $state('');
  let pipelineRunning = $state(false);
  let actionMsg = $state('');
  let runSteps = $state([]);

  function resolveStatus(stepId, state) {
    const stepStatus = {};
    for (const s of (state.steps || [])) {
      stepStatus[s.step] = s.status;
    }
    const backendStep = stepId === 'vf' ? 'viralfinder' : stepId;
    const current = state.current_step || '';
    const running = state.pipeline_running;
    if (current === backendStep && running) return 'active';
    const st = stepStatus[backendStep];
    if (st === 'ok' || st === 'approve') return 'completed';
    if (st === 'error' || st === 'reject') return 'failed';
    return 'pending';
  }

  async function init() {
    const [state, flow, saved] = await Promise.all([fetchState(), fetchFlow(), fetchPositions()]);
    runSteps = state.steps || [];
    console.log('[viz] flow:', flow.nodes.length, 'nodes,', flow.edges.length, 'edges');
    console.log('[viz] saved positions:', saved.nodes ? Object.keys(saved.nodes).length : 0, 'nodes');
    // Backend nodeTypes = source de vérité ; local NODE_TYPES = fallback offline
    allNodeTypes = { ...NODE_TYPES, ...flow.nodeTypes };
    const laid = computeLayout(
      flow.nodes.map(n => ({ ...n, position: { x: 0, y: 0 }, data: { stepId: n.stepId } })),
      flow.edges,
      { rankdir: 'LR', nodesep: 60, ranksep: 160 }
    );
    nodes = laid.map(n => ({
      ...n,
      position: saved.nodes?.[n.id] || n.position,
      data: {
        ...n.data,
        ...allNodeTypes[n.data.stepId],
        status: resolveStatus(n.data.stepId, state),
      },
    }));
    edges = flow.edges.map(e => ({
      ...e,
      ...saved.edges?.[e.id],
      type: 'smoothstep',
      animated: !!e.label,
    }));
    await tick();
  }

  async function syncStructure() {
    // Auto-update de la structure : si le backend a changé de graphe (nouveaux
    // nœuds/arêtes), on régénère nodes/edges sans perdre les positions connues.
    const [state, flow] = await Promise.all([fetchState(), fetchFlow()]);
    const existing = new Set(nodes.map(n => n.id));
    const newIds = flow.nodes.map(n => n.id);
    const changed = newIds.length !== nodes.length || newIds.some(id => !existing.has(id));
    if (!changed) return;

    // Mettre à jour allNodeTypes si le backend a de nouveaux nœuds
    allNodeTypes = { ...NODE_TYPES, ...flow.nodeTypes };

    const saved = await fetchPositions();
    const laid = computeLayout(
      flow.nodes.map(n => ({ ...n, position: { x: 0, y: 0 }, data: { stepId: n.stepId } })),
      flow.edges,
      { rankdir: 'LR', nodesep: 60, ranksep: 160 }
    );
    nodes = laid.map(n => ({
      ...n,
      position: saved.nodes?.[n.id] || n.position,
      data: {
        ...n.data,
        ...allNodeTypes[n.data.stepId],
        status: resolveStatus(n.data.stepId, state),
      },
    }));
    edges = flow.edges.map(e => ({
      ...e,
      ...saved.edges?.[e.id],
      type: 'smoothstep',
      animated: !!e.label,
    }));
    await tick();
  }

  function resetLayout() {
    apiSavePositions({ nodes: {}, edges: {} });
    init();
  }

  async function refresh() {
    try {
      const state = await fetchState();
      runSteps = state.steps || [];
      pipelineRunning = !!state.pipeline_running;
      nodes = nodes.map(n => {
        const newStatus = resolveStatus(n.data.stepId, state);
        return n.data.status === newStatus ? n : { ...n, data: { ...n.data, status: newStatus } };
      });
    } catch (e) {
      console.error('Poll error:', e);
    }
  }

  async function saveCurrentPositions() {
    const nodeData = {};
    for (const n of nodes) {
      nodeData[n.id] = { x: Math.round(n.position.x), y: Math.round(n.position.y) };
    }
    const edgeData = {};
    for (const e of edges) {
      const entry = {};
      if (e.sourceHandle) entry.sourceHandle = e.sourceHandle;
      if (e.targetHandle) entry.targetHandle = e.targetHandle;
      if (e.source) entry.source = e.source;
      if (e.target) entry.target = e.target;
      if (Object.keys(entry).length > 0) edgeData[e.id] = entry;
    }
    await apiSavePositions({ nodes: nodeData, edges: edgeData });
    alert('✅ Positions sauvegardées');
  }

  async function handleTrigger() {
    if (!topic.trim()) {
      actionMsg = '⚠️ Entrez un mot-clé';
      return;
    }
    actionMsg = '';
    const res = await triggerPipeline(topic);
    pipelineRunning = true;
    actionMsg = res?.message || 'Pipeline déclenché';
    setTimeout(() => { actionMsg = ''; }, 5000);
  }

  async function handleTriggerTest() {
    if (!topic.trim()) {
      actionMsg = '⚠️ Entrez un mot-clé';
      return;
    }
    actionMsg = '';
    const res = await triggerTestPipeline(topic);
    pipelineRunning = true;
    actionMsg = res?.message || 'Pipeline test déclenché';
    setTimeout(() => { actionMsg = ''; }, 5000);
  }

  async function handleStop() {
    actionMsg = '';
    const res = await cancelPipeline();
    pipelineRunning = false;
    actionMsg = res?.message || 'Pipeline arrêté';
    setTimeout(() => { actionMsg = ''; }, 5000);
  }

  function bubble(icon, label, content, color = 'info') {
    return `<div class="pf-bubble pf-bubble-${color}">
      <div class="pf-bubble-header"><span class="icon">${icon}</span> ${label}</div>
      <div class="pf-bubble-content">${content}</div>
    </div>`;
  }

  function bubblePre(icon, label, text, color = 'neutral') {
    const safe = (text || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return bubble(icon, label, `<pre>${safe}</pre>`, color);
  }

  function bubbleToggle(icon, label, summary, details, color = 'neutral') {
    const safeSummary = (summary || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const safeDetails = (details || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<div class="pf-bubble pf-bubble-${color}">
      <div class="pf-bubble-header"><span class="icon">${icon}</span> ${label}</div>
      <div class="pf-bubble-content">
        <div>${safeSummary}</div>
        <button class="pf-bubble-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.textContent=this.textContent.includes('Afficher')?'Masquer':'Afficher les détails ▾'">Afficher les détails ▾</button>
        <pre style="display:none">${safeDetails}</pre>
      </div>
    </div>`;
  }

  const HIST_COLOR = {
    ok: 'success', approve: 'success', success: 'success', done: 'success',
    error: 'error', failed: 'error', reject: 'error',
    reformat: 'warn', refix: 'warn',
    auto_repair: 'info', running: 'info', active: 'info',
  };

  function histSummary(text) {
    const m = String(text || '').match(/Value error,?\s*([^\n]+)/);
    if (m) return m[1].trim();
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  function runHistoryHtml(stepId) {
    const backendStep = stepId === 'vf' ? 'viralfinder' : stepId;
    const evts = (runSteps || []).filter(s => s.step === backendStep);
    if (evts.length === 0) return '';
    const n = evts.length;
    const body = evts.map((e, i) => {
      const label = `[${i + 1}/${n}] ${String(e.ts || '').slice(11, 19)} — ${e.status || 'event'}`
        + (e.attempt != null ? ` · tentative ${e.attempt}` : '');
      const color = HIST_COLOR[e.status] || 'neutral';
      const detail = e.error || e.output || '';
      if (detail) {
        return bubbleToggle('⚠️', label, histSummary(detail).slice(0, 220), detail, color);
      }
      return bubble('•', label, '<div>—</div>', color);
    }).join('');
    return bubble('📜', `Historique du run — ${n} événement${n > 1 ? 's' : ''}`, body, 'info');
  }

  async function openModal(stepId) {
    let title = allNodeTypes[stepId]?.label || stepId;
    let html = runHistoryHtml(stepId);

    if (stepId === 'validate_vf') {
      title = '👤 Validation ViralFinder';
      const [sub, traces] = await Promise.all([
        fetchSubShared('viralfinder'),
        fetchTraces('tikhub_search'),
      ]);
      const keyword = extractKeyword(traces);
      const sv = sub.selected_video || {};
      const url = typeof sv === 'string' ? sv : (sv.url || '');
      const desc = typeof sv === 'object' ? (sv.description || '') : '';

      if (keyword) html += bubblePre('🔍', 'Keyword recherché', keyword, 'info');
      if (url) {
        const meta = desc ? `<div style="margin-bottom:4px">${desc}</div>` : '';
        html += bubble('📊', 'Vidéo sélectionnée', `${meta}<pre>${url}</pre>`, 'success');
      }
      if (sub.video_feedback) html += bubblePre('🟠', 'Feedback utilisateur', sub.video_feedback, 'warn');
      if (!keyword && !url && !sub.video_feedback) html += `<div class="pf-empty">Aucune donnée pour le moment</div>`;

    } else if (stepId === 'validate_sw') {
      title = '✍️ Validation ScriptWriter';
      const sub = await fetchSubShared('scriptwriter');
      const sv = sub.selected_video || {};
      const url = typeof sv === 'string' ? sv : (sv.url || '');
      const topic = sub.topic || '';
      const script = sub.script || '';

      if (topic) html += bubblePre('📌', 'Topic', topic, 'info');
      if (url) html += bubblePre('🔗', 'URL source', url, 'info');
      if (script) {
        if (script.length > 300) {
          html += bubbleToggle('📝', 'Script', script.slice(0, 300) + '…', script, 'success');
        } else {
          html += bubblePre('📝', 'Script', script, 'success');
        }
      }
      if (sub.script_feedback) html += bubblePre('🟠', 'Feedback utilisateur', sub.script_feedback, 'warn');
      if (!topic && !script && !sub.script_feedback) html += `<div class="pf-empty">Aucune donnée pour le moment</div>`;

    } else if (stepId === 'validate_af') {
      title = '🎨 Validation AssetFinder';
      const sub = await fetchSubShared('assetfinder');
      const topic = sub.topic || '';
      const script = sub.script || '';
      const assets = sub.assets || '';

      if (topic) html += bubblePre('📌', 'Topic', topic, 'info');
      if (script) {
        if (script.length > 300) {
          html += bubbleToggle('📝', 'Script', script.slice(0, 300) + '…', script, 'neutral');
        } else {
          html += bubblePre('📝', 'Script', script, 'neutral');
        }
      }
      if (assets) {
        if (assets.length > 300) {
          html += bubbleToggle('🎨', 'Plan de montage', assets.slice(0, 300) + '…', assets, 'success');
        } else {
          html += bubblePre('🎨', 'Plan de montage', assets, 'success');
        }
      }
      if (sub.af_feedback) html += bubblePre('🟠', 'Feedback utilisateur', sub.af_feedback, 'warn');
      if (!topic && !assets && !sub.af_feedback) html += `<div class="pf-empty">Aucune donnée pour le moment</div>`;

    } else if (stepId === 'vf') {
      const sub = await fetchSubShared('viralfinder');
      const keys = Object.keys(sub);
      if (keys.length === 0) {
        html += `<div class="pf-empty">Shared store vide</div>`;
      } else {
        for (const k of keys) {
          const v = sub[k];
          const text = typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
          const truncated = text.length > 300 ? text.slice(0, 300) + '…' : text;
          if (text.length > 300) {
            html += bubbleToggle('📌', k, truncated, text, 'neutral');
          } else {
            html += bubblePre('📌', k, text, 'neutral');
          }
        }
      }

    } else if (['tikhub_search', 'tiktok_analyst', 'video_analysis_llm', 'script_generator', 'asset_planner', 'montage_planner', 'montage_critic', 'videoeditor_planner'].includes(stepId)) {
      const traces = await fetchTraces(stepId);
      const mgr = traces.manager || {};
      const agent = traces.agent || {};
      const phases = Object.keys(mgr);
      if (phases.length === 0) {
        html += `<div class="pf-empty">Aucune trace LLM pour cette étape</div>`;
      } else {
        for (const phase of phases) {
          const d = mgr[phase];
          const model = d.model ? `<div style="font-size:10px;color:#656d76;margin-bottom:4px">Modèle: ${d.model}</div>` : '';
          let content = model;
          if (d.user_msg_preview) content += `<pre>${d.user_msg_preview}</pre>`;
          if (d.response_preview) content += `<pre>${d.response_preview}</pre>`;
          html += bubble('🧠', phase, content, 'purple');
        }
        for (const [k, v] of Object.entries(agent)) {
          if (typeof v === 'string') html += bubblePre('🤖', k, v, 'purple');
        }
      }

    } else if (stepId === 'reset') {
      const shared = await fetchShared();
      const keys = Object.keys(shared);
      if (keys.length === 0) {
        html += `<div class="pf-empty">Aucune donnée (shared global vide)</div>`;
      } else {
        for (const k of keys) {
          const v = shared[k];
          const text = typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
          const truncated = text.length > 300 ? text.slice(0, 300) + '…' : text;
          if (text.length > 300) html += bubbleToggle('📌', k, truncated, text, 'info');
          else html += bubblePre('📌', k, text, 'info');
        }
      }

    } else {
      const sub = await fetchSubShared(stepId);
      const keys = Object.keys(sub);
      if (keys.length === 0) {
        html += `<div class="pf-empty">Aucune donnée pour cette étape</div>`;
      } else {
        for (const k of keys) {
          const v = sub[k];
          const text = typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v);
          const truncated = text.length > 300 ? text.slice(0, 300) + '…' : text;
          if (text.length > 300) {
            html += bubbleToggle('📦', k, truncated, text, 'neutral');
          } else {
            html += bubblePre('📦', k, text, 'neutral');
          }
        }
      }
    }

    modalContent = { title, html };
    showModal = true;
  }

  function extractKeyword(traces) {
    try {
      const mgr = traces.manager || {};
      for (const k of Object.keys(mgr).sort().reverse()) {
        const resp = mgr[k]?.response_preview || '';
        if (resp) {
          const cleaned = resp.replace(/```json\s*|\s*```/g, '').trim();
          return JSON.parse(cleaned).keyword || '';
        }
      }
    } catch {}
    return '';
  }

  function close() { showModal = false; showSharedStore = false; }

  function openSharedStore() {
    showSharedStore = true;
    fetchShared().then(data => {
      sharedStoreData = data;
    });
  }

  function handleNodeClick(detail) {
    const node = detail?.node;
    if (node?.data?.stepId) {
      openModal(node.data.stepId);
    }
  }

  function handleNodeDragStop(detail) {
    const dragged = detail?.nodes || [];
    if (dragged.length === 0) return;
    nodes = nodes.map(n => {
      const d = dragged.find(x => x.id === n.id);
      return d ? { ...n, position: { ...d.position } } : n;
    });
  }

  function handleEdgeClick(detail) {
    const edge = detail?.edge;
    if (!edge) return;
    selectedEdgeId = selectedEdgeId === edge.id ? null : edge.id;
  }

  function handleReconnect(oldEdge, newConnection) {
    edges = edges.map(e => {
      if (e.id === oldEdge.id) {
        return {
          ...e,
          source: newConnection.source,
          target: newConnection.target,
          sourceHandle: newConnection.sourceHandle,
          targetHandle: newConnection.targetHandle,
        };
      }
      return e;
    });
    selectedEdgeId = null;
  }

  function handlePaneClick() {
    selectedEdgeId = null;
  }

  $effect(() => {
    init();
    const iv = setInterval(refresh, 2000);
    const ivStruct = setInterval(() => { syncStructure().catch(() => {}); }, 10000);
    return () => { clearInterval(iv); clearInterval(ivStruct); };
  });
</script>

<div style="width:100%;height:100%;position:relative">
  <SvelteFlow
    bind:nodes={nodes}
    bind:edges={edges}
    {nodeTypes}
    {edgeTypes}
    fitView
    proOptions={{ hideAttribution: true }}
    onnodeclick={handleNodeClick}
    onnodedragstop={handleNodeDragStop}
    onedgeclick={handleEdgeClick}
    onreconnect={handleReconnect}
    onpaneclick={handlePaneClick}
    nodesDraggable={true}
    nodesConnectable={true}
    edgesSelectable={true}
    defaultEdgeOptions={{
      type: 'smoothstep',
    }}
  >
    <Background />
    <Controls />
    <MiniMap />
  </SvelteFlow>

  <div class="pf-toolbar">
    <input
      type="text"
      class="pf-topic-input"
      bind:value={topic}
      placeholder="Thème vidéo (ex: motivation fitness)"
      onkeydown={(e) => e.key === 'Enter' && !pipelineRunning && handleTrigger()}
    />
    <button class="pf-trigger-btn" onclick={handleTrigger} disabled={pipelineRunning} title="Lancer le pipeline">
      ▶ Trigger
    </button>
    <button class="pf-trigger-test-btn" onclick={handleTriggerTest} disabled={pipelineRunning} title="Run de test : chemin alt + validations Telegram auto-approuvées">
      🧪 Trigger Test
    </button>
    <button class="pf-stop-btn" onclick={handleStop} disabled={!pipelineRunning} title="Arrêter le pipeline">
      ⏹ Stop
    </button>
    {#if actionMsg}
      <span class="pf-action-msg">{actionMsg}</span>
    {/if}
  </div>

  <div class="pf-toolbar pf-toolbar-bottom">
    <button class="pf-shared-btn" onclick={saveCurrentPositions} title="Sauvegarder les positions des nœuds et des edges">
      💾 Sauvegarder
    </button>
    <button class="pf-shared-btn" onclick={openSharedStore} title="Voir le Shared Store global">
      📦 Shared Store
    </button>
    <button class="pf-reset-btn" onclick={resetLayout} title="Réinitialiser la disposition du graphe">
      🔄 Reset layout
    </button>
  </div>

  {#if showModal}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="pf-modal-overlay" onclick={close} role="dialog" tabindex="-1" onkeydown={(e) => e.key === 'Escape' && close()}>
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div class="pf-modal" onclick={(e) => e.stopPropagation()} role="document" tabindex="-1" onkeydown={(e) => e.key === 'Escape' && close()}>
        <div class="pf-modal-header">
          <span>{modalContent.title}</span>
          <button class="pf-modal-close" onclick={close}>&times;</button>
        </div>
        <div class="pf-modal-body">
          {@html modalContent.html}
        </div>
      </div>
    </div>
  {/if}

  {#if showSharedStore}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="pf-modal-overlay" onclick={close} role="dialog" tabindex="-1" onkeydown={(e) => e.key === 'Escape' && close()}>
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <div class="pf-modal" onclick={(e) => e.stopPropagation()} role="document" tabindex="-1" onkeydown={(e) => e.key === 'Escape' && close()}>
        <div class="pf-modal-header">
          <span>📦 Shared Store Global</span>
          <button class="pf-modal-close" onclick={close}>&times;</button>
        </div>
        <div class="pf-modal-body">
          {#each Object.entries(sharedStoreData) as [k, v]}
            {@const text = typeof v === 'object' ? JSON.stringify(v, null, 2) : String(v)}
            {@const truncated = text.length > 400 ? text.slice(0, 400) + '…' : text}
            <div class="pf-bubble pf-bubble-neutral">
              <div class="pf-bubble-header"><span class="icon">📌</span> {k}</div>
              <div class="pf-bubble-content">
                {#if text.length > 400}
                  <pre>{truncated}</pre>
                  <button class="pf-bubble-toggle" onclick={(e) => {
                    const pre = e.target.previousElementSibling;
                    if (pre.style.maxHeight === 'none') {
                      pre.style.maxHeight = '200px';
                      pre.textContent = truncated;
                      e.target.textContent = 'Afficher tout ▾';
                    } else {
                      pre.style.maxHeight = 'none';
                      pre.textContent = text;
                      e.target.textContent = 'Réduire ▴';
                    }
                  }}>Afficher tout ▾</button>
                {:else}
                  <pre>{text}</pre>
                {/if}
              </div>
            </div>
          {/each}
          {#if Object.keys(sharedStoreData).length === 0}
            <div class="pf-empty">Shared store vide</div>
          {/if}
        </div>
      </div>
    </div>
  {/if}
</div>

<style>
</style>
