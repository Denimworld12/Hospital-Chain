import { SectionHeading, formatBytes } from "../components/ui.jsx";

function time(ts) {
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toISOString().replace("T", " ").slice(0, 23) + "Z";
}

// Hospitals by their display name (the node id, their stable identity, is in the tooltip).
function Party({ id, names }) {
  if (!id) return "-";
  return names[id] && names[id] !== id ? <span title={id}>{names[id]}</span> : id;
}

export default function AuditLog({ snapshot }) {
  const rows = snapshot?.audit || [];
  const names = Object.fromEntries((snapshot?.nodes || []).map((n) => [n.node_id, n.display_name]));
  return (
    <section className="section">
      <div className="page">
        <SectionHeading eyebrow="Every /update, logged by its receiver" title="Audit log" tagline="a journal of every weight update" />
        <div className="journal-wrap">
          <table className="journal" data-testid="audit-log">
            <thead>
              <tr>
                <th>Timestamp</th>
                <th>Event</th>
                <th>Round</th>
                <th>Sender</th>
                <th>Receiver</th>
                <th className="num">Payload bytes</th>
                <th>Checksum (sha-256)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} data-event={r.event}>
                  <td>{time(r.ts)}</td>
                  <td>{r.event.replaceAll("_", " ")}</td>
                  <td>{r.round ?? "-"}</td>
                  <td><Party id={r.sender} names={names} /></td>
                  <td><Party id={r.receiver} names={names} /></td>
                  <td className="num">{r.payload_bytes != null ? formatBytes(r.payload_bytes) : "-"}</td>
                  <td className="checksum" title={r.checksum || ""}>
                    {r.checksum ? `${r.checksum.slice(0, 16)}...` : "-"}
                    {r.detail?.verified === false && <span className="bad"> mismatch</span>}
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={7} className="journal__empty">
                    No entries yet. Start a training round to fill the journal.
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
