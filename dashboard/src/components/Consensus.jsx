import { MetricChart, ResidualChart } from "./Charts.jsx";
import { Eyebrow, Pill, engineInfo } from "./ui.jsx";

const sci = (v) => (v == null ? "-" : Number(v).toExponential(2));

// Q-GADMM's primal residual ||theta_hat_n - theta_hat_{n+1}||_2 per chain edge: how far apart
// neighbouring hospitals' published models still are. It falls to zero as they agree.
export default function ConsensusPanel({ snapshot }) {
  const c = snapshot?.consensus || { edges: [] };
  const total = snapshot?.run?.total_rounds || 20;
  const gossip = engineInfo(snapshot?.engine).family === "gossip";
  return (
    <div className="consensus" data-testid="consensus-panel">
      <Eyebrow>Disagreement between neighbours</Eyebrow>
      <div className="card consensus__card">
        <div className="consensus__head">
          <h3 className="consensus__title">Primal residual per edge</h3>
          {c.reached && <Pill className="status-pill--sage" data-testid="consensus-pill">Consensus reached</Pill>}
        </div>
        {c.edges.length ? (
          <>
            <p className="consensus__meta">
              Latest round {c.latest_round ?? "-"}: largest edge residual <strong>{sci(c.latest_max)}</strong>, epsilon {c.epsilon}
              {c.converged_round != null && <> / below epsilon on every edge for 3 rounds by round {c.converged_round}</>}
            </p>
            {c.epoch_start_round != null && c.epoch_start_round > 1 && (
              <p className="consensus__meta" data-testid="consensus-epoch">
                The chain changed at round {c.epoch_start_round} (membership epoch {c.epoch}): {c.edges.length} link
                {c.edges.length === 1 ? "" : "s"} now, and the consensus check restarted on the new chain.
              </p>
            )}
            <ResidualChart series={c.series} edges={c.edges} epsilon={c.epsilon} totalRounds={total} />
          </>
        ) : !gossip ? (
          <p className="note">Waiting for the first round's residuals...</p>
        ) : (
          <p className="note">
            The naive gossip baseline has no consensus constraint and keeps no quantized snapshots, so it logs no
            residuals. Run engine: qgadmm or gadmm_fp to see neighbours converge here.
          </p>
        )}
      </div>
    </div>
  );
}

const loss = (v) => (v == null ? "-" : Number(v).toFixed(3));

// The objective the hospitals minimise together. A single hospital's held-out loss can rise while
// this falls: consensus trades a little local fit (on non-IID data) for one shared model.
export function NetworkLoss({ snapshot }) {
  const series = snapshot?.network_loss || [];
  const total = snapshot?.run?.total_rounds || 20;
  const last = series[series.length - 1];
  return (
    <div className="consensus" data-testid="network-loss">
      <Eyebrow>Loss across the network</Eyebrow>
      <div className="card consensus__card">
        <div className="consensus__head">
          <h3 className="consensus__title">Network training loss</h3>
          <span className="consensus__meta">
            {last ? `round ${last.round}: ${loss(last.loss)} over ${last.hospitals} hospital${last.hospitals === 1 ? "" : "s"}` : "no rounds yet"}
          </span>
        </div>
        <p className="consensus__meta">
          Mean training log-loss over all hospitals' own patients, each at its own model, weighted by patients.
          Each round averages the hospitals in the chain that round, so it recomputes when a hospital joins or leaves.
          One hospital's held-out loss can rise while this falls: agreeing on one model costs a little local fit.
        </p>
        <MetricChart data={series} dataKey="loss" title="" totalRounds={total} domain={["auto", "auto"]} format={loss} />
      </div>
    </div>
  );
}
