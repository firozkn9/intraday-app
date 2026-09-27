// Minimal dependency-free SVG line chart (equity curves, intraday P&L).

export function LineChart({
  points,
  height = 160,
  label,
}: {
  points: { x: string; y: number }[];
  height?: number;
  label: string;
}) {
  if (points.length < 2) return <p className="text-sm text-muted">Not enough data for a chart yet.</p>;
  const w = 600;
  const ys = points.map((p) => p.y);
  const min = Math.min(...ys);
  const max = Math.max(...ys);
  const span = max - min || 1;
  const path = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = height - 8 - ((p.y - min) / span) * (height - 16);
      return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const up = (ys[ys.length - 1] ?? 0) >= (ys[0] ?? 0);
  const firstX = points[0]?.x ?? "";
  const lastX = points[points.length - 1]?.x ?? "";
  return (
    <figure>
      <svg viewBox={`0 0 ${w} ${height}`} role="img" aria-label={label} className="h-40 w-full" preserveAspectRatio="none">
        <path d={path} fill="none" strokeWidth={2} vectorEffect="non-scaling-stroke"
          className={up ? "stroke-ok" : "stroke-danger"} />
      </svg>
      <figcaption className="flex justify-between text-xs text-muted">
        <span>{firstX}</span>
        <span>
          min {min.toLocaleString("en-US", { maximumFractionDigits: 2 })} · max{" "}
          {max.toLocaleString("en-US", { maximumFractionDigits: 2 })}
        </span>
        <span>{lastX}</span>
      </figcaption>
    </figure>
  );
}

export function Table({ head, children, empty }: { head: string[]; children: React.ReactNode; empty?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted">
            {head.map((h) => (
              <th key={h} className="px-2 py-1.5 font-medium">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
      {empty && <p className="px-2 py-3 text-sm text-muted">Nothing to show.</p>}
    </div>
  );
}
