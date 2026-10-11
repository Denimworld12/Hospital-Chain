import { useEffect, useState } from "react";
import favicon from "@assets/favicon.svg";
import Hero from "./components/Hero.jsx";
import Topology from "./components/Topology.jsx";
import { EngineChip, PillButton, WaveDivider } from "./components/ui.jsx";
import TrainingMonitor from "./pages/TrainingMonitor.jsx";
import Communication from "./pages/Communication.jsx";
import AuditLog from "./pages/AuditLog.jsx";
import Assessment from "./pages/Assessment.jsx";
import Nodes from "./pages/Nodes.jsx";
import { control, useSnapshot } from "./useSnapshot.js";

const ROUTES = [
  { hash: "#/", label: "Topology" },
  { hash: "#/training", label: "Training monitor" },
  { hash: "#/communication", label: "Communication" },
  { hash: "#/nodes", label: "Nodes" },
  { hash: "#/audit", label: "Audit log" },
  { hash: "#/assessment", label: "Risk assessment" },
];

function useHashRoute() {
  const read = () => (ROUTES.some((r) => r.hash === window.location.hash) ? window.location.hash : "#/");
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => setRoute(read());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

function ControlStrip({ snapshot }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const run = snapshot?.run;
  const state = run?.state || "idle";
  const running = state === "running" || state === "paused";
  const round = Math.max(0, ...(snapshot?.nodes || []).filter((n) => n.status === "alive").map((n) => n.current_round || 0));

  const act = async (action) => {
    setBusy(true);
    setMessage("");
    try {
      // no round count: each node runs its configured training.rounds (config/*.yaml)
      const res = await control(action);
      if (!res.ok) setMessage(res.detail || res.error || "The nodes refused the request.");
    } catch {
      setMessage("Could not reach the dashboard server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="control-strip">
      <div className="page control-strip__inner">
        <div className="control-strip__status">
          <EngineChip label={snapshot?.engine || "naive gossip baseline"} />
          <span className="control-strip__run" data-testid="run-state">
            {run ? `${run.run_id} / ${state}` : "No training run yet"}
          </span>
        </div>
        <div className="control-strip__actions">
          <PillButton variant="primary" onClick={() => act("start")} disabled={busy || running} aria-busy={running}>
            {running ? `Training: round ${round} of ${run.total_rounds}` : "Start training round"}
          </PillButton>
          {state === "paused" ? (
            <PillButton variant="secondary" onClick={() => act("resume")} disabled={busy}>
              Resume
            </PillButton>
          ) : (
            <PillButton variant="secondary" onClick={() => act("pause")} disabled={busy || state !== "running"}>
              Pause
            </PillButton>
          )}
        </div>
      </div>
      {message && <p className="page control-strip__message" role="status">{message}</p>}
    </div>
  );
}

export default function App() {
  const route = useHashRoute();
  const { snapshot, connection } = useSnapshot();

  return (
    <>
      <header className="topbar">
        <div className="page topbar__inner">
          <a className="brand" href="#/">
            <img src={favicon} alt="" width="28" height="28" />
            <span>MedChain</span>
          </a>
          <nav className="nav" aria-label="Dashboard pages">
            {ROUTES.map((r) => (
              <a key={r.hash} href={r.hash} className={`nav__item${route === r.hash ? " nav__item--active" : ""}`}>
                {r.label}
              </a>
            ))}
          </nav>
          <span className={`live live--${connection}`}>{connection === "live" ? "Live" : connection}</span>
        </div>
      </header>
      <ControlStrip snapshot={snapshot} />
      <main>
        {route === "#/" && (
          <>
            <Hero engine={snapshot?.engine || "naive gossip baseline"} hospitals={snapshot?.topology?.chain?.length || null} />
            <WaveDivider fill="var(--color-pure-white)" />
            <Topology snapshot={snapshot} />
          </>
        )}
        {route === "#/training" && <TrainingMonitor snapshot={snapshot} />}
        {route === "#/communication" && <Communication snapshot={snapshot} />}
        {route === "#/nodes" && <Nodes snapshot={snapshot} />}
        {route === "#/audit" && <AuditLog snapshot={snapshot} />}
        {route === "#/assessment" && <Assessment snapshot={snapshot} />}
      </main>
      <footer className="footer">
        <div className="page">
          The dashboard only observes training: it reads Postgres and the WebSocket and never coordinates it.
          Risk assessments are relayed to one hospital node and not stored.
        </div>
      </footer>
    </>
  );
}
