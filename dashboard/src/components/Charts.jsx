import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, Legend } from "recharts";
import { shortId } from "./ui.jsx";

const axis = { stroke: "var(--color-ink-black)", strokeWidth: 1 };
const tick = { fill: "var(--color-ink-black)", fontSize: 12, fontFamily: "var(--font-body)" };

function ChartTooltip({ active, payload, label, format }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tip">
      <span className="chart-tip__round">Round {label}</span>
      <span className="chart-tip__value">{format(payload[0].value)}</span>
    </div>
  );
}

export function MetricChart({ data, dataKey, title, totalRounds, domain, format }) {
  return (
    <figure className="metric-chart">
      <figcaption className="metric-chart__title">{title}</figcaption>
      <div className="metric-chart__plot">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <XAxis dataKey="round" type="number" domain={[1, totalRounds]} allowDecimals={false} axisLine={axis} tickLine={axis} tick={tick} />
            <YAxis domain={domain} axisLine={axis} tickLine={axis} tick={tick} tickFormatter={format} width={44} />
            <Tooltip content={<ChartTooltip format={format} />} cursor={{ stroke: "var(--color-deep-indigo)", strokeDasharray: "3 3" }} />
            <Line
              type="monotone"
              dataKey={dataKey}
              stroke="var(--mc-chart-line)"
              strokeWidth={1.5}
              dot={data.length > 40 ? false : { r: 3.5, fill: "var(--mc-chart-dot)", stroke: "var(--mc-chart-dot)" }}
              activeDot={{ r: 5, fill: "var(--mc-chart-dot)", stroke: "var(--color-deep-indigo)", strokeWidth: 1 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}

// Every edge is drawn in the one brand indigo; edges are told apart by dash pattern and legend.
const EDGE_DASHES = ["", "7 4", "2 3", "10 3 2 3", "4 4"];
const edgeName = (e) => e.split("|").map(shortId).join(" - ");
const sci = (v) => (v == null ? "-" : Number(v).toExponential(1));

function ResidualTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="chart-tip">
      <span className="chart-tip__round">Round {label}</span>
      {payload.map((p) => (
        <span key={p.dataKey} className="chart-tip__value">
          {edgeName(p.dataKey)}: {sci(p.value)}
        </span>
      ))}
    </div>
  );
}

export function ResidualChart({ series, edges, epsilon, totalRounds }) {
  // log axis: clamp exact zeros (identical snapshots) to a floor instead of dropping the point
  const data = series.map((p) => {
    const q = { round: p.round };
    for (const e of edges) if (p[e] != null) q[e] = Math.max(p[e], 1e-9);
    return q;
  });
  return (
    <div className="residual-chart" data-testid="residual-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 12, right: 16, bottom: 0, left: 4 }}>
          <XAxis dataKey="round" type="number" domain={[1, totalRounds]} allowDecimals={false} axisLine={axis} tickLine={axis} tick={tick} />
          <YAxis scale="log" domain={["auto", "auto"]} allowDataOverflow axisLine={axis} tickLine={axis} tick={tick} tickFormatter={sci} width={56} />
          <Tooltip content={<ResidualTooltip />} cursor={{ stroke: "var(--color-deep-indigo)", strokeDasharray: "3 3" }} />
          <Legend formatter={edgeName} wrapperStyle={{ fontFamily: "var(--font-label)", fontSize: 12, letterSpacing: "0.133em", textTransform: "uppercase" }} />
          {epsilon != null && (
            <ReferenceLine y={epsilon} stroke="var(--color-ink-black)" strokeDasharray="2 4" ifOverflow="extendDomain"
              label={{ value: `epsilon ${epsilon}`, position: "insideBottomLeft", fill: "var(--color-ink-black)", fontSize: 12, fontFamily: "var(--font-body)" }} />
          )}
          {edges.map((e, i) => (
            <Line key={e} type="monotone" dataKey={e} name={e} stroke="var(--mc-chart-line)" strokeWidth={1.5}
              strokeDasharray={EDGE_DASHES[i % EDGE_DASHES.length]} dot={false} isAnimationActive={false} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
