import { useEffect, useState } from "react";
import type { Result } from "@/lib/api";
import "./findings-map.css";

type Mark = { kind: string; label: string; count: number; details: string[] };
type Findings = { pages: { page: number; marks: Mark[] }[]; warnings?: string[] };

function SourceMap({ jobId, source }: {
  jobId: string; source: Result["sources"][number];
}) {
  const [open, setOpen] = useState(false);
  const [findings, setFindings] = useState<Findings>();
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!open || findings) return;
    const controller = new AbortController();
    const query = new URLSearchParams({ file: source.name });
    async function load() {
      try {
        const response = await fetch(`/api/jobs/${jobId}/findings?${query}`, { signal: controller.signal });
        if (!response.ok) throw new Error();
        const data: Findings = await response.json();
        if (!Array.isArray(data.pages)) throw new Error();
        if (!controller.signal.aborted) setFindings(data);
      } catch {
        if (!controller.signal.aborted) setError("Не удалось загрузить карту находок.");
      }
    }
    setError("");
    void load();
    return () => controller.abort();
  }, [open, findings, jobId, source.name, attempt]);

  return (
    <article className="findings-source">
      <div className="findings-source-head">
        <a className="text-btn" href={source.url} target="_blank" rel="noopener" aria-label={`Открыть PDF ${source.name}`}>{source.name}</a>
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
          <span aria-hidden="true">{open ? "▾" : "▸"}</span> Карта находок
        </button>
      </div>
      {open && <div className="findings-body">
        {error && <div role="status">{error} <button type="button" className="text-btn"
          onClick={() => setAttempt(attempt + 1)}>Повторить</button></div>}
        {!findings && !error && <p role="status">Загружаем страницы…</p>}
        {findings?.warnings?.map((warning) => <p key={warning} role="status">{warning}</p>)}
        {findings?.pages.length === 0 && <p>В сохранённых результатах для этого PDF нет привязок к страницам.</p>}
        {findings && findings.pages.length > 0 && <ol className="findings-pages">
          {findings.pages.map(({ page, marks }) => <li key={page}>
            <span className="findings-page">Стр. {page}</span>
            <div className="findings-items">{marks.map((mark) =>
              <div key={mark.kind} className="findings-item">
                <a className={`findings-mark findings-mark--${mark.kind}`} href={`${source.url}#page=${page}`}
                  target="_blank" rel="noopener" aria-label={`${mark.label}, ${mark.count}. Открыть ${source.name}, страница ${page}`}>
                  {mark.label} <strong>{mark.count}</strong>
                </a>
                {mark.details.length > 0 && <details className="findings-detail">
                  <summary>{mark.details.length > 1 ? `Список (${mark.details.length})` : "Подробнее"}</summary>
                  <ul>{mark.details.map((detail, index) => <li key={`${mark.kind}/${index}`}>{detail}</li>)}</ul>
                </details>}
              </div>)}</div>
          </li>)}
        </ol>}
      </div>}
    </article>
  );
}

export default function FindingsMap({ jobId, sources }: { jobId: string; sources: Result["sources"] }) {
  return <section className="dash-card" aria-labelledby="findings-heading">
    <h2 id="findings-heading" className="text-lg font-semibold">Карта находок по страницам</h2>
    {sources.length === 0 && <p className="findings-empty">Для этого задания нет исходных PDF.</p>}
    {sources.map((source) => <SourceMap key={jobId + "/" + source.name} jobId={jobId} source={source} />)}
  </section>;
}
