import { useModalDialog } from "@/lib/use-modal-dialog";
import { statusLabel } from "@/lib/api";
import { useApp } from "./AppProvider";
import { ChartIcon, CloseIcon, DocIcon, DownloadIcon, UserIcon } from "./icons";

export default function AccountDrawer() {
  const { accountOpen, closeAccount, accountTab, setAccountTab, history, clearHistory, resetFlow } = useApp();

  const dialog = useModalDialog(accountOpen);

  const tabs = [
    { key: "all", label: "Все" },
    { key: "pending", label: "Незавершённые" },
    { key: "completed", label: "Готовые" },
  ] as const;
  const shown = history.filter((item) => accountTab === "all" || (item.status === "completed") === (accountTab === "completed"));

  return (
      <dialog ref={dialog} className="account-drawer" aria-labelledby="accountDrawerTitle"
        onCancel={(event) => { event.preventDefault(); closeAccount(); }}
        onClick={(event) => {
          if (event.target !== event.currentTarget) return;
          const box = event.currentTarget.getBoundingClientRect();
          if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeAccount();
        }}>
        <div className="p-5 sm:p-6 bg-[var(--navy-900)] text-[var(--beige-100)] flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-[var(--beige-100)] text-[var(--navy-900)] grid place-items-center"><UserIcon size={20} /></div>
            <div>
              <h2 className="text-lg font-bold leading-tight" id="accountDrawerTitle">История заданий</h2>
              <p className="text-xs text-[#B8AE9A]">В этом браузере</p>
            </div>
          </div>
          <button type="button" className="w-9 h-9 rounded-full grid place-items-center hover:bg-white/10 cursor-pointer"
            onClick={closeAccount} aria-label="Закрыть историю"><CloseIcon /></button>
        </div>

        <div className="px-5 pt-4 pb-2 border-b border-[var(--beige-300)] bg-[var(--beige-100)]">
          <div className="flex gap-1 p-1 bg-[var(--beige-200)] rounded-2xl" aria-label="Фильтр истории">
            {tabs.map((tab) => (
              <button key={tab.key} type="button" aria-pressed={accountTab === tab.key}
                className={`account-tab${accountTab === tab.key ? " active" : ""}`} onClick={() => setAccountTab(tab.key)}>
                {tab.label}
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-5 space-y-2.5">
          {shown.length === 0 && <p className="text-center py-8 rounded-2xl border border-dashed border-[var(--beige-300)] text-sm text-[var(--navy-500)]">
            Здесь появятся отправленные задания.
          </p>}
          {shown.map((item) => (
            <div className="account-item history-item" key={item.id}>
              <div className="flex items-center gap-3 min-w-0">
                <span className="file-icon shrink-0" style={{ width: 38, height: 38 }}><DocIcon /></span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[var(--navy-900)] truncate">{item.files.join(", ")}</p>
                  <p className="text-xs text-[var(--navy-500)]">
                    {new Date(item.createdAt).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" })}
                    {item.tablesCount !== undefined && ` · ${item.tablesCount} CSV`}
                  </p>
                </div>
                <span className="text-xs font-medium text-[var(--navy-600)]">{statusLabel[item.status]}</span>
              </div>
              <div className="flex flex-wrap gap-2 mt-2.5">
                <a className="btn btn-sm flex-1" href={`/dashboard/${item.id}`} onClick={closeAccount}>
                  <ChartIcon size={15} /> {item.status === "completed" ? "Дашборд" : "Открыть задание"}
                </a>
                {item.status === "completed" && item.hasArchive !== false && <a className="btn btn-sm btn-ghost" href={`/api/jobs/${item.id}/download`}>
                  <DownloadIcon size={15} /> ZIP
                </a>}
              </div>
            </div>
          ))}
        </div>

        <div className="p-4 border-t border-[var(--beige-300)] bg-[var(--beige-100)] flex items-center justify-between gap-3">
          <a href="/" className="btn btn-sm" onClick={() => { closeAccount(); resetFlow(); }}>К загрузке статей</a>
          {history.length > 0 && <button type="button" className="text-btn text-xs" onClick={clearHistory}>Очистить список</button>}
        </div>
      </dialog>
  );
}
