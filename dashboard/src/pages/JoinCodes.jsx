import { useCallback, useEffect, useState } from "react";
import { PillButton, SectionHeading } from "../components/ui.jsx";
import { notAuthorized } from "../auth.js";

// Join codes (directory/enroll.py): each one lets one hospital enroll itself, once, before it expires.
// The full code is shown only right after it is created; the hub keeps only its hash.

const RAW = "https://raw.githubusercontent.com/Denimworld12/Hospital-Chain";

// The one command a friend runs (Docker Desktop and Tailscale already installed, on this tailnet).
function InstallCommands({ install }) {
  if (!install) return null;
  const hub = install.hub || "<this hub's Tailscale IP>";
  const base = `${RAW}/${install.version}/deploy/install`;
  return (
    <div className="card join-code">
      <p className="field__label">On the hospital's laptop</p>
      <p>Windows (PowerShell):</p>
      <pre className="join-code__cmd" data-testid="install-windows">{`$env:MEDCHAIN_HUB = "${hub}"; irm ${base}/install.ps1 | iex`}</pre>
      <p>macOS or Linux (Terminal):</p>
      <pre className="join-code__cmd">{`curl -fsSL ${base}/install.sh | MEDCHAIN_HUB=${hub} bash`}</pre>
      <p>It asks for the hospital&apos;s name and the join code, checks Docker, Tailscale, this hub and disk space, and starts the hospital.</p>
    </div>
  );
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString() : "-";
}

async function call(method, path, body) {
  const res = await fetch(`/api/admin/join-codes${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  notAuthorized(res);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, ...data };
}

export default function JoinCodes() {
  const [listing, setListing] = useState(null);
  const [ttl, setTtl] = useState(30);
  const [slice, setSlice] = useState("");
  const [created, setCreated] = useState(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const out = await call("GET", "");
    if (out.ok) setListing(out);
    else setMessage(out.detail || "Could not load the join codes.");
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const create = async (e) => {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    const out = await call("POST", "", { ttl_minutes: Number(ttl), data_slice: slice || null });
    setBusy(false);
    if (!out.ok) return setMessage(out.detail || "Could not create a join code.");
    setCreated(out);
    load();
  };

  const revoke = async (codeId) => {
    const out = await call("POST", `/${codeId}/revoke`, {});
    if (!out.ok) setMessage(out.detail || "Could not revoke it.");
    if (created?.code_id === codeId) setCreated(null);
    load();
  };

  const codes = listing?.codes || [];
  const hospitals = listing?.hospitals || [];
  return (
    <section className="section">
      <div className="page">
        <SectionHeading eyebrow="Admin" title="Join codes" tagline="one code, one hospital, thirty minutes" />
        <div className="assess-grid">
          <form className="card assess-form" onSubmit={create} data-testid="join-code-form">
            <label className="field">
              <span className="field__label">Valid for <em>minutes</em></span>
              <input type="number" min={1} max={1440} required value={ttl} onChange={(e) => setTtl(e.target.value)} />
            </label>
            <label className="field">
              <span className="field__label">Data slice</span>
              <select value={slice} onChange={(e) => setSlice(e.target.value)}>
                <option value="">The least used one</option>
                {(listing?.slices || []).map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
            <div className="field--wide assess-actions">
              <PillButton type="submit" disabled={busy}>Create a join code</PillButton>
            </div>
            {message && <p className="field--wide assess-message" role="alert">{message}</p>}
          </form>
          <div className="card join-code" aria-live="polite">
            {created ? (
              <>
                <p className="field__label">Give this code to one hospital. It is shown only now.</p>
                <p className="join-code__code" data-testid="join-code">{created.code}</p>
                <p>Expires {when(created.expires_at)}. Works once.</p>
              </>
            ) : (
              <p>Create a code, then read it out or send it to the person setting up the hospital.</p>
            )}
          </div>
        </div>
        <InstallCommands install={listing?.install} />
        <div className="journal-wrap">
          <table className="journal" data-testid="join-codes">
            <thead>
              <tr>
                <th>Code</th>
                <th>State</th>
                <th>Created</th>
                <th>By</th>
                <th>Expires</th>
                <th>Data slice</th>
                <th>Used by</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {codes.map((c) => (
                <tr key={c.code_id} data-state={c.state}>
                  <td className="checksum">{c.code_id}-....</td>
                  <td>{c.state}{c.failures ? ` (${c.failures} wrong tries)` : ""}</td>
                  <td>{when(c.created_at)}</td>
                  <td>{c.created_by || "-"}</td>
                  <td>{when(c.expires_at)}</td>
                  <td>{c.data_slice || "least used"}</td>
                  <td>{c.used_by || (c.revoked_by ? `revoked by ${c.revoked_by}` : "-")}</td>
                  <td>
                    {c.state === "active" && (
                      <PillButton variant="secondary" onClick={() => revoke(c.code_id)}>Revoke</PillButton>
                    )}
                  </td>
                </tr>
              ))}
              {codes.length === 0 && (
                <tr>
                  <td colSpan={8} className="journal__empty">No join codes yet.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <h3>Hospitals that joined with a code</h3>
        <div className="journal-wrap">
          <table className="journal" data-testid="enrolled">
            <thead>
              <tr><th>Name</th><th>Node id</th><th>Data slice</th><th>Code</th><th>Enrolled</th></tr>
            </thead>
            <tbody>
              {hospitals.map((h) => (
                <tr key={h.node_id}>
                  <td>{h.display_name}</td>
                  <td className="checksum">{h.node_id}</td>
                  <td>{h.data_slice}</td>
                  <td className="checksum">{h.code_id}</td>
                  <td>{when(h.enrolled_at)}</td>
                </tr>
              ))}
              {hospitals.length === 0 && (
                <tr><td colSpan={5} className="journal__empty">None yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
