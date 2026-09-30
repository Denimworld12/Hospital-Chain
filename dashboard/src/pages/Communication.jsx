import networkChain from "@assets/network-chain.svg";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Legend } from "recharts";
import { Eyebrow, Illustration, SectionHeading, formatBytes } from "../components/ui.jsx";

const axis = { stroke: "var(--color-ink-black)", strokeWidth: 1 };
const tick = { fill: "var(--color-ink-black)", fontSize: 12, fontFamily: "var(--font-body)" };

export default function Communication({ snapshot }) {
  const c = snapshot?.comm || {};
  const sent = c.bytes_this_round || 0;
  const full = c.full_precision_bytes_this_round || 0;
  const max = Math.max(sent, full, 1);
  return (
    <section className="section">
      <div className="page">
        <SectionHeading eyebrow="Weights only, never patient rows" title="Communication" tagline="what actually crosses the wire" />
        <div className="comm-grid">
          <div className="comm-counter-wrap">
            <Eyebrow>Bytes sent this round{c.round ? ` - round ${c.round}` : ""}</Eyebrow>
            <div className="card comm-counter">
              <p className="display-number" data-testid="bytes-this-round">{formatBytes(sent)}</p>
              <p className="comm-counter__unit">bytes across {c.messages_this_round || 0} weight updates</p>
              <p className="comm-counter__total">
                Total this run: <strong>{formatBytes(c.total_bytes || 0)}</strong> bytes
              </p>
            </div>
          </div>
          <div className="comm-bars-wrap">
            <Eyebrow>Full precision vs actually sent</Eyebrow>
            <div className="card comm-bars">
              <div className="bar-row">
                <span className="bar-row__label">Full precision would cost</span>
                <span className="bar-row__track">
                  <span className="bar-row__bar bar-row__bar--full" style={{ width: `${(full / max) * 100}%` }} />
                </span>
                <span className="bar-row__value">{formatBytes(full)} B</span>
              </div>
              <div className="bar-row">
                <span className="bar-row__label">Actually sent</span>
                <span className="bar-row__track">
                  <span className="bar-row__bar bar-row__bar--sent" style={{ width: `${(sent / max) * 100}%` }} />
                </span>
                <span className="bar-row__value">{formatBytes(sent)} B</span>
              </div>
              <p className="note">
                The naive gossip baseline sends every weight as a full-precision float64, so the two bars
                are equal today. Quantization arrives with Q-GADMM in a later phase; that is what will
                shrink the gold bar.
              </p>
            </div>
          </div>
        </div>
        <Eyebrow className="comm-history-label">Bytes per round, this run</Eyebrow>
        <div className="card comm-history">
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={c.per_round || []} margin={{ top: 12, right: 12, bottom: 0, left: 0 }} barGap={2}>
              <XAxis dataKey="round" axisLine={axis} tickLine={axis} tick={tick} />
              <YAxis axisLine={axis} tickLine={axis} tick={tick} width={56} />
              <Tooltip cursor={{ fill: "rgba(255, 196, 0, 0.12)" }} contentStyle={{ border: "1px solid #000", borderRadius: 8, fontFamily: "var(--font-body)" }} />
              <Legend wrapperStyle={{ fontFamily: "var(--font-label)", fontSize: 12, letterSpacing: "0.133em", textTransform: "uppercase" }} />
              <Bar dataKey="full_precision_bytes" name="Full precision would cost" fill="var(--mc-bar-full)" isAnimationActive={false} />
              <Bar dataKey="bytes" name="Actually sent" fill="var(--mc-bar-sent)" isAnimationActive={false} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <Illustration src={networkChain} rotate={-6} className="comm__accent" />
    </section>
  );
}
