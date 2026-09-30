// MedChain dashboard server: serves the built UI, pushes read-only snapshots of
// Postgres over a WebSocket, and relays the two buttons (start / pause) to a
// hospital node as a one-shot request. Training is coordinated peer-to-peer by
// the nodes; killing this process does not affect it.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";
import { WebSocketServer } from "ws";
import { buildSnapshot, STALE_AFTER_S } from "./snapshot.js";

const PORT = Number(process.env.PORT || 8080);
const DIST = process.env.DASHBOARD_DIST || fileURLToPath(new URL("../dist", import.meta.url));
const TICK_MS = Number(process.env.SNAPSHOT_INTERVAL_MS || 1000);

const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 4 });
pool.on("error", (err) => console.warn("postgres pool error:", err.message));

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".svg": "image/svg+xml",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".json": "application/json",
};

function send(res, status, body, type = "application/json") {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body));
}

async function readJson(req) {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  try {
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

// Relay a button press to one alive node; the node floods it to its neighbours.
async function relay(action, body) {
  const { rows } = await pool.query(
    `SELECT node_id, url FROM nodes WHERE last_heartbeat > now() - make_interval(secs => $1) ORDER BY node_id`,
    [STALE_AFTER_S],
  );
  if (!rows.length) return { status: 503, body: { detail: "No hospital node is alive." } };
  let last = null;
  for (const node of rows) {
    try {
      const resp = await fetch(`${node.url}/round/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "start" ? { rounds: body.rounds, initiated_by: "dashboard" } : {}),
        signal: AbortSignal.timeout(8000),
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok || resp.status === 409) return { status: resp.status, body: { via: node.node_id, ...data } };
      last = { status: resp.status, body: { via: node.node_id, ...data } };
    } catch (err) {
      last = { status: 502, body: { detail: `${node.node_id} unreachable: ${err.message}` } };
    }
  }
  return last;
}

function serveStatic(req, res) {
  const url = new URL(req.url, "http://x");
  let file = path.normalize(path.join(DIST, decodeURIComponent(url.pathname)));
  if (!file.startsWith(DIST)) return send(res, 403, { detail: "forbidden" });
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, "index.html");
  if (!fs.existsSync(file)) return send(res, 404, { detail: "dashboard not built" });
  const type = TYPES[path.extname(file)] || "application/octet-stream";
  res.writeHead(200, { "Content-Type": type, "Cache-Control": file.endsWith("index.html") ? "no-store" : "public, max-age=3600" });
  fs.createReadStream(file).pipe(res);
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.url === "/healthz") return send(res, 200, { status: "ok" });
    if (req.url === "/api/snapshot" && req.method === "GET") return send(res, 200, await buildSnapshot(pool));
    const m = req.url.match(/^\/api\/round\/(start|pause|resume)$/);
    if (m && req.method === "POST") {
      const body = await readJson(req);
      const rounds = Number.isInteger(body.rounds) && body.rounds > 0 ? body.rounds : undefined;
      const out = await relay(m[1], { rounds });
      return send(res, out.status, out.body);
    }
    if (req.url.startsWith("/api/")) return send(res, 404, { detail: "not found" });
    return serveStatic(req, res);
  } catch (err) {
    console.warn("request failed:", err.message);
    return send(res, 503, { detail: err.message });
  }
});

const wss = new WebSocketServer({ server, path: "/ws" });
let latest = null;

async function tick() {
  if (wss.clients.size === 0) return;
  try {
    latest = JSON.stringify({ type: "snapshot", data: await buildSnapshot(pool) });
    for (const ws of wss.clients) if (ws.readyState === ws.OPEN) ws.send(latest);
  } catch (err) {
    console.warn("snapshot failed:", err.message);
  }
}

wss.on("connection", async (ws) => {
  try {
    ws.send(JSON.stringify({ type: "snapshot", data: await buildSnapshot(pool) }));
  } catch (err) {
    console.warn("initial snapshot failed:", err.message);
  }
});

setInterval(tick, TICK_MS);
server.listen(PORT, () => console.log(`medchain dashboard (observer) on :${PORT}`));
