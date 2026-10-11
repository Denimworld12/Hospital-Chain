// The admin page's join codes: relayed to the directory's admin API (directory/app.py, /admin/join-codes)
// with the hub's admin token, which the directory writes on first start to a volume the dashboard reads.
// The full code exists only in the answer to "create"; the directory keeps its hash.

import fs from "node:fs";

const DIRECTORY = (process.env.DIRECTORY_ADMIN_URL || "https://directory:8100").replace(/\/$/, "");
const TOKEN_FILE = process.env.DIRECTORY_ADMIN_TOKEN_FILE || "/hub/admin_token";

function token() {
  try {
    return fs.readFileSync(TOKEN_FILE, "utf8").trim();
  } catch {
    return "";
  }
}

export async function directoryAdmin(method, path, body) {
  const t = token();
  if (!t) return { status: 503, body: { detail: `No hub admin token at ${TOKEN_FILE}: is the directory running?` } };
  try {
    const resp = await fetch(`${DIRECTORY}/admin${path}`, {
      method,
      headers: { Authorization: `Bearer ${t}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(8000),
    });
    return { status: resp.status, body: await resp.json().catch(() => ({})) };
  } catch (err) {
    return { status: 502, body: { detail: `The directory is unreachable: ${err.message}` } };
  }
}
