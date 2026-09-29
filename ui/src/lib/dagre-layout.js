import dagre from '@dagrejs/dagre';

export function computeLayout(nodes, edges, opts = {}) {
  const {
    rankdir = 'LR',
    nodesep = 80,
    ranksep = 160,
    width = 150,
    height = 60,
  } = opts;

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir, nodesep, ranksep, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));

  nodes.forEach(n => {
    g.setNode(n.id, { width, height });
  });

  edges.forEach(e => {
    if (g.hasNode(e.source) && g.hasNode(e.target)) {
      g.setEdge(e.source, e.target);
    }
  });

  dagre.layout(g);

  return nodes.map(n => {
    const pos = g.node(n.id);
    return {
      ...n,
      position: {
        x: (pos?.x || 0) - width / 2,
        y: (pos?.y || 0) - height / 2,
      },
    };
  });
}
