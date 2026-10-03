import microscope from "@assets/microscope.svg";
import { MetricChart } from "../components/Charts.jsx";
import ConsensusPanel, { NetworkLoss } from "../components/Consensus.jsx";
import { Eyebrow, Illustration, SectionHeading, StatusDot } from "../components/ui.jsx";

const pct = (v) => `${Math.round(v * 100)}%`;
const loss = (v) => (v == null ? "-" : Number(v).toFixed(2));

export default function TrainingMonitor({ snapshot }) {
  // chain members in chain order, then hospitals that left (their history from this run stays visible)
  const chain = snapshot?.topology?.chain || [];
  const rank = (n) => (chain.includes(n.node_id) ? chain.indexOf(n.node_id) : chain.length);
  const nodes = [...(snapshot?.nodes || [])].sort((a, b) => rank(a) - rank(b));
  const total = snapshot?.run?.total_rounds || 20;
  return (
    <section className="section">
      <div className="page">
        <SectionHeading
          eyebrow={`Engine: ${snapshot?.engine || "naive gossip baseline"}`}
          title="Training monitor"
          tagline="each hospital learns on its own patients"
        />
        <ConsensusPanel snapshot={snapshot} />
        <NetworkLoss snapshot={snapshot} />
        <div className="card-grid">
          {nodes.map((n) => {
            const series = snapshot?.metrics?.[n.node_id] || [];
            const last = series[series.length - 1];
            return (
              <article key={n.node_id} className="node-card-wrap" data-node={n.node_id}>
                <Eyebrow>
                  {n.display_name} - {n.patients} patients{chain.length > 0 && !chain.includes(n.node_id) ? " - left the chain" : ""}
                </Eyebrow>
                <div className="card node-card">
                  <div className="node-card__head">
                    <StatusDot status={n.status} />
                    <span className="node-card__round">
                      Round {last?.round ?? 0} / {total}
                    </span>
                  </div>
                  <dl className="node-card__stats">
                    <div>
                      <dt>Validation loss</dt>
                      <dd>{loss(last?.loss)}</dd>
                    </div>
                    <div>
                      <dt>Accuracy</dt>
                      <dd>{last ? pct(last.accuracy) : "-"}</dd>
                    </div>
                  </dl>
                  <MetricChart data={series} dataKey="loss" title="Loss (local held-out split)" totalRounds={total} domain={["auto", "auto"]} format={loss} />
                  <MetricChart data={series} dataKey="accuracy" title="Accuracy (local held-out split)" totalRounds={total} domain={[0.4, 1]} format={pct} />
                </div>
              </article>
            );
          })}
        </div>
        {nodes.length === 0 && <p className="empty">Waiting for hospital nodes to register...</p>}
      </div>
      <Illustration src={microscope} rotate={-7} className="training__accent" />
    </section>
  );
}
