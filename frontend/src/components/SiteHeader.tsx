import { useApp } from "./AppProvider";
import { Deer, UserIcon } from "./icons";

const connectionLabel = {
  checking: "Проверяем подключение",
  ready: "Пакет модели настроен",
  "no-model": "Пакет модели не подключён",
  unavailable: "Сервер недоступен",
};

export default function SiteHeader() {
  const { history, connection, openAccount, setHowOpen } = useApp();

  return (
    <header className="site-header">
      {/* Full-width row: logo hugs the left edge, account + instruction hug the right edge */}
      <div className="w-full h-[76px] px-3 sm:px-5 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <a href="/" className="flex items-center gap-2.5 min-w-0" aria-label="NEXUS — на главную">
            <span className="logo-badge">
              <Deer size={32} />
            </span>
            <span className="text-lg sm:text-xl font-semibold tracking-[.12em]">NEXUS</span>
          </a>
          <span className="org hidden md:block ml-1">ВИНИТИ / ЮФУ</span>
        </div>

        <div className="flex items-center gap-1.5 sm:gap-2 flex-none">
          <span role="status" className="model-status hidden md:inline-flex" data-connection={connection}>{connectionLabel[connection]}</span>
          <button type="button" className="link-btn" onClick={() => setHowOpen(true)}>
            Инструкция
          </button>
          <button type="button" className="link-btn account-btn" onClick={() => openAccount()} aria-label="Открыть историю заданий">
            <UserIcon />
            <span className="hidden sm:inline">История</span>
            <span className="account-badge">{history.length}</span>
          </button>
        </div>
      </div>
    </header>
  );
}
