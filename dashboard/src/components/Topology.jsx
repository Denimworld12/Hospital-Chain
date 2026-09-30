import hospitalLg from "@assets/hospital-building.svg";
import hospitalMd from "@assets/hospital-building-md.svg";
import hospitalSm from "@assets/hospital-building-sm.svg";
import blobGold from "@assets/blob-gold.svg";
import stethoscope from "@assets/stethoscope.svg";
import ecg from "@assets/ecg-wave.svg";
import { useEffect, useRef, useState } from "react";
import { Illustration, SectionHeading, StatusDot } from "./ui.jsx";
import { subscribeUpdates } from "../useSnapshot.js";

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

function Envelope({ x, y, rot, flying = false }) {
  return (
    <g transform={`translate(${x} ${y}) rotate(${rot})`} className={`topo__envelope${flying ? " topo__envelope--flying" : ""}`}
      data-testid={flying ? "envelope-in-flight" : undefined}>
      <rect x="-11" y="-7.5" width="22" height="15" rx="1" />
      <path d="M-11 -7.5 L0 1.5 L11 -7.5" />
    </g>
  );
}

const FLIGHT_MS = 700;
const linkKey = (a, b) => [a, b].sort().join("|");

// Geometry of one chain link: a quadratic curve from the left node's roof to the right node's.
function linkGeometry(a, b) {
  const [l, r] = a.x < b.x ? [a, b] : [b, a];
  const p0 = [l.x + l.size * 0.46, l.y - l.size * 0.3];
  const p2 = [r.x - r.size * 0.46, r.y - r.size * 0.3];
  const p1 = [(p0[0] + p2[0]) / 2, Math.min(p0[1], p2[1]) - 60];
  return { left: l.node_id, right: r.node_id, p0, p1, p2 };
}

function pointAt({ p0, p1, p2 }, t) {
  const u = 1 - t;
  const x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0];
  const y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1];
  const dx = 2 * u * (p1[0] - p0[0]) + 2 * t * (p2[0] - p1[0]);
  const dy = 2 * u * (p1[1] - p0[1]) + 2 * t * (p2[1] - p1[1]);
  return { x, y, rot: (Math.atan2(dy, dx) * 180) / Math.PI };
}

// One envelope per delivered /update (a WebSocket "update" event), flying sender -> receiver.
function useFlights() {
  const [flights, setFlights] = useState([]);
  const [now, setNow] = useState(() => performance.now());
  const frame = useRef(0);
  useEffect(
    () =>
      subscribeUpdates((rows) => {
        const t = performance.now();
        setFlights((cur) => {
          const next = cur.filter((f) => t - f.start < FLIGHT_MS);
          for (const row of rows) {
            if (row.status !== "delivered") continue;
            const key = linkKey(row.sender, row.receiver);
            // at most one envelope per direction in flight, so a busy run stays legible
            if (next.some((f) => f.key === key && f.from === row.sender)) continue;
            next.push({ id: row.id, key, from: row.sender, start: t, bytes: row.payload_bytes });
          }
          return next;
        });
      }),
    [],
  );
  useEffect(() => {
    if (!flights.length) return undefined;
    const step = () => {
      const t = performance.now();
      setNow(t);
      if (flights.some((f) => t - f.start < FLIGHT_MS)) frame.current = requestAnimationFrame(step);
      else setFlights([]);
    };
    frame.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame.current);
  }, [flights]);
  return { flights, now };
}

export default function Topology({ snapshot }) {
  const nodes = snapshot?.nodes || [];
  const placed = layout(nodes);
  const links = edges(placed);
  const centre = placed[Math.floor(placed.length / 2)];
  const alive = nodes.filter((n) => n.status === "alive").length;
  const { flights, now } = useFlights();

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
              const g = linkGeometry(a, b);
              const key = linkKey(a.node_id, b.node_id);
              const inFlight = flights.filter((f) => f.key === key && now - f.start < FLIGHT_MS);
              return (
                <g key={key} className={`topo__link${down ? " topo__link--down" : ""}`} data-link={key}>
                  <path d={`M${g.p0[0]} ${g.p0[1]} Q${g.p1[0]} ${g.p1[1]} ${g.p2[0]} ${g.p2[1]}`} />
                  {inFlight.map((f) => {
                    const t = Math.min(1, Math.max(0, (now - f.start) / FLIGHT_MS));
                    const pt = pointAt(g, f.from === g.left ? t : 1 - t);
                    return <Envelope key={f.id} x={pt.x} y={pt.y - 12} rot={pt.rot > 90 || pt.rot < -90 ? pt.rot + 180 : pt.rot} flying />;
                  })}
                  {!down && !inFlight.length && (() => {
                    const pt = pointAt(g, 0.5);
                    return <Envelope x={pt.x} y={pt.y - 14} rot={0} />;
                  })()}
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
