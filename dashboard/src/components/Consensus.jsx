import { ResidualChart } from "./Charts.jsx";
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
