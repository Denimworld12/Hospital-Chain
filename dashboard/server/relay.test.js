// node --test: which hospitals a dashboard button press is sent to (server/relay.js).

import { test } from "node:test";
import assert from "node:assert/strict";
import { relayTo } from "./relay.js";

const CHAIN = ["a", "b", "c"].map((id) => ({ node_id: id, url: `https://${id}:8000` }));

function fakeChain({ dead = [], refuse = [] } = {}) {
  const calls = [];
  const post = async (node, payload) => {
    calls.push({ node: node.node_id, payload });
    if (dead.includes(node.node_id)) throw new Error("connect ECONNREFUSED");
    if (refuse.includes(node.node_id)) return { status: 403, data: { detail: "no" } };
    return { status: 200, data: { status: "stopped", node_id: node.node_id } };
  };
  return { calls, post };
}

test("stop reaches a hospital beyond a dead one, with one shared op_id", async () => {
  const { calls, post } = fakeChain({ dead: ["b"] });
  const out = await relayTo("stop", {}, CHAIN, post);
  assert.equal(out.status, 200);
  assert.deepEqual(calls.map((c) => c.node).sort(), ["a", "b", "c"]);
  const ops = new Set(calls.map((c) => c.payload.op_id));
  assert.equal(ops.size, 1);
  assert.match([...ops][0], /^stop-/);
  assert.equal(out.body.op_id, [...ops][0]);
  assert.equal(out.body.results.a.status, "stopped");
  assert.equal(out.body.results.c.status, "stopped");
  assert.equal(out.body.results.b.status, "unreachable");
});

test("stop that no hospital accepts is reported as a failure, per node", async () => {
  const { post } = fakeChain({ dead: ["a", "b", "c"] });
  const out = await relayTo("stop", {}, CHAIN, post);
  assert.equal(out.status, 502);
  assert.match(out.body.detail, /No hospital accepted/);
  assert.deepEqual(Object.keys(out.body.results).sort(), ["a", "b", "c"]);
});

test("each stop press gets a fresh op_id so a second press is not dropped as seen", async () => {
  const { calls, post } = fakeChain();
  await relayTo("stop", {}, CHAIN, post);
  await relayTo("stop", {}, CHAIN, post);
  assert.equal(new Set(calls.map((c) => c.payload.op_id)).size, 2);
});

test("start, pause and resume still go to the first member that answers", async () => {
  for (const action of ["start", "pause", "resume"]) {
    const { calls, post } = fakeChain({ dead: ["a"] });
    const out = await relayTo(action, { rounds: 5 }, CHAIN, post);
    assert.equal(out.status, 200);
    assert.equal(out.body.via, "b");
    assert.deepEqual(calls.map((c) => c.node), ["a", "b"]);
    assert.deepEqual(calls[1].payload, action === "start" ? { rounds: 5, initiated_by: "dashboard" } : {});
  }
});

test("no alive hospital is a 503", async () => {
  const out = await relayTo("stop", {}, [], fakeChain().post);
  assert.equal(out.status, 503);
});
