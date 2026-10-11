// How a button press reaches the chain. Start, pause and resume go to the first alive member that
// answers and that node floods them on. Stop goes to every alive member with one shared op_id, so a
// hospital beyond a dead neighbour stops too; each node drops the copies it has already seen.
// `post(node, payload)` sends one request and resolves to { status, data } (or throws).

import crypto from "node:crypto";

export async function relayTo(action, body, nodes, post) {
  if (!nodes.length) return { status: 503, body: { detail: "No hospital node is alive." } };
  if (action === "stop") return stopAll(nodes, post);
  const payload = action === "start" ? { rounds: body.rounds, initiated_by: "dashboard" } : {};
  let last = null;
  for (const node of nodes) {
    const out = await attempt(node, payload, post);
    if (out.ok) return { status: out.status, body: { via: node.node_id, ...out.data } };
    last = { status: out.status, body: out.error ? { detail: out.error } : { via: node.node_id, ...out.data } };
  }
  return last;
}

async function stopAll(nodes, post) {
  const payload = { op_id: `stop-${crypto.randomBytes(6).toString("hex")}`, origin: "dashboard" };
  const outs = await Promise.all(nodes.map((node) => attempt(node, payload, post)));
  const results = Object.fromEntries(
    nodes.map((node, i) => [node.node_id, outs[i].error ? { status: "unreachable", detail: outs[i].error }
                                                          : { http: outs[i].status, ...outs[i].data }]),
  );
  const ok = outs.some((o) => o.ok);
  return {
    status: ok ? 200 : outs.at(-1).error ? 502 : outs.at(-1).status,
    body: ok ? { status: "stopped", op_id: payload.op_id, results }
             : { detail: "No hospital accepted the stop.", op_id: payload.op_id, results },
  };
}

async function attempt(node, payload, post) {
  try {
    const { status, data } = await post(node, payload);
    return { ok: (status >= 200 && status < 300) || status === 409, status, data };
  } catch (err) {
    return { ok: false, status: 502, error: `${node.node_id} unreachable: ${err.message}` };
  }
}
