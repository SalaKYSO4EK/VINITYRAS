import type { ReactNode } from "react";

export const COLORS = {
  green: "#B0D84F",
  darkGreen: "#00A63E",
  blue: "#45B7E8",
  lightBlue: "#8FD0FA",
  orange: "#FFC26B",
  pink: "#DB9ACB",
  red: "#E06B63",
  violet: "#7C8FD6",
  line: "#0A8CD6",
  track: "#E3E6EC",
};

export interface Segment {
  label: string;
  value: number;
  color: string;
}

/** Donut chart with centered content */
export function Donut({
  segments,
  size = 170,
  thickness = 22,
  gap = 2,
  children,
}: {
  segments: Segment[];
  size?: number;
  thickness?: number;
  gap?: number;
  children?: ReactNode;
}) {
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  const arcs = segments.reduce<{ seg: Segment; len: number; offset: number }[]>((acc, seg) => {
    const prev = acc[acc.length - 1];
    const offset = prev ? prev.offset + prev.len : 0;
    acc.push({ seg, len: (seg.value / total) * c, offset });
    return acc;
  }, []);

  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={COLORS.track} strokeWidth={thickness} opacity={0.35} />
        <g transform={`rotate(-90 ${size / 2} ${size / 2})`}>
          {arcs.map(({ seg, len, offset }, i) => {
            const dash = Math.max(len - (segments.length > 1 ? gap : 0), 0);
            return (
              <circle
                key={i}
                className="donut-seg"
                style={{ animationDelay: `${i * 90}ms` }}
                cx={size / 2}
                cy={size / 2}
                r={r}
                fill="none"
                stroke={seg.color}
                strokeWidth={thickness}
                strokeDasharray={`${dash} ${c - dash}`}
                strokeDashoffset={-offset}
              >
                <title>{`${seg.label}: ${seg.value}`}</title>
              </circle>
            );
          })}
        </g>
      </svg>
      <div className="absolute inset-0 grid place-items-center text-center pointer-events-none">{children}</div>
    </div>
  );
}

/** Single-value ring (gauge) */
export function Ring({
  value,
  color,
  size = 120,
  thickness = 14,
  children,
}: {
  value: number;
  color: string;
  size?: number;
  thickness?: number;
  children?: ReactNode;
}) {
  return (
    <Donut
      size={size}
      thickness={thickness}
      gap={0}
      segments={[
        { label: "Значение", value, color },
        { label: "Остаток", value: Math.max(0, 100 - value), color: COLORS.track },
      ]}
    >
      {children}
    </Donut>
  );
}

export function Legend({ items }: { items: { label: string; value: string | number; color: string }[] }) {
  return (
    <ul className="space-y-2.5 min-w-0">
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-2.5 text-[.78rem] text-[#3A4559]">
          <span className="legend-dot" style={{ borderColor: it.color }} />
          <span className="truncate">
            {it.label} - <span className="font-medium">{it.value}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Diverging bars: found values go up (green), missing go down (red) */
export function DivergingBars({ data }: { data: { label: string; up: number; down: number }[] }) {
  const W = 400;
  const H = 210;
  const top = 10;
  const bottom = 26;
  const maxUp = Math.max(...data.map((d) => d.up), 1);
  const maxDown = Math.max(...data.map((d) => d.down), 1);
  const unit = (H - top - bottom) / (maxUp + maxDown * 1.6);
  const base = top + maxUp * unit;
  const slot = W / data.length;
  const bw = Math.min(30, slot * 0.55);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img" aria-label="Найдено и пропущено по таблицам">
      <line x1="0" x2={W} y1={base} y2={base} stroke="#D5D9E1" strokeWidth="1" strokeDasharray="3 3" />
      {data.map((d, i) => {
        const x = i * slot + (slot - bw) / 2;
        const upH = d.up * unit;
        const downH = d.down * unit * 1.6;
        return (
          <g key={d.label}>
            <rect className="bar-grow up" style={{ animationDelay: `${i * 70}ms` }} x={x} y={base - upH} width={bw} height={upH} fill={COLORS.green}>
              <title>{`${d.label}: найдено ${d.up}`}</title>
            </rect>
            <rect className="bar-grow" style={{ animationDelay: `${i * 70}ms` }} x={x} y={base + 1} width={bw} height={downH} fill={COLORS.red}>
              <title>{`${d.label}: пропущено ${d.down}`}</title>
            </rect>
            <text x={x + bw / 2} y={H - 8} textAnchor="middle" fontSize="10.5" fill="#8A94A8">
              {d.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** Line chart (confidence by page) */
export function LineChart({ values }: { values: number[] }) {
  const W = 400;
  const H = 210;
  const left = 28;
  const right = 8;
  const top = 12;
  const bottom = 28;
  const min = Math.floor(Math.min(...values) / 5) * 5 - 5;
  const max = Math.min(100, Math.ceil(Math.max(...values) / 5) * 5 + 3);
  const x = (i: number) => left + (values.length === 1 ? 0 : (i / (values.length - 1)) * (W - left - right));
  const y = (v: number) => top + (1 - (v - min) / (max - min)) * (H - top - bottom);
  const path = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const ticks = [min, Math.round((min + max) / 2), max];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img" aria-label="Достоверность по страницам">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke="#E3E6EC" strokeWidth="1" strokeDasharray="3 4" />
          <text x={left - 6} y={y(t) + 3} textAnchor="end" fontSize="10" fill="#8A94A8">
            {t}
          </text>
        </g>
      ))}
      <path d={path} pathLength={1} className="line-draw" fill="none" stroke={COLORS.line} strokeWidth="2.4" strokeLinejoin="round" strokeLinecap="round" />
      {values.map((v, i) => (
        <circle key={i} cx={x(i)} cy={y(v)} r="3" fill="#fff" stroke={COLORS.line} strokeWidth="1.8">
          <title>{`Стр. ${i + 1}: ${v}%`}</title>
        </circle>
      ))}
      <text x={left} y={H - 8} fontSize="10.5" fill="#8A94A8">
        стр. 1
      </text>
      <text x={W - right} y={H - 8} textAnchor="end" fontSize="10.5" fill="#8A94A8">
        стр. {values.length}
      </text>
    </svg>
  );
}
