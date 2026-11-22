import { useEffect, useRef, useState, type FormEvent } from "react";
import { useApp } from "@/components/AppProvider";
import DeerRunner from "@/components/DeerRunner";
import FileDiagnostics from "@/components/FileDiagnostics";
import Forest from "@/components/Forest";
import { ChartIcon, CheckIcon, DocIcon, DownloadIcon, UploadIcon } from "@/components/icons";
import { csvArtifacts, progressDetails, reuseNotice, type FileIssue } from "@/lib/api";
import { tableName } from "@/lib/tables";

const steps = ["Загрузить статьи", "Обработка статей", "Скачать таблицы"];
const uploadLimit = 128 * 1024 * 1024;
const pdfLimit = 64 * 1024 * 1024;
const progressLabels = {
  uploading: "Загружаем и проверяем файлы",
  checking: "Проверяем статус задания",
  queued: "Задание в очереди",
  running: "Модель обрабатывает статьи",
};

export default function HomePage() {
  const { phase, busy, names, jobId, result, error, progress, reused, uploadIssues, fileOutcomes, startProcessing, resetFlow } = useApp();
  const reuse = reuseNotice(reused, progress);
  const input = useRef<HTMLInputElement>(null);
  const criteriaInput = useRef<HTMLTextAreaElement>(null);
  const criteriaFileInput = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [selected, setSelected] = useState<File[]>([]);
  const [criteria, setCriteria] = useState("");
  const [criteriaPdf, setCriteriaPdf] = useState<File | null>(null);
  const [criteriaPages, setCriteriaPages] = useState("");
  const [validation, setValidation] = useState("");
  const [selectionIssues, setSelectionIssues] = useState<FileIssue[]>([]);
  const step = result && phase === "completed" ? 3 : busy ? 2 : 1;
  const tables = result ? csvArtifacts(result).filter((file) => result.archive_files?.includes(file.name) ?? file.row_count > 0) : [];

  useEffect(() => {
    const stop = (event: DragEvent) => {
      if (event.dataTransfer?.types.includes("Files")) event.preventDefault();
    };
    window.addEventListener("dragover", stop);
    window.addEventListener("drop", stop);
    return () => {
      window.removeEventListener("dragover", stop);
      window.removeEventListener("drop", stop);
    };
  }, []);

  useEffect(() => { window.scrollTo({ top: 0 }); }, [step]);

  function addFiles(selection: FileList) {
    if (busy) return;
    const next = [...selected];
    const known = new Set(next.map((file) => file.name.toLowerCase()));
    let size = next.reduce((sum, file) => sum + file.size, 0);
    const skipped: FileIssue[] = [];
    for (const file of selection) {
      let reason = "";
      if (known.has(file.name.toLowerCase())) reason = "Файл с таким именем уже выбран.";
      else if (/\.pdf$/i.test(file.name) && file.size > pdfLimit) reason = "Размер PDF превышает 64 МиБ.";
      else if (next.length >= 100 || size + file.size > uploadLimit) reason = "Превышен лимит загрузки: 100 файлов или 128 МиБ.";
      if (reason) {
        skipped.push({ file: file.name, reason });
        continue;
      }
      next.push(file);
      known.add(file.name.toLowerCase());
      size += file.size;
    }
    setSelected(next);
    setValidation("");
    setSelectionIssues(skipped);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selected.length) {
      setValidation("Выберите хотя бы одну статью или ZIP с публикациями.");
      return;
    }
    const size = selected.reduce((sum, file) => sum + file.size, criteriaPdf?.size ?? 0);
    if (size > uploadLimit) {
      setValidation("Общий размер статей и PDF с критериями превышает 128 МиБ.");
      return;
    }
    setValidation("");
    await startProcessing(selected, criteriaPdf ? { pdf: criteriaPdf, pages: criteriaPages.trim() } : criteria.trim());
  }

  const activeProgress = phase === "queued" || phase === "running" ? progress : undefined;
  const progressTitle = busy ? activeProgress?.label || progressLabels[phase as keyof typeof progressLabels] : "";
  const details = progressDetails(activeProgress);

  return (
    <main className="flex-1 px-4 sm:px-6 pt-8 sm:pt-10 pb-16">
      <div className="max-w-3xl mx-auto text-center">
        <nav className="stepper" aria-label="Шаги">
          {steps.map((label, i) => (
            <span key={label} className="contents">
              {i > 0 && <span className="step-line" />}
              <div className={`step${step === i + 1 ? " active" : ""}${step > i + 1 ? " done" : ""}`}
                aria-current={step === i + 1 ? "step" : undefined}>
                <span className="dot">{step > i + 1 ? <CheckIcon size={16} /> : i + 1}</span>
                <span className="label">{label}</span>
              </div>
            </span>
          ))}
        </nav>
      </div>

      {step === 1 && (
        <section className="view max-w-4xl mx-auto relative">
          <svg className="deer-bg hidden lg:block" style={{ right: -210, top: 40 }} width="320" height="320" aria-hidden="true">
            <use href="#deer" />
          </svg>
          <div className="text-center mt-8 sm:mt-10 relative">
            <h1 className="title">Начните с научных статей</h1>
            <p className="subtitle mt-3">Загрузите PDF или ZIP с публикациями. По умолчанию используется схема для солнечных элементов.</p>
          </div>
          <form className="card p-3 sm:p-4 mt-7 relative" onSubmit={submit}>
            <div className={`dropzone text-center px-4 sm:px-8 py-7${drag ? " drag" : ""}`}
              onDragOver={(event) => { event.preventDefault(); setDrag(true); }}
              onDragLeave={() => setDrag(false)}
              onDrop={(event) => { event.preventDefault(); setDrag(false); addFiles(event.dataTransfer.files); }}>
              <div className="icon-circle mx-auto"><UploadIcon size={28} /></div>
              <h2 className="text-xl font-semibold mt-3">Перетащите статьи сюда</h2>
              <button type="button" className="btn btn-sm mt-4" onClick={() => input.current?.click()}>
                <UploadIcon size={16} /> Выбрать PDF или ZIP
              </button>
              <p className="text-xs text-[var(--navy-500)] mt-3">До 100 PDF · до 128 МиБ на загрузку</p>
              <input ref={input} type="file" accept=".pdf,.zip" multiple hidden onChange={(event) => {
                if (event.target.files?.length) addFiles(event.target.files);
                event.target.value = "";
              }} />
            </div>

            {selected.length > 0 && (
              <ul className="mt-4 space-y-2 max-h-48 overflow-auto" aria-label="Выбранные файлы">
                {selected.map((file) => (
                  <li key={file.name} className="file-row">
                    <span className="file-icon"><DocIcon /></span>
                    <span className="font-medium truncate flex-1 min-w-0">{file.name}</span>
                    <button type="button" className="text-btn text-sm" aria-label={`Убрать ${file.name}`}
                      onClick={() => setSelected(selected.filter((entry) => entry !== file))}>Убрать</button>
                  </li>
                ))}
              </ul>
            )}

            <FileDiagnostics skipped={[...selectionIssues, ...(uploadIssues ?? [])]} outcomes={fileOutcomes} />
            <div className="p-2 sm:px-3 pt-5">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                {criteriaPdf ? <h2 className="font-semibold">PDF с критериями</h2>
                  : <label htmlFor="criteria" className="font-semibold">Свои критерии (необязательно)</label>}
                {!criteriaPdf && <div className="flex flex-wrap gap-2">
                  <button type="button" className="text-btn text-sm" onClick={() => {
                    setCriteria("Извлеки PCE и Voc и укажи источники в PDF");
                    criteriaInput.current?.focus();
                  }}>Вставить пример</button>
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => criteriaFileInput.current?.click()}>
                    <DocIcon size={16} /> Приложить PDF с критериями
                  </button>
                </div>}
              </div>
              <input ref={criteriaFileInput} type="file" accept=".pdf,application/pdf" aria-label="PDF с критериями" hidden onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) {
                  if (!/\.pdf$/i.test(file.name)) setValidation("Выберите файл критериев в формате PDF.");
                  else if (file.size > pdfLimit) setValidation("PDF с критериями превышает 64 МиБ.");
                  else {
                    setCriteriaPdf(file);
                    setValidation("");
                  }
                }
                event.target.value = "";
              }} />
              {criteriaPdf ? (
                <>
                  <div className="file-row mt-2">
                    <span className="file-icon"><DocIcon /></span>
                    <span className="font-medium truncate flex-1 min-w-0">{criteriaPdf.name}</span>
                    <button type="button" className="text-btn text-sm" onClick={() => {
                      setCriteriaPdf(null);
                      setCriteriaPages("");
                      setValidation("");
                    }}>Убрать PDF</button>
                  </div>
                  <label className="block text-sm mt-3" htmlFor="criteria-pages">Страницы критериев</label>
                  <input id="criteria-pages" className="criteria-page-input mt-1" value={criteriaPages} maxLength={300}
                    placeholder="Например, 3-6" onChange={(event) => { setCriteriaPages(event.target.value); setValidation(""); }} />
                  <p className="text-xs text-[var(--navy-500)] mt-1">При пустом поле используются все страницы PDF.</p>
                </>
              ) : <textarea ref={criteriaInput} id="criteria" name="criteria" className="criteria-input mt-2" rows={3}
                maxLength={32000} placeholder="Какие показатели, условия и источники нужно извлечь?" value={criteria}
                onChange={(event) => { setCriteria(event.target.value); setValidation(""); }} />}
              <div className="flex flex-wrap justify-between items-center gap-3 mt-3">
                <div role="status" className="text-sm text-[#8B342C]">
                  {validation && <p>{validation}</p>}
                  {error && error !== validation && <p>{error}</p>}
                  {error && jobId && <>
                    <p className="mt-1 text-[var(--navy-600)]">{selected.length
                      ? "Для повтора проверьте файлы и критерии и нажмите «Обработать статьи»."
                      : "Для повтора заново выберите исходные файлы и укажите критерии, если использовали их."}</p>
                    <a className="text-btn block mt-1" href={`/dashboard/${jobId}`}>Проверить задание</a>
                  </>}
                </div>
                <button className="btn btn-sm ml-auto" type="submit">Обработать статьи</button>
              </div>
            </div>
          </form>
        </section>
      )}

      {step === 2 && (
        <section className="view max-w-3xl mx-auto">
          <div className="text-center mt-10 sm:mt-12">
            <h1 className="title" role="status">{progressTitle}</h1>
            {details && <p className="subtitle mt-3">{details}</p>}
            {phase === "queued" && <p className="subtitle mt-3">Задания выполняются по одному.</p>}
          </div>
          <div className="card p-5 sm:p-8 mt-8 sm:mt-10">
            <div className="progress-slider-wrap" aria-hidden="true">
              <Forest pct={45} />
              <DeerRunner pct={45} running={phase === "running"} />
              <div className="progress-track"><div className="progress-fill progress-indeterminate" /></div>
            </div>
            {names.length > 0 && <ul className="mt-6 space-y-3" aria-label="Файлы задания">
              {names.map((name) => (
                <li className="file-row" key={name}>
                  <span className="file-icon"><DocIcon /></span>
                  <span className="font-medium truncate min-w-0">{name}</span>
                </li>
              ))}
            </ul>}
            <FileDiagnostics skipped={[...selectionIssues, ...(uploadIssues ?? [])]} />
          </div>
        </section>
      )}

      {step === 3 && result && jobId && (
        <section className="view max-w-3xl mx-auto">
          <div className="text-center mt-8 sm:mt-10">
            <div className="icon-circle mx-auto done-badge" style={{ width: 68, height: 68 }}><CheckIcon size={30} /></div>
            <h1 className="title mt-5">Анализ завершён</h1>
            <p className="subtitle mt-3">
              {tables.length ? "Таблицы готовы к просмотру и скачиванию" : `Обработано статей: ${names.length}`}
            </p>
            {reuse && <p className="subtitle mt-3">{reuse}</p>}
          </div>
          <div className="card p-6 sm:p-8 mt-8 text-center">
            <h2 className="text-xl sm:text-2xl font-semibold">
              {tables.length ? "Таблицы и источники" : "Обработка выполнена успешно"}
            </h2>
            <div className="flex flex-wrap justify-center gap-3 mt-5">
              {result.download_url && <a className="btn" href={result.download_url}><DownloadIcon /> Скачать ZIP</a>}
              <a className="btn btn-ghost" href={`/dashboard/${jobId}`}><ChartIcon /> Открыть дашборд</a>
            </div>
            {tables.length > 0 && <ul className="text-left grid grid-cols-1 sm:grid-cols-2 gap-x-6 border-t border-[var(--beige-300)] mt-6 pt-2">
              {tables.map((table) => (
                <li className="zip-item" key={table.name}>
                  <DocIcon size={18} />
                  <a className="text-btn" href={table.url}>{tableName(table.name)}</a>
                  <span className="text-sm text-[var(--navy-500)]">{table.row_count}</span>
                </li>
              ))}
            </ul>}
            {tables.length === 0 && (
              <p className="text-sm text-[var(--navy-600)] mt-4">
                Подробности анализа и источники доступны в дашборде.
              </p>
            )}
            {result.reference_comparison && (
              <p className="text-sm text-[var(--navy-600)] mt-4">
                Сверка CSV завершена. <a className="text-btn" href={`/dashboard/${jobId}#referenceHeading`}>Посмотреть итог сверки</a>
              </p>
            )}
            {result.warnings.length > 0 && <p className="text-sm text-[var(--navy-600)] mt-4">Замечания модели доступны в дашборде.</p>}
            <FileDiagnostics skipped={[...selectionIssues, ...(uploadIssues ?? [])]} outcomes={fileOutcomes} />
          </div>
          <div className="text-center mt-8">
            <button type="button" className="text-btn" onClick={() => { setSelected([]); setSelectionIssues([]); resetFlow(); }}>Загрузить другие статьи</button>
          </div>
        </section>
      )}
    </main>
  );
}
