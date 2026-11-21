import { useApp } from "./AppProvider";
import { Deer } from "./icons";

export default function SiteFooter() {
  const { toastMsg, toastVisible } = useApp();
  return (
    <>
      <footer className="bg-[var(--navy-950)] text-[var(--beige-200)] border-t border-white/5">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 py-6 flex items-center gap-3 text-sm">
          <Deer size={26} className="text-[var(--beige-100)]" />
          <span className="tracking-[.12em] font-semibold">NEXUS</span>
          <span className="text-[#B8AE9A]">ВИНИТИ / ЮФУ</span>
        </div>
      </footer>
      <div className={`toast${toastVisible ? " show" : ""}`} role="status">{toastVisible ? toastMsg : ""}</div>
    </>
  );
}
