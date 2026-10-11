import { useState } from "react";
import { PillButton, SectionHeading } from "../components/ui.jsx";
import { notAuthorized } from "../auth.js";

// Proof of origin: what each hospital's own machine says about itself, in messages signed with the
// key that signs its training updates and checked by the observer against the key the directory has
// on record (node/proof.py). Nothing here comes from the dashboard or from a button press.

const BADGE = {
  verified: { cls: "status-pill--sage", text: "Signature verified" },
  rejected: { cls: "status-pill--orange", text: "Rejected" },
  stale: { cls: "status-pill--grey", text: "No recent message" },
  none: { cls: "status-pill--grey", text: "Never verified" },
};

function duration(s) {
  if (s == null) return "-";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${Math.floor(s % 60)}s`;
}

function ago(s) {
  return s == null ? "-" : s < 60 ? `${Math.round(s)} s ago` : `${duration(s)} ago`;
}

function Rtts({ rtts, names }) {
  const entries = Object.entries(rtts || {});
  if (!entries.length) return "no neighbours yet";
  return entries.map(([id, ms]) => (
    <div key={id} title={id}>
      {names[id] || id}: {ms == null ? <span className="bad">no answer</span> : `${ms} ms`}
    </div>
  ));
}

// The organiser takes a hospital out of the chain on its behalf (a laptop that is off or gone cannot leave by
// itself); its neighbours bridge, and an enrolled hospital's name is freed (directory: /admin/members/<id>/remove).
async function removeMember(id) {
  const res = await fetch(`/api/admin/members/${encodeURIComponent(id)}/remove`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  notAuthorized(res);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, ...data };
}

export default function Nodes({ snapshot }) {
  const [message, setMessage] = useState("");
  const chain = snapshot?.topology?.chain || [];
  const names = Object.fromEntries((snapshot?.nodes || []).map((n) => [n.node_id, n.display_name]));
  // every hospital the dashboard knows, with its newest proof (or none: a node that never sent one is listed too)
  const byId = Object.fromEntries((snapshot?.proofs || []).map((p) => [p.node_id, p]));
  // outside the chain, only a hospital still proving itself is listed (a removed one goes quiet and drops off)
  const outside = Object.keys(byId).filter((id) => !chain.includes(id) && byId[id].status === "verified");
  const proofs = [...new Set([...chain, ...outside])].map((id) => byId[id] || {
    node_id: id, display_name: names[id] || id, status: "none", message: null, seconds_since_proof: null,
  });
  const remove = async (id) => {
    if (!window.confirm(`Take ${names[id] || id} out of the chain? Its neighbours connect to each other instead.`)) return;
    setMessage("");
    const out = await removeMember(id);
    setMessage(out.ok ? `${names[id] || id} is being taken out of the chain.` : out.detail || "Could not remove it.");
  };
  return (
    <section className="section">
      <div className="page">
        <SectionHeading
          eyebrow="Signed by each hospital's own key, checked by the hub"
          title="Nodes"
          tagline="proof that each hospital is a real machine"
        />
        <p>
          Every node sends the observer a status message every 10 seconds, signed with the Ed25519 key that signs its
          training updates. The observer checks the signature against the key the directory has on record for that
          hospital and refuses (and audits) anything else. Host facts come from the install script on that computer;
          round-trip times and uptime are measured live.
        </p>
        {message && <p className="assess-message" role="status">{message}</p>}
        <div className="journal-wrap">
          <table className="journal" data-testid="proofs">
            <thead>
              <tr>
                <th>Hospital</th>
                <th>Signature</th>
                <th>Computer</th>
                <th>Operating system</th>
                <th className="num">Uptime</th>
                <th>Tailscale device</th>
                <th>Round trip to neighbours</th>
                <th className="num">Data rows</th>
                <th>Last signed message</th>
                <th>Chain</th>
              </tr>
            </thead>
            <tbody>
              {proofs.map((p) => {
                const m = p.message || {};
                const host = m.host || {};
                const badge = BADGE[p.status];
                return (
                  <tr key={p.node_id} data-node={p.node_id} data-proof={p.status}>
                    <td title={p.node_id}>
                      <strong>{names[p.node_id] || p.display_name}</strong>
                      <div className="checksum">{p.node_id}</div>
                    </td>
                    <td>
                      <span className={`status-pill ${badge.cls}`}>{badge.text}</span>
                      {p.key_fingerprint && <div className="checksum">key {p.key_fingerprint}</div>}
                      {p.rejection && <div className="bad">{p.rejection}</div>}
                    </td>
                    <td title={host.source || ""}>{host.hostname || "-"}</td>
                    <td>{host.os || "-"}</td>
                    <td className="num">{duration(host.uptime_s)}</td>
                    <td>{host.tailscale_device || "-"}</td>
                    <td>
                      <Rtts rtts={m.neighbours_rtt_ms} names={names} />
                    </td>
                    <td className="num">{m.data_rows ?? "-"}</td>
                    <td>
                      {ago(p.seconds_since_proof)}
                      {Math.abs(m.clock_skew_s || 0) > 60 && (
                        <div className="bad" title="the hospital's clock differs from the hub's; proofs still count">
                          clock {m.clock_skew_s > 0 ? "behind" : "ahead"} by {duration(Math.abs(m.clock_skew_s))}
                        </div>
                      )}
                    </td>
                    <td>
                      {chain.includes(p.node_id) ? (
                        <PillButton variant="secondary" onClick={() => remove(p.node_id)}>Remove</PillButton>
                      ) : (
                        "not in the chain"
                      )}
                    </td>
                  </tr>
                );
              })}
              {proofs.length === 0 && (
                <tr>
                  <td colSpan={10} className="journal__empty">
                    No signed status message yet. A node sends its first one about 10 seconds after it starts.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
