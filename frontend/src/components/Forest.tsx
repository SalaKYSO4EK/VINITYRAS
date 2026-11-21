const OUTLINE = "#5B2A1E";

/** Cartoon tree in the style of the supplied reference (cloudy crown, brown trunk, grass tuft). */
function Tree({ hue = 0 }: { hue?: number }) {
  return (
    <svg
      viewBox="0 0 100 115"
      width="100%"
      height="100%"
      aria-hidden="true"
      style={hue ? { filter: `hue-rotate(${hue}deg)` } : undefined}
    >
      <g strokeLinejoin="round" strokeLinecap="round" strokeWidth="3">
        {/* grass */}
        <ellipse cx="50" cy="109" rx="34" ry="5.5" fill="#7A9A56" stroke={OUTLINE} />
        <path d="M24 106 L20 99 L28 103 M76 106 L80 99 L72 103" fill="none" stroke={OUTLINE} strokeWidth="2.4" />
        {/* trunk */}
        <path d="M40 109 C44 94 46 82 44 62 L57 62 C55 82 56 94 61 109 Z" fill="#8A4A2A" stroke={OUTLINE} />
        <path d="M50 108 C52 94 52 82 50 68" fill="none" stroke="#6A331C" strokeWidth="2.4" />
        {/* back crown */}
        <g fill="#6E8F4E" stroke={OUTLINE}>
          <circle cx="50" cy="36" r="28" />
          <circle cx="25" cy="54" r="19" />
          <circle cx="75" cy="54" r="19" />
          <circle cx="40" cy="62" r="15" />
          <circle cx="62" cy="62" r="15" />
        </g>
        {/* front crown */}
        <g fill="#7F9F5C" stroke={OUTLINE}>
          <circle cx="52" cy="28" r="19" />
          <circle cx="31" cy="48" r="15" />
          <circle cx="71" cy="49" r="15" />
          <circle cx="50" cy="52" r="16" />
        </g>
        {/* highlights */}
        <g fill="#B5CF96" stroke="none" opacity=".75">
          <ellipse cx="46" cy="20" rx="7" ry="3" transform="rotate(-20 46 20)" />
          <ellipse cx="25" cy="42" rx="5" ry="2.4" transform="rotate(-25 25 42)" />
          <ellipse cx="66" cy="42" rx="6" ry="2.6" transform="rotate(-20 66 42)" />
          <ellipse cx="44" cy="46" rx="5" ry="2.2" transform="rotate(-20 44 46)" />
        </g>
      </g>
    </svg>
  );
}

const TREES = [
  { x: 6, h: 34, hue: 0 },
  { x: 15, h: 44, hue: 8 },
  { x: 25, h: 36, hue: -8 },
  { x: 35, h: 48, hue: 4 },
  { x: 45, h: 38, hue: -6 },
  { x: 55, h: 46, hue: 10 },
  { x: 65, h: 34, hue: -4 },
  { x: 75, h: 44, hue: 6 },
  { x: 85, h: 36, hue: -10 },
  { x: 94, h: 46, hue: 2 },
];

/** Trees planted along the progress strip. They "come alive" once the deer runs past them. */
export default function Forest({ pct }: { pct: number }) {
  return (
    <div className="forest" aria-hidden="true">
      {TREES.map((t, i) => {
        const passed = pct >= t.x;
        return (
          <div key={i} className="tree-pos" style={{ left: `${t.x}%`, width: t.h * 0.87, height: t.h }}>
            <div
              className={`tree-inner${passed ? " passed" : ""}${i % 2 ? " sway" : ""}`}
              style={{ width: "100%", height: "100%" }}
            >
              <Tree hue={t.hue} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
