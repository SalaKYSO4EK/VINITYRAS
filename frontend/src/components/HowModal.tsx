import { useModalDialog } from "@/lib/use-modal-dialog";
import { useApp } from "./AppProvider";
import { CloseIcon, Deer } from "./icons";

export default function HowModal() {
  const { howOpen, setHowOpen } = useApp();

  const dialog = useModalDialog(howOpen);

  return (
    <dialog ref={dialog} className="modal" aria-labelledby="modalTitle"
      onCancel={(event) => { event.preventDefault(); setHowOpen(false); }}
      onClick={(event) => { if (event.target === event.currentTarget) setHowOpen(false); }}>
      <div className="modal-card">
        <button className="absolute top-4 right-4 w-9 h-9 rounded-full grid place-items-center hover:bg-[var(--beige-200)] transition cursor-pointer" onClick={() => setHowOpen(false)} aria-label="Закрыть">
          <CloseIcon />
        </button>
        <div className="flex items-center gap-3 mb-3">
          <span className="logo-badge" style={{ background: "var(--navy-900)", color: "var(--beige-100)" }}>
            <Deer size={32} />
          </span>
          <h2 className="text-2xl font-bold" id="modalTitle">Инструкция</h2>
        </div>
        <p className="text-[var(--navy-600)] text-sm mb-5">NEXUS извлекает данные из научных PDF и собирает их в таблицы.</p>
        <ol className="space-y-3">
          <li className="strict-step">
            <span className="n">1</span>
            <div>
              <b className="text-[var(--navy-900)]">Загрузите PDF или ZIP</b>
              <p className="text-sm text-[var(--navy-600)] mt-0.5">Выберите статьи или один архив с публикациями.</p>
            </div>
          </li>
          <li className="strict-step">
            <span className="n">2</span>
            <div>
              <b className="text-[var(--navy-900)]">Запустите обработку</b>
              <p className="text-sm text-[var(--navy-600)] mt-0.5">При необходимости задайте свои критерии. Пустое поле включает схему для солнечных элементов.</p>
            </div>
          </li>
          <li className="strict-step">
            <span className="n">3</span>
            <div>
              <b className="text-[var(--navy-900)]">Скачайте ZIP с таблицами</b>
              <p className="text-sm text-[var(--navy-600)] mt-0.5">В дашборде можно открыть CSV, замечания и страницы PDF.</p>
            </div>
          </li>
        </ol>
        <div className="mt-4 p-3.5 rounded-2xl bg-[var(--beige-200)] text-xs text-[var(--navy-800)] leading-relaxed">
          Ссылки на задания доступны в <b>истории</b> этого браузера.
        </div>
        <button className="btn w-full mt-5" onClick={() => setHowOpen(false)}>Понятно</button>
      </div>
    </dialog>
  );
}
