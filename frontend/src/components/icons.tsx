type P = { size?: number; className?: string };

const base = (size: number) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
});

export const DocIcon = ({ size = 20, className }: P) => (
  <svg {...base(size)} strokeWidth="1.7" className={className} aria-hidden="true">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5M9 13h6M9 17h6" />
  </svg>
);

export const CheckIcon = ({ size = 22, className }: P) => (
  <svg {...base(size)} strokeWidth="2.2" className={className} aria-hidden="true">
    <path d="M5 12.5 10 17.5 19 7.5" />
  </svg>
);

export const DownloadIcon = ({ size = 20, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M12 4v12m0 0 5-5m-5 5-5-5" />
    <path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
  </svg>
);

export const UploadIcon = ({ size = 20, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M12 16V4m0 0L7 9m5-5 5 5" />
    <path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
  </svg>
);

export const UserIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M20 21a8 8 0 0 0-16 0" />
    <circle cx="12" cy="7" r="4" />
  </svg>
);

export const CloseIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);

export const ChartIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />
  </svg>
);

export const ArrowLeftIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <path d="M19 12H5m0 0 6-6m-6 6 6 6" />
  </svg>
);

export const InfoIcon = ({ size = 18, className }: P) => (
  <svg {...base(size)} strokeWidth="2" className={className} aria-hidden="true">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </svg>
);

/** SVG sprite with the NEXUS deer emblem (referenced as #deer) */
export function DeerSprite() {
  return (
    <svg width="0" height="0" style={{ position: "absolute" }} aria-hidden="true">
      <defs>
        <symbol id="deer" viewBox="0 0 100 100">
          <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round">
            <path d="M39 46 L30 32 L24 14 M30 32 L16 26 M30 32 L38 20 M24 14 L17 10 M16 26 L9 22" />
            <path d="M61 46 L70 32 L76 14 M70 32 L84 26 M70 32 L62 20 M76 14 L83 10 M84 26 L91 22" />
            <circle cx="17" cy="10" r="2.6" fill="#FBF8F2" />
            <circle cx="9" cy="22" r="2.6" fill="#FBF8F2" />
            <circle cx="38" cy="20" r="2.6" fill="#FBF8F2" />
            <circle cx="83" cy="10" r="2.6" fill="#FBF8F2" />
            <circle cx="91" cy="22" r="2.6" fill="#FBF8F2" />
            <circle cx="62" cy="20" r="2.6" fill="#FBF8F2" />
            <path d="M35 49 L12 45 L22 58 L38 58 Z" />
            <path d="M65 49 L88 45 L78 58 L62 58 Z" />
            <path d="M35 49 L50 40 L65 49 L62 72 L50 90 L38 72 Z" />
            <path d="M38 72 L50 66 L62 72 M50 66 L50 90" />
          </g>
          <circle cx="24" cy="14" r="3" fill="#E8A53A" />
          <circle cx="76" cy="14" r="3" fill="#E8A53A" />
          <circle cx="30" cy="32" r="2.6" fill="#E8A53A" />
          <circle cx="70" cy="32" r="2.6" fill="#E8A53A" />
          <polygon points="50,66 62,72 50,90" fill="#E8A53A" />
          <polygon points="76,50 88,45 78,58" fill="#E8A53A" />
          <polygon points="44,75 56,75 50,81" fill="currentColor" />
          <circle cx="50" cy="55" r="9.5" fill="#FBF8F2" stroke="#E8A53A" strokeWidth="2.2" />
          <g stroke="currentColor" strokeWidth="1.2" fill="none">
            <path d="M50 55 L46 52 M50 55 L54 52 M50 55 L50 60" />
          </g>
          <g fill="#FBF8F2" stroke="currentColor" strokeWidth="1.1">
            <circle cx="46" cy="52" r="1.6" />
            <circle cx="54" cy="52" r="1.6" />
            <circle cx="50" cy="60" r="1.6" />
            <circle cx="50" cy="55" r="1.8" />
          </g>
          <path d="M57 62 L68 73" stroke="currentColor" strokeWidth="3.4" strokeLinecap="round" />
        </symbol>
      </defs>
    </svg>
  );
}

export const Deer = ({ size, className }: { size: number; className?: string }) => (
  <svg width={size} height={size} className={className} aria-hidden="true">
    <use href="#deer" />
  </svg>
);
