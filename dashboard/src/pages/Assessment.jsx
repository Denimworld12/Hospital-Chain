import { useEffect, useState } from "react";
import stethoscope from "@assets/stethoscope.svg";
import { Eyebrow, Illustration, PillButton, SectionHeading } from "../components/ui.jsx";

const LEVELS = [
  [1, "Normal"],
  [2, "Above normal"],
  [3, "Well above normal"],
];

const EMPTY = {
  age: 52, gender: "female", height: 160, weight: 68, ap_hi: 130, ap_lo: 85,
  cholesterol: 1, gluc: 1, smoke: false, alco: false, active: true,
};

const NUMBERS = [
  ["age", "Age", "years", 18, 100],
  ["height", "Height", "cm", 120, 220],
  ["weight", "Weight", "kg", 30, 200],
  ["ap_hi", "Systolic BP", "mmHg", 60, 250],
  ["ap_lo", "Diastolic BP", "mmHg", 40, 160],
];

const TOGGLES = [
  ["smoke", "Smoker"],
  ["alco", "Drinks alcohol"],
  ["active", "Physically active"],
];

const ECG_CLASSES = [
  ["normal", "Normal ECG"],
  ["mi", "Myocardial infarction"],
  ["abnormal", "Abnormal heartbeat"],
];

async function post(path, node, body, isForm) {
  return fetch(`/api/assess${path}?node=${encodeURIComponent(node || "")}`, {
    method: "POST",
    headers: isForm ? undefined : { "Content-Type": "application/json" },
    body: isForm ? body : JSON.stringify(body),
  });
}

async function errorText(res) {
  const data = await res.json().catch(() => ({}));
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ");
  return data.detail || `The node answered ${res.status}.`;
}

async function download(res, fallback) {
  const blob = await res.blob();
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || fallback;
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Bar({ label, value, tone, marker }) {
  return (
    <div className="bar-row">
      <span className="bar-row__label">{label}</span>
      <span className="bar-row__track assess-bar__track">
        <span className={`bar-row__bar assess-bar--${tone}`} style={{ width: `${Math.min(value, 100)}%` }} />
        {marker != null && <span className="assess-bar__marker" style={{ left: `${marker}%` }} title="Decision threshold" />}
      </span>
      <span className="bar-row__value">{value.toFixed(1)}%</span>
    </div>
  );
}

function Factors({ title, items }) {
  return (
    <div className="assess-factors__group">
      <Eyebrow>{title}</Eyebrow>
      {items.length ? (
        <ul>{items.map((f) => <li key={f}>{f}</li>)}</ul>
      ) : (
        <p className="assess-factors__none">None identified</p>
      )}
    </div>
  );
}

function TabularResult({ r }) {
  const m = r.model.metrics || {};
  return (
    <div className="card assess-result" data-testid="assess-result">
      <div className="assess-result__head">
        <span className={`tier tier--${r.risk_tier.toLowerCase()}`} data-testid="risk-tier">{r.risk_tier} risk</span>
        <span className="assess-result__via">scored on {r.hospital}</span>
      </div>
      <p className="display-number" data-testid="assess-confidence">{r.confidence.toFixed(1)}%</p>
      <p className="assess-result__label">{r.label}: blended probability, positive at {Math.round(r.threshold * 100)}% or more</p>
      <Bar label="Random forest" value={r.rf_confidence} tone="rf" />
      <Bar label="XGBoost (calibrated)" value={r.xgb_confidence} tone="xgb" />
      <Bar label="Blended 0.4 / 0.6" value={r.confidence} tone={r.risk_tier.toLowerCase()} marker={r.threshold * 100} />
      <div className="assess-factors">
        <Factors title="Critical" items={r.critical_factors} />
        <Factors title="Moderate" items={r.moderate_factors} />
        <Factors title="Lifestyle" items={r.lifestyle_factors} />
      </div>
      <p className="note">
        {r.model.architecture}, trained on the {r.model.trained_on}: general international data, not
        Indian-sourced. Held-out accuracy {Math.round((m.accuracy || 0) * 1000) / 10}%, ROC-AUC {(m.roc_auc || 0).toFixed(3)}.
        A screening aid, not a diagnosis.
      </p>
    </div>
  );
}

function EcgPanel({ node, available }) {
  const [file, setFile] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const send = async (pdf) => {
    if (!file) return;
    setBusy(true);
    setMessage("");
    try {
      const form = new FormData();
      form.append("ecg", file);
      const res = await post(pdf ? "/ecg/pdf" : "/ecg", node, form, true);
      if (!res.ok) setMessage(await errorText(res));
      else if (pdf) await download(res, "ecg-analysis.pdf");
      else setResult(await res.json());
    } catch {
      setMessage("Could not reach the dashboard server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="assess-ecg">
      <Eyebrow>ECG image - 12-lead printout, PNG or JPG</Eyebrow>
      <div className="card assess-ecg__card">
        <label className="assess-file">
          <input type="file" accept="image/png,image/jpeg" disabled={!available}
            onChange={(e) => { setFile(e.target.files?.[0] || null); setResult(null); setMessage(""); }} />
          <span>{file ? file.name : "Choose an ECG image"}</span>
        </label>
        <div className="assess-actions">
          <PillButton onClick={() => send(false)} disabled={!available || !file || busy}>Analyse ECG</PillButton>
          <PillButton variant="secondary" onClick={() => send(true)} disabled={!available || !file || busy}>Download ECG report</PillButton>
        </div>
        {message && <p className="assess-message" role="status">{message}</p>}
        {result && (
          <div className="assess-ecg__result" data-testid="ecg-result">
            <p className="assess-ecg__label">{result.label} <span>{result.confidence.toFixed(1)}%</span></p>
            {ECG_CLASSES.map(([k, label]) => (
              <Bar key={k} label={label} value={result.probabilities[k] * 100} tone={`ecg-${k}`} />
            ))}
            <p className="note">
              {result.model.architecture}, trained on the {result.model.trained_on}. Held-out accuracy{" "}
              {Math.round((result.model.metrics?.accuracy || 0) * 1000) / 10}%.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Assessment({ snapshot }) {
  const alive = (snapshot?.nodes || []).filter((n) => n.status === "alive");
  const [node, setNode] = useState("");
  const [patient, setPatient] = useState(EMPTY);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (alive.length && !alive.some((n) => n.node_id === node)) setNode(alive[0].node_id);
  }, [alive.map((n) => n.node_id).join(","), node]);

  const set = (key, value) => setPatient((p) => ({ ...p, [key]: value }));

  const submit = async (pdf) => {
    setBusy(true);
    setMessage("");
    try {
      const res = await post(pdf ? "/pdf" : "", node, patient);
      if (!res.ok) setMessage(await errorText(res));
      else if (pdf) await download(res, "risk-assessment.pdf");
      else setResult(await res.json());
    } catch {
      setMessage("Could not reach the dashboard server.");
    } finally {
      setBusy(false);
    }
  };

  const available = alive.length > 0;

  return (
    <section className="section">
      <div className="page">
        <SectionHeading eyebrow="Inference only - not part of federated training" title="Risk assessment"
          tagline="one patient, one hospital, a second opinion" />
        <div className="assess-grid">
          <form className="assess-form-wrap" onSubmit={(e) => { e.preventDefault(); submit(false); }}>
            <Eyebrow>Patient - cardiovascular risk factors</Eyebrow>
            <div className="card assess-form">
              <label className="field field--wide">
                <span className="field__label">Hospital node</span>
                <select value={node} onChange={(e) => setNode(e.target.value)} disabled={!available} data-testid="assess-node">
                  {alive.map((n) => <option key={n.node_id} value={n.node_id}>{n.display_name}</option>)}
                </select>
              </label>
              {NUMBERS.map(([key, label, unit, min, max]) => (
                <label key={key} className="field">
                  <span className="field__label">{label} <em>{unit}</em></span>
                  <input type="number" required min={min} max={max} step="any" value={patient[key]}
                    onChange={(e) => set(key, e.target.value === "" ? "" : Number(e.target.value))} name={key} />
                </label>
              ))}
              <label className="field">
                <span className="field__label">Sex</span>
                <select value={patient.gender} onChange={(e) => set("gender", e.target.value)} name="gender">
                  <option value="female">Female</option>
                  <option value="male">Male</option>
                </select>
              </label>
              {[["cholesterol", "Cholesterol"], ["gluc", "Glucose"]].map(([key, label]) => (
                <label key={key} className="field">
                  <span className="field__label">{label}</span>
                  <select value={patient[key]} onChange={(e) => set(key, Number(e.target.value))} name={key}>
                    {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                  </select>
                </label>
              ))}
              <div className="field field--wide assess-toggles">
                {TOGGLES.map(([key, label]) => (
                  <label key={key} className="toggle">
                    <input type="checkbox" checked={patient[key]} onChange={(e) => set(key, e.target.checked)} name={key} />
                    <span>{label}</span>
                  </label>
                ))}
              </div>
              <div className="field--wide assess-actions">
                <PillButton type="submit" disabled={!available || busy}>Assess risk</PillButton>
                <PillButton variant="secondary" onClick={() => submit(true)} disabled={!available || busy}>
                  Download PDF report
                </PillButton>
              </div>
              {!available && <p className="assess-message field--wide">No hospital node is alive.</p>}
              {message && <p className="assess-message field--wide" role="status">{message}</p>}
            </div>
          </form>
          <div className="assess-result-wrap">
            <Eyebrow>Result</Eyebrow>
            {result ? (
              <TabularResult r={result} />
            ) : (
              <div className="card assess-result assess-result--empty">
                <p>Fill in the patient's readings and press <strong>Assess risk</strong>. The hospital node scores
                  them with its RandomForest + XGBoost ensemble; nothing is stored.</p>
              </div>
            )}
          </div>
        </div>
        <EcgPanel node={node} available={available} />
      </div>
      <Illustration src={stethoscope} rotate={8} className="assess__accent" />
    </section>
  );
}
