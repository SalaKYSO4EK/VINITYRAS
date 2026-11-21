import { useEffect, useMemo, useRef, useState } from "react";
import { csvArtifacts, getTable, populatedColumns, type CsvArtifact, type Job, type Result, type TablePage } from "@/lib/api";
import { columnName, tableName } from "@/lib/tables";
import { COLORS, Donut, Legend } from "./charts";
import { DownloadIcon } from "./icons";

const palette = [COLORS.blue, COLORS.darkGreen, COLORS.orange, COLORS.lightBlue, COLORS.pink, COLORS.violet, COLORS.green];
const pageSize = 25;
const firstColumns = ["metric_name", "metric_value", "metric_unit", "source_page", "source_table", "device_id"];

function ResultTable({ tables, sources }: { tables: CsvArtifact[]; sources: Result["sources"] }) {
  const first = tables.find((file) => file.name === "results.csv" && file.row_count > 0)
    ?? tables.find((file) => file.row_count > 0) ?? tables[0];
  const [name, setName] = useState(first.name);
  const [offset, setOffset] = useState(0);
  const [loaded, setLoaded] = useState<{ name: string; offset: number; page?: TablePage; columns?: string[]; error?: string }>();
  const columnsByTable = useRef(new Map<string, string[]>());
  const table = tables.find((file) => file.name === name) ?? first;
  const current = loaded?.name === table.name && loaded.offset === offset ? loaded : undefined;
  const page = current?.page;
  const sourcesByName = useMemo(() => new Map(sources.map((source) => [source.name, source.url])), [sources]);
  const sourcesById = useMemo(() => new Map(sources.map((source) => [source.publication_id, source.url])), [sources]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    async function load() {
      try {
        const data = await getTable(table, offset, pageSize, signal);
        const columns = columnsByTable.current.get(table.name) ?? await populatedColumns(table, data, signal);
        if (signal.aborted) return;
        columnsByTable.current.set(table.name, columns);
        setLoaded({ name: table.name, offset, page: data, columns });
      } catch (error) {
        if (!signal.aborted) {
          setLoaded({ name: table.name, offset, error: error instanceof Error ? error.message : "Не удалось показать таблицу." });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [table, offset]);

  function sourcePage(row: Record<string, string>) {
    const url = sourcesByName.get(row.source_file) || sourcesById.get(row.publication_id);
    return url && /^[1-9]\d*$/.test(row.source_page) ? `${url}#page=${row.source_page}` : undefined;
  }

  const visible = current?.columns;
  const columns = visible && table.name === "results.csv"
    ? [...firstColumns.filter((column) => visible.includes(column)), ...visible.filter((column) => !firstColumns.includes(column))]
    : visible;

  return (
    <section className="dash-card" aria-labelledby="tablesHeading">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold" id="tablesHeading">Таблицы результатов</h2>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="resultTable">Выбрать таблицу</label>
          <select id="resultTable" className="dash-select" value={table.name} onChange={(event) => {
            setName(event.target.value);
            setOffset(0);
          }}>
            {tables.map((file) => <option value={file.name} key={file.name}>{tableName(file.name)} ({file.row_count})</option>)}
          </select>
          <a className="btn btn-sm btn-ghost" href={table.url}><DownloadIcon size={15} /> Скачать CSV</a>
        </div>
      </div>
      {current?.error && <p className="mt-4 text-sm text-[#8B342C]" role="status">{current.error}</p>}
      {!current && <p className="mt-4 text-sm text-[var(--navy-600)]" role="status">Загружаем таблицу…</p>}
      {page && columns?.length === 0 && (
        <p className="mt-4 text-sm text-[var(--navy-600)]">
          {page.total ? "В таблице нет заполненных ячеек." : "В этой таблице нет строк."}
        </p>
      )}
      {page && columns && columns.length > 0 && (
        <>
          <div className="result-table-area mt-4" tabIndex={0} aria-label="Просмотр таблицы">
            <table className="result-table">
              <thead><tr>{columns.map((column) => <th key={column} scope="col">{columnName(column)}</th>)}</tr></thead>
              <tbody>
                {page.rows.map((row, index) => {
                  const url = sourcePage(row);
                  return (
                    <tr key={page.offset + index}>
                      {columns.map((column) => (
                        <td key={column}>
                          {column === "source_page" && url
                            ? <a className="text-btn" href={url} target="_blank" rel="noopener" aria-label={`Страница ${row[column]} в PDF`}>{row[column]}</a>
                            : row[column] || "—"}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {page.total > 0 && <div className="flex flex-wrap items-center justify-between gap-3 mt-3 text-sm text-[var(--navy-600)]">
            <span aria-live="polite">{page.offset + 1}–{page.offset + page.rows.length} из {page.total} строк</span>
            <div className="flex gap-2">
              <button type="button" className="btn btn-sm btn-ghost" disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - pageSize))}>Назад</button>
              <button type="button" className="btn btn-sm btn-ghost" disabled={offset + pageSize >= page.total}
                onClick={() => setOffset(offset + pageSize)}>Далее</button>
            </div>
          </div>}
        </>
      )}
    </section>
  );
}

export default function ReportDashboard({ job, result }: { job: Job; result: Result }) {
  const tables = useMemo(() => csvArtifacts(result), [result]);
  const segments = tables.map((file, i) => ({ label: tableName(file.name), value: file.row_count, color: palette[i % palette.length] }));
  const rows = tables.reduce((sum, file) => sum + file.row_count, 0);
  const seconds = (Date.parse(job.finished_at ?? "") - Date.parse(job.started_at ?? "")) / 1000;
  const stats = [
    { label: "Входных PDF", value: result.sources.length },
    { label: "Таблиц CSV", value: tables.length },
    { label: "Время обработки", value: Number.isFinite(seconds) && seconds >= 0 ? `${seconds} с` : "—" },
  ];

  return (
    <div className="dash-board space-y-3">
      <div className="grid grid-cols-1 lg:grid-cols-[1.2fr_1fr] gap-3">
        <section className="dash-card flex flex-wrap items-center justify-center gap-5" aria-label="Распределение строк по таблицам">
          <Donut size={152} thickness={20} segments={segments.filter((segment) => segment.value > 0)}>
            <div><p className="text-3xl font-bold leading-none">{rows}</p><p className="text-xs mt-1">Строк в таблицах</p></div>
          </Donut>
          <Legend items={segments} />
        </section>
        <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {stats.map((stat) => <div className="dash-card dash-kpi" key={stat.label}>
            <dt className="l order-1">{stat.label}</dt><dd className="v">{stat.value}</dd>
          </div>)}
        </dl>
      </div>

      {result.warnings.length > 0 && (
        <section className="dash-card" aria-labelledby="warningsHeading">
          <h2 className="text-base font-semibold" id="warningsHeading">Замечания модели</h2>
          <ul className="list-disc pl-5 mt-2 space-y-1 text-sm text-[var(--navy-600)]">
            {result.warnings.map((warning, i) => <li key={i}>{warning}</li>)}
          </ul>
        </section>
      )}

      {tables.length > 0 && <ResultTable key={job.job_id} tables={tables} sources={result.sources} />}
      {result.sources.length > 0 && (
        <details className="dash-card">
          <summary className="font-semibold cursor-pointer">Исходные PDF</summary>
          <ul className="mt-2 space-y-2">
            {result.sources.map((source) => <li key={source.name}>
              <a className="text-btn" href={source.url} target="_blank" rel="noopener">{source.name}</a>
            </li>)}
          </ul>
        </details>
      )}
    </div>
  );
}
