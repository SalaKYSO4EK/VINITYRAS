export default function DeerRunner({ pct, running }: { pct: number; running: boolean }) {
  return (
    <div className={`deer-runner${running ? " running" : ""}`} style={{ left: `${pct}%` }} aria-hidden="true">
      <svg viewBox="0 0 120 90" width="76" height="58" fill="none" xmlns="http://www.w3.org/2000/svg">
        <g className="deer-body-group">
          <g className="deer-back-legs">
            <path d="M34 50 L24 64 L16 78" stroke="#8C4A27" strokeWidth="5.5" strokeLinecap="round" strokeLinejoin="round" />
            <polygon points="13,78 19,76 17,82 11,81" fill="#4A2518" />
            <path d="M38 48 C34 58 38 66 47 77" stroke="#C87533" strokeWidth="6" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M38 48 C34 58 38 66 47 77" stroke="#4A2518" strokeWidth="1.6" strokeLinecap="round" />
            <polygon points="45,76 51,76 50,82 44,81" fill="#4A2518" />
          </g>
          <g className="deer-front-legs">
            <path d="M72 50 L82 63 L74 74" stroke="#9B532B" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
            <polygon points="70,73 76,73 74,78 68,77" fill="#4A2518" />
            <path d="M76 49 L92 64 L103 75" stroke="#D4823E" strokeWidth="5.5" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M76 49 L92 64 L103 75" stroke="#4A2518" strokeWidth="1.5" strokeLinecap="round" />
            <polygon points="101,74 107,74 106,80 100,78" fill="#4A2518" />
          </g>
          <g className="deer-tail">
            <path d="M27 42 C18 40 16 33 19 28 C22 33 25 36 30 39 Z" fill="#F4DEC2" stroke="#4A2518" strokeWidth="1.8" strokeLinejoin="round" />
            <path d="M27 40 C20 38 18 33 19 28" stroke="#C87533" strokeWidth="3" strokeLinecap="round" />
          </g>
          <path d="M27 42 C32 35 52 36 68 35 C74 34 80 24 83 22 C89 24 90 36 84 48 C79 56 69 55 58 54 C46 53 35 54 27 42 Z" fill="#D4823E" stroke="#4A2518" strokeWidth="2" strokeLinejoin="round" />
          <path d="M42 49 C52 53 66 54 77 50 C82 45 86 35 85 27 C88 33 85 46 79 52 C68 56 52 55 41 51 Z" fill="#F4DEC2" />
          {[
            [42, 40], [49, 39], [56, 40], [63, 39], [46, 44], [53, 44], [60, 44], [68, 42],
          ].map(([cx, cy], i) => (
            <circle key={i} cx={cx} cy={cy} r="1.7" fill="#F3BA73" />
          ))}
          <path d="M84 19 C75 14 64 10 56 4 M67 12 L64 4 M76 15 L74 7 M86 19 C82 12 79 8 81 3" stroke="#5A3222" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M82 21 C77 17 73 18 74 22 C75 24 79 25 83 23 Z" fill="#F4DEC2" stroke="#4A2518" strokeWidth="1.8" />
          <path d="M81 21 C87 18 95 21 101 26 C103 28 102 31 98 32 C92 33 86 31 82 27 Z" fill="#D4823E" stroke="#4A2518" strokeWidth="2" strokeLinejoin="round" />
          <ellipse cx="101" cy="27.5" rx="2.4" ry="1.8" fill="#4A2518" />
          <circle cx="91" cy="24.5" r="2" fill="#3B1E14" />
          <circle cx="91.6" cy="24" r="0.7" fill="#FFFFFF" />
        </g>
      </svg>
    </div>
  );
}
