import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";

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
              dot={{ r: 3.5, fill: "var(--mc-chart-dot)", stroke: "var(--mc-chart-dot)" }}
              activeDot={{ r: 5, fill: "var(--mc-chart-dot)", stroke: "var(--color-deep-indigo)", strokeWidth: 1 }}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </figure>
  );
}
