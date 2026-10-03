// Chain layout for the topology page: plain JS (no JSX, no assets) so it is unit-tested with node:test.
// Everything comes from the snapshot's `topology`, so any number of hospitals draws without code changes.

export const W = 1000;
export const H = 470;
const TILTS = [-6, 5, -5, 7, -7];

// Chain members in chain order, joined to their node rows. A member that has not registered a row yet
// (it only just joined) still gets a placeholder, so the chain is never drawn with a gap.
export function chainMembers(topology, nodes) {
  const byId = Object.fromEntries((nodes || []).map((n) => [n.node_id, n]));
  return (topology?.chain || []).map((id, i) => ({
    ...(byId[id] || { node_id: id, display_name: id, patients: null, status: "down", current_round: 0 }),
    position: i + 1,
    role: (i + 1) % 2 === 1 ? "head" : "tail",
  }));
}

// Lay members out along a gentle arc. Up to three keep the original large art; beyond that every node
// shrinks to fit its share of the width, so eight hospitals stay legible.
export function layout(members) {
  const n = members.length;
  const margin = n <= 3 ? 150 : 90;
  const spacing = n > 1 ? (W - 2 * margin) / (n - 1) : 0;
  return members.map((node, i) => {
    const t = n === 1 ? 0.5 : i / (n - 1);
    const x = margin + t * (W - 2 * margin);
    const y = 300 - Math.sin(Math.PI * t) * 90;
    let art;
    let size;
    if (n <= 3) {
      art = n > 2 && i === Math.floor(n / 2) ? "lg" : "md";
      size = art === "lg" ? 230 : 180;
    } else {
      size = Math.round(Math.max(90, Math.min(180, spacing * 0.85)));
      art = size >= 140 ? "md" : "sm";
    }
    return { ...node, x, y, art, size, tilt: TILTS[i % TILTS.length], dense: n > 5 };
  });
}

// Links only between consecutive chain members, in chain order (the same "left|right" keys the nodes
// log their per-edge residuals under).
export function chainLinks(placed) {
  return placed.slice(1).map((b, i) => [placed[i], b]);
}
