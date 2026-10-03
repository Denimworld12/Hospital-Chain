import networkChain from "@assets/network-chain.svg";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { EngineChip, Eyebrow, Illustration, SectionHeading, engineInfo, formatBytes } from "../components/ui.jsx";

const axis = { stroke: "var(--color-ink-black)", strokeWidth: 1 };
const tick = { fill: "var(--color-ink-black)", fontSize: 12, fontFamily: "var(--font-body)" };
// tooltip header: the round and how many hospitals were in the chain for it
const roundLabel = (rows = []) => (round) => {
  const h = rows.find((r) => r.round === round)?.hospitals;
  return h != null ? `Round ${round} - ${h} hospitals` : `Round ${round}`;
};
const ratio = (a, b) => (a > 0 && b > 0 ? (a / b).toFixed(1) : "-");

function EngineNote({ family, bits, d }) {
  const dims = d ? `d = ${d}` : "this model";
  if (family === "qgadmm")
    return (
      <p className="note">
        Q-GADMM sends {bits} bits per coordinate plus an 8-byte header (b, R) per packet. At {dims} that header
        dominates, so a packet is about {ratio(4 * (d || 0), Math.ceil((bits * (d || 0)) / 8) + 8)}x smaller than the
        32-bit reference; the coordinates alone are {32 / bits}x smaller. The per-packet ratio approaches {32 / bits}x as
        models grow (experiments/compare_engines.py).
      </p>
    );
  if (family === "gadmm_fp")
    return (
      <p className="note">
        Full-precision GADMM sends each difference as raw float32 plus the same 8-byte header, so it costs slightly more
        than the 32-bit reference. It is the b = infinity reference for Q-GADMM.
      </p>
    );
  return (
    <p className="note">
      The naive gossip baseline sends every weight as a full float64, twice the 32-bit reference. It does not quantize.
    </p>
  );
}

export default function Communication({ snapshot }) {
  const c = snapshot?.comm || {};
  const label = snapshot?.engine || "naive gossip baseline";
  const { family, bits } = engineInfo(label);
  const sent = c.bytes_this_round || 0;
  const ref = c.ref32_bytes_this_round || 0;
  const max = Math.max(sent, ref, 1);
  const admm = family !== "gossip";
  return (
    <section className="section">
      <div className="page">
        <SectionHeading eyebrow="Model updates only, never patient rows" title="Communication" tagline="what actually crosses the wire" />
        <div className="comm-engine">
          <EngineChip label={label} />
        </div>
        <div className="comm-grid">
          <div className="comm-counter-wrap">
            <Eyebrow>Bytes sent this round{c.round ? ` - round ${c.round}` : ""}</Eyebrow>
            <div className="card comm-counter">
              <p className="display-number" data-testid="bytes-this-round">{formatBytes(sent)}</p>
              <p className="comm-counter__unit">bytes across {c.messages_this_round || 0} updates (measured payload)</p>
              <p className="comm-counter__chain" data-testid="comm-chain">
                {c.hospitals || 0} hospital{c.hospitals === 1 ? "" : "s"}, {c.links || 0} link{c.links === 1 ? "" : "s"} in the chain now:
                each round sends 2 x {c.links || 0} = {c.packets_per_round || 0} packets
                {c.hospitals_this_round != null && c.hospitals_this_round !== c.hospitals && (
                  <> (round {c.round} ran with {c.hospitals_this_round})</>
                )}
                .
              </p>
              {c.failed_this_round > 0 && (
                <p className="comm-counter__failed">
                  {c.failed_this_round} send{c.failed_this_round === 1 ? "" : "s"} to unreachable neighbours this round (0 bytes delivered)
                </p>
              )}
              <p className="comm-counter__total">
                Total this run: <strong data-testid="bytes-total">{formatBytes(c.total_bytes || 0)}</strong> bytes in{" "}
                {formatBytes(c.messages_total || 0)} updates
              </p>
            </div>
          </div>
          <div className="comm-bars-wrap">
            <Eyebrow>32-bit reference vs actually sent</Eyebrow>
            <div className="card comm-bars">
              <div className="bar-row">
                <span className="bar-row__label">32-bit would cost</span>
                <span className="bar-row__track">
                  <span className="bar-row__bar bar-row__bar--full" style={{ width: `${(ref / max) * 100}%` }} />
                </span>
                <span className="bar-row__value" data-testid="ref32-this-round">{formatBytes(ref)} B</span>
              </div>
              <div className="bar-row">
                <span className="bar-row__label">We sent</span>
                <span className="bar-row__track">
                  <span className="bar-row__bar bar-row__bar--sent" style={{ width: `${(sent / max) * 100}%` }} />
                </span>
                <span className="bar-row__value">{formatBytes(sent)} B</span>
              </div>
              <p className="comm-ratio" data-testid="comm-ratio">
                Whole run: 32-bit would cost <strong>{formatBytes(c.ref32_total_bytes || 0)} B</strong> (4 x d = {c.d ?? "-"}{" "}
                parameters x {formatBytes(c.messages_total || 0)} messages); we sent <strong>{formatBytes(c.total_bytes || 0)} B</strong>
                {c.total_bytes > 0 && (
                  <>
                    {" "}={" "}
                    {c.ref32_total_bytes >= c.total_bytes ? (
                      <><strong>{ratio(c.ref32_total_bytes, c.total_bytes)}x</strong> less</>
                    ) : (
                      <><strong>{ratio(c.total_bytes, c.ref32_total_bytes)}x</strong> more</>
                    )}
                  </>
                )}
                .
                {admm && c.coordinate_bytes_total > 0 && (
                  <>
                    {" "}Coordinates alone, without the 8-byte header: {formatBytes(c.coordinate_bytes_total)} B ={" "}
                    {ratio(c.ref32_total_bytes, c.coordinate_bytes_total)}x less.
                  </>
                )}
              </p>
              <EngineNote family={family} bits={bits} d={c.d} />
            </div>
          </div>
        </div>
        <Eyebrow className="comm-history-label">Bytes per round, this run</Eyebrow>
        <div className="card comm-history">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={c.per_round || []} margin={{ top: 12, right: 12, bottom: 0, left: 0 }} barGap={2}>
              <XAxis dataKey="round" axisLine={axis} tickLine={axis} tick={tick} />
              <YAxis axisLine={axis} tickLine={axis} tick={tick} width={56} />
              <Tooltip cursor={{ fill: "rgba(255, 196, 0, 0.12)" }} labelFormatter={roundLabel(c.per_round)} contentStyle={{ border: "1px solid #000", borderRadius: 8, fontFamily: "var(--font-body)" }} />
              <Legend wrapperStyle={{ fontFamily: "var(--font-label)", fontSize: 12, letterSpacing: "0.133em", textTransform: "uppercase" }} />
              <Bar dataKey="ref32_bytes" name="32-bit would cost" fill="var(--mc-bar-full)" isAnimationActive={false} />
              <Bar dataKey="bytes" name="Actually sent" fill="var(--mc-bar-sent)" isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <Illustration src={networkChain} rotate={-6} className="comm__accent" />
    </section>
  );
}
