import hospitalLg from "@assets/hospital-building.svg";
import hospitalMd from "@assets/hospital-building-md.svg";
import hospitalSm from "@assets/hospital-building-sm.svg";
import blobGold from "@assets/blob-gold.svg";
import stethoscope from "@assets/stethoscope.svg";
import ecg from "@assets/ecg-wave.svg";
import { Illustration, SectionHeading, StatusDot } from "./ui.jsx";

const W = 1000;
const H = 470;
const TILTS = [-6, 5, -5, 7, -7];

// Lay nodes out along a gentle arc. Everything comes from the snapshot, so a
// config with a different number of hospitals draws without code changes.
function layout(nodes) {
  const n = nodes.length;
  return nodes.map((node, i) => {
    const t = n === 1 ? 0.5 : i / (n - 1);
    const x = 150 + t * (W - 300);
    const y = 300 - Math.sin(Math.PI * t) * 90;
    const art = n > 2 && i === Math.floor(n / 2) ? hospitalLg : n > 4 ? hospitalSm : hospitalMd;
    const size = art === hospitalLg ? 230 : art === hospitalMd ? 180 : 130;
    return { ...node, x, y, art, size, tilt: TILTS[i % TILTS.length] };
  });
}

function edges(placed) {
  const byId = Object.fromEntries(placed.map((p) => [p.node_id, p]));
  const seen = new Set();
  const out = [];
  for (const p of placed) {
    for (const nb of p.neighbours || []) {
      const key = [p.node_id, nb].sort().join("|");
      if (seen.has(key) || !byId[nb]) continue;
      seen.add(key);
      out.push([p, byId[nb]]);
    }
  }
  return out;
}

function Envelope({ x, y, rot }) {
  return (
    <g transform={`translate(${x} ${y}) rotate(${rot})`} className="topo__envelope">
      <rect x="-11" y="-7.5" width="22" height="15" rx="1" />
      <path d="M-11 -7.5 L0 1.5 L11 -7.5" />
    </g>
  );
}

export default function Topology({ snapshot }) {
  const nodes = snapshot?.nodes || [];
  const placed = layout(nodes);
  const links = edges(placed);
  const centre = placed[Math.floor(placed.length / 2)];
  const alive = nodes.filter((n) => n.status === "alive").length;

  return (
    <section className="topology section section--white" id="topology">
      <div className="page">
        <SectionHeading
          eyebrow={`${alive} of ${nodes.length} nodes alive / heartbeat every 5s`}
          title="Network topology"
          tagline="each hospital talks only to its neighbours"
        />
        {nodes.length === 0 ? (
          <p className="empty">Waiting for hospital nodes to register...</p>
        ) : (
          <svg className="topo" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Hospital network topology">
            {centre && (
              <image href={blobGold} x={centre.x - 170} y={centre.y - 205} width="340" height="320" className="topo__blob" />
            )}
            {links.map(([a, b]) => {
              const down = a.status !== "alive" || b.status !== "alive";
              const [l, r] = a.x < b.x ? [a, b] : [b, a];
              const x1 = l.x + l.size * 0.46;
              const y1 = l.y - l.size * 0.3;
              const x2 = r.x - r.size * 0.46;
              const y2 = r.y - r.size * 0.3;
              const cx = (x1 + x2) / 2;
              const cy = Math.min(y1, y2) - 60;
              const ex = 0.25 * x1 + 0.5 * cx + 0.25 * x2;
              const ey = 0.25 * y1 + 0.5 * cy + 0.25 * y2;
              return (
                <g key={`${a.node_id}-${b.node_id}`} className={`topo__link${down ? " topo__link--down" : ""}`}>
                  <path d={`M${x1} ${y1} Q${cx} ${cy} ${x2} ${y2}`} />
                  {!down && <Envelope x={ex} y={ey - 14} rot={y2 > y1 ? 8 : -8} />}
                </g>
              );
            })}
            {placed.map((p) => (
              <g key={p.node_id} className="topo__node" data-node={p.node_id} data-status={p.status}>
                <image
                  href={p.art}
                  x={p.x - p.size / 2}
                  y={p.y - p.size * 0.72}
                  width={p.size}
                  height={p.size * 0.9}
                  transform={`rotate(${p.tilt} ${p.x} ${p.y})`}
                  opacity={p.status === "alive" ? 1 : 0.45}
                />
                <circle cx={p.x + p.size * 0.36} cy={p.y - p.size * 0.6} r="11" className={`topo__dot topo__dot--${p.status === "alive" ? "alive" : "down"}`} />
                <text x={p.x} y={p.y + p.size * 0.28 + 8} className="topo__name" textAnchor="middle">
                  {p.display_name}
                </text>
                <text x={p.x} y={p.y + p.size * 0.28 + 30} className="topo__meta" textAnchor="middle">
                  {p.patients} patients / {p.status === "alive" ? `round ${p.current_round || 0}` : `down ${Math.round(p.seconds_since_heartbeat || 0)}s`}
                </text>
              </g>
            ))}
          </svg>
        )}
        <ul className="topo__legend">
          {nodes.map((n) => (
            <li key={n.node_id}>
              <StatusDot status={n.status} label={false} /> {n.display_name}: {n.status === "alive" ? "alive" : "down"}
            </li>
          ))}
        </ul>
      </div>
      <Illustration src={stethoscope} rotate={-8} className="topology__accent" />
      <img className="ecg-rule" src={ecg} alt="" aria-hidden="true" />
    </section>
  );
}
