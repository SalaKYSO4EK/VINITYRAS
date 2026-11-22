import { useEffect, useMemo, useRef, useState } from "react";
import {
  csvArtifacts, getSelectedCsv, getTable, locateTableRow, populatedColumns, RequestError,
  type CsvArtifact, type Job, type Result, type SelectedRow, type TablePage, type TableQuery,
} from "@/lib/api";
import { columnName, tableName } from "@/lib/tables";
import { COLORS, Donut, Legend } from "./charts";
import FindingsMap from "./FindingsMap";
import { DownloadIcon } from "./icons";

const palette = [COLORS.blue, COLORS.darkGreen, COLORS.orange, COLORS.lightBlue, COLORS.pink, COLORS.violet, COLORS.green];
const pageSize = 25;
const firstColumns = ["metric_name", "metric_value", "metric_unit", "source_page", "source_table", "device_id"];
const rowIdPattern = /^[a-f0-9]{32}$/;

interface TableView {
  table: string;
  offset: number;
  query: TableQuery;
  rowId?: string;
  notice?: string;
}

interface LoadedTable {
  view: TableView;
  page?: TablePage;
  columns?: string[];
  error?: string;
  rowId?: string;
  notice?: string;
}

function initialTableView(tables: CsvArtifact[], first: CsvArtifact): TableView {
  const params = new URLSearchParams(window.location.search);
  const name = params.get("table");
  const rowId = params.get("row");
  const view = { table: first.name, offset: 0, query: {} };
  if (!params.has("table") && !params.has("row")) return view;
  const target = tables.find((file) => file.name === name);
  if (!target || !rowId || !rowIdPattern.test(rowId)) {
    return { ...view, notice: "Строка по ссылке недоступна. Проверьте адрес." };
  }
  return { ...view, table: target.name, rowId };
}

async function copyLink(url: string) {
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(url);
      return true;
    } catch {
      // Local network addresses may not have a clipboard permission.
    }
  }

  const previous = document.activeElement;
  const field = document.createElement("textarea");
  field.value = url;
  field.readOnly = true;
  field.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.append(field);
  field.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
    if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
  }
}

function selectionKey(row: SelectedRow) {
  return `${row.table}/${row.row_id}`;
}

function ResultTable({ tables, sources, selectedUrl, dashboardUrl }: {
  tables: CsvArtifact[];
  sources: Result["sources"];
  selectedUrl?: string;
  dashboardUrl?: string;
}) {
  const first = tables.find((file) => file.name === "results.csv" && file.row_count > 0)
    ?? tables.find((file) => file.row_count > 0) ?? tables[0];
  const [view, setView] = useState(() => initialTableView(tables, first));
  const [loaded, setLoaded] = useState<LoadedTable>();
  const [filterText, setFilterText] = useState("");
  const [filterColumn, setFilterColumn] = useState("");
  const [selected, setSelected] = useState(() => new Map<string, SelectedRow>());
  const [exporting, setExporting] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const [shareMessage, setShareMessage] = useState("");
  const [manualLink, setManualLink] = useState("");
  const columnsByTable = useRef(new Map<string, string[]>());
  const linkedRow = useRef<HTMLTableRowElement>(null);
  const manualField = useRef<HTMLInputElement>(null);
  const pageCheckbox = useRef<HTMLInputElement>(null);
  const download = useRef<AbortController | undefined>(undefined);
  const table = tables.find((file) => file.name === view.table) ?? first;
  const current = loaded?.view === view ? loaded : undefined;
  const page = current?.page;
  const sourcesByName = useMemo(() => new Map(sources.map((source) => [source.name, source.url])), [sources]);
  const sourcesById = useMemo(() => new Map(sources.map((source) => [source.publication_id, source.url])), [sources]);

  useEffect(() => {
    function resetFromUrl() {
      setView(initialTableView(tables, first));
      setFilterText("");
      setFilterColumn("");
    }
    window.addEventListener("popstate", resetFromUrl);
    return () => window.removeEventListener("popstate", resetFromUrl);
  }, [tables, first]);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;

    async function load() {
      let offset = view.offset;
      let notice = view.notice;
      let rowId = view.rowId;
      try {
        if (rowId) {
          try {
            offset = (await locateTableRow(table, rowId, pageSize, signal)).offset;
          } catch (error) {
            if (!(error instanceof RequestError) || error.status !== 404) throw error;
            notice = "Эта строка больше недоступна в таблице.";
            rowId = undefined;
          }
        }
        const data = await getTable(table, offset, pageSize, signal, view.query);
        let columns = columnsByTable.current.get(table.name);
        if (!columns) {
          const sample = view.query.filter && !table.populated_columns
            ? await getTable(table, 0, pageSize, signal)
            : data;
          columns = await populatedColumns(table, sample, signal);
        }
        if (signal.aborted) return;
        columnsByTable.current.set(table.name, columns);
        if (rowId && !data.row_ids?.includes(rowId)) {
          notice = "Эта строка больше недоступна в таблице.";
          rowId = undefined;
        }
        setLoaded({ view, page: data, columns, rowId, notice });
      } catch (error) {
        if (!signal.aborted) {
          setLoaded({ view, error: error instanceof Error ? error.message : "Не удалось показать таблицу." });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [table, view]);

  useEffect(() => {
    if (!current?.rowId || !linkedRow.current) return;
    linkedRow.current.focus({ preventScroll: true });
    linkedRow.current.scrollIntoView({ block: "center", inline: "nearest" });
  }, [current]);

  useEffect(() => {
    if (!manualLink) return;
    manualField.current?.focus();
    manualField.current?.select();
  }, [manualLink]);

  useEffect(() => () => download.current?.abort(), []);

  function sourcePage(row: Record<string, string>) {
    const url = sourcesByName.get(row.source_file) || sourcesById.get(row.publication_id);
    return url && /^[1-9]\d*$/.test(row.source_page) ? `${url}#page=${row.source_page}` : undefined;
  }

  function updateView(offset: number, query = view.query) {
    setView({ table: table.name, offset, query });
  }

  function changeTable(name: string) {
    setFilterText("");
    setFilterColumn("");
    setView({ table: name, offset: 0, query: {} });
  }

  function sortBy(column: string) {
    const direction = view.query.sort === column && view.query.direction !== "desc" ? "desc" : "asc";
    updateView(0, { ...view.query, sort: column, direction });
  }

  function markRows(ids: string[], checked: boolean) {
    setExportMessage("");
    setSelected((previous) => {
      const next = new Map(previous);
      for (const id of ids) {
        const pick = { table: table.name, row_id: id };
        const key = selectionKey(pick);
        if (checked) next.set(key, pick);
        else next.delete(key);
      }
      return next;
    });
  }

  async function share(rowId: string) {
    const url = new URL(dashboardUrl || window.location.pathname, window.location.origin);
    url.searchParams.set("table", table.name);
    url.searchParams.set("row", rowId);
    const copied = await copyLink(url.href);
    setManualLink(copied ? "" : url.href);
    setShareMessage(copied ? "Ссылка на строку скопирована." : "Скопируйте ссылку из поля.");
  }

  async function exportSelected() {
    if (!selectedUrl || !selected.size || download.current) return;
    const controller = new AbortController();
    download.current = controller;
    setExporting(true);
    setExportMessage("");
    try {
      const file = await getSelectedCsv(selectedUrl, Array.from(selected.values()), controller.signal);
      if (controller.signal.aborted) return;
      const href = URL.createObjectURL(file);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "NEXUS-selected.csv";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
      setExportMessage("CSV выбранных строк скачан.");
    } catch (error) {
      if (!controller.signal.aborted) {
        setExportMessage(error instanceof Error ? error.message : "Не удалось скачать выбранные строки.");
      }
    } finally {
      if (!controller.signal.aborted) {
        download.current = undefined;
        setExporting(false);
      }
    }
  }

  const visible = current?.columns ?? columnsByTable.current.get(table.name);
  const columns = visible && table.name === "results.csv"
    ? [...firstColumns.filter((column) => visible.includes(column)), ...visible.filter((column) => !firstColumns.includes(column))]
    : visible;
  const ids = page?.row_ids ?? [];
  const selectedOnPage = ids.filter((id) => selected.has(selectionKey({ table: table.name, row_id: id }))).length;
  const allSelected = ids.length > 0 && selectedOnPage === ids.length;

  useEffect(() => {
    if (pageCheckbox.current) pageCheckbox.current.indeterminate = selectedOnPage > 0 && !allSelected;
  }, [page, selectedOnPage, allSelected]);

  return (
    <section className="dash-card" aria-labelledby="tablesHeading">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold" id="tablesHeading">Таблицы результатов</h2>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="resultTable">Выбрать таблицу</label>
          <select id="resultTable" className="dash-select" value={table.name} onChange={(event) => changeTable(event.target.value)}>
            {tables.map((file) => <option value={file.name} key={file.name}>{tableName(file.name)} ({file.row_count})</option>)}
          </select>
          <a className="btn btn-sm btn-ghost" href={table.url}><DownloadIcon size={15} /> Скачать CSV</a>
        </div>
      </div>
      {selectedUrl && <div className="result-actions mt-3">
        <button type="button" className="btn btn-sm btn-ghost"
          disabled={!selected.size || exporting} onClick={() => void exportSelected()}>
          <DownloadIcon size={15} /> <span aria-live="polite">Скачать выбранные ({selected.size})</span>
        </button>
        {selected.size > 0 && <button type="button" className="text-btn" onClick={() => {
          setSelected(new Map());
          setExportMessage("");
        }}>Снять выбор</button>}
        {exporting ? <span role="status">Готовим CSV…</span> : exportMessage && <span role="status">{exportMessage}</span>}
      </div>}
      {columns && columns.length > 0 && (
        <form className="result-actions mt-3" role="search" aria-label="Фильтр таблицы" onSubmit={(event) => {
          event.preventDefault();
          const filter = filterText.trim();
          updateView(0, { ...view.query, filter, filter_column: filter ? filterColumn : undefined });
        }}>
          <label className="sr-only" htmlFor="resultFilter">Поиск по таблице</label>
          <input id="resultFilter" className="dash-select" value={filterText} maxLength={200}
            placeholder="Поиск по таблице" onChange={(event) => setFilterText(event.target.value)} />
          <label className="sr-only" htmlFor="resultFilterColumn">Столбец для поиска</label>
          <select id="resultFilterColumn" className="dash-select" value={filterColumn} onChange={(event) => setFilterColumn(event.target.value)}>
            <option value="">Все столбцы</option>
            {columns.map((column) => <option value={column} key={column}>{columnName(column)}</option>)}
          </select>
          <button type="submit" className="btn btn-sm btn-ghost">Найти</button>
          {view.query.filter && <button type="button" className="text-btn" onClick={() => {
            setFilterText("");
            setFilterColumn("");
            updateView(0, { sort: view.query.sort, direction: view.query.direction });
          }}>Сбросить поиск</button>}
        </form>
      )}
      {(current?.notice || view.notice) && <p className="mt-3 text-sm text-[#8B342C]" role="status">{current?.notice || view.notice}</p>}
      {current?.rowId && <p className="mt-3 text-sm" role="status">Строка из ссылки выделена.</p>}
      {shareMessage && <p className="mt-3 text-sm" role="status">{shareMessage}</p>}
      {manualLink && <div className="result-actions mt-2">
        <label className="sr-only" htmlFor="manualRowLink">Ссылка на строку</label>
        <input id="manualRowLink" className="dash-select result-link-field" ref={manualField} value={manualLink} readOnly />
        <button type="button" className="text-btn" onClick={() => {
          setManualLink("");
          setShareMessage("");
        }}>Закрыть</button>
      </div>}
      {current?.error && <p className="mt-4 text-sm text-[#8B342C]" role="status">{current.error}</p>}
      {!current && <p className="mt-4 text-sm text-[var(--navy-600)]" role="status">Загружаем таблицу…</p>}
      {page && columns?.length === 0 && (
        <p className="mt-4 text-sm text-[var(--navy-600)]">
          {page.total ? "В таблице нет заполненных ячеек." : "В этой таблице нет строк."}
        </p>
      )}
      {page && !page.total && view.query.filter && <p className="mt-4 text-sm" role="status">По запросу ничего не найдено.</p>}
      {page && columns && columns.length > 0 && (
        <>
          <div className="result-table-area mt-4" tabIndex={0} aria-label="Просмотр таблицы">
            <table className="result-table">
              <thead><tr>
                {selectedUrl && <th className="result-table-select" scope="col">
                  <input type="checkbox" ref={pageCheckbox} aria-label="Выбрать строки на этой странице" checked={allSelected}
                    disabled={!ids.length} onChange={(event) => markRows(ids, event.target.checked)} />
                </th>}
                {columns.map((column) => {
                  const order = view.query.sort === column ? view.query.direction : undefined;
                  return <th key={column} scope="col" aria-sort={order === "desc" ? "descending" : order ? "ascending" : undefined}>
                    <button type="button" className="result-sort" onClick={() => sortBy(column)} title="Сортировать">
                      {columnName(column)} <span aria-hidden="true">{order === "asc" ? "↑" : order === "desc" ? "↓" : "↕"}</span>
                    </button>
                  </th>;
                })}
                <th scope="col">Ссылка</th>
              </tr></thead>
              <tbody>
                {page.rows.map((row, index) => {
                  const url = sourcePage(row);
                  const rowId = ids[index];
                  const active = Boolean(rowId && current?.rowId === rowId);
                  const rowNumber = page.offset + index + 1;
                  return (
                    <tr key={rowId ?? rowNumber} className={active ? "result-linked-row" : undefined}
                      ref={active ? linkedRow : undefined} tabIndex={active ? -1 : undefined}>
                      {selectedUrl && <td className="result-table-select">
                        <input type="checkbox" aria-label={`Выбрать строку ${rowNumber}`} disabled={!rowId}
                          checked={Boolean(rowId && selected.has(selectionKey({ table: table.name, row_id: rowId })))}
                          onChange={(event) => markRows([rowId], event.target.checked)} />
                      </td>}
                      {columns.map((column) => (
                        <td key={column}>
                          {column === "source_page" && url
                            ? <a className="text-btn" href={url} target="_blank" rel="noopener" aria-label={`Страница ${row[column]} в PDF`}>{row[column]}</a>
                            : row[column] || "—"}
                        </td>
                      ))}
                      <td><button type="button" className="text-btn result-share-button" disabled={!rowId}
                        aria-label={`Скопировать ссылку на строку ${rowNumber}`} onClick={() => void share(rowId)}>Скопировать ссылку</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {page.total > 0 && <div className="flex flex-wrap items-center justify-between gap-3 mt-3 text-sm text-[var(--navy-600)]">
            <span aria-live="polite">{page.offset + 1}–{page.offset + page.rows.length} из {page.total} строк</span>
            <div className="flex gap-2">
              <button type="button" className="btn btn-sm btn-ghost" disabled={page.offset === 0}
                onClick={() => updateView(Math.max(0, page.offset - pageSize))}>Назад</button>
              <button type="button" className="btn btn-sm btn-ghost" disabled={page.offset + pageSize >= page.total}
                onClick={() => updateView(page.offset + pageSize)}>Далее</button>
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
  const comparison = result.reference_comparison;
  const comparisonFile = result.files.find((file) => file.name === comparison?.file);
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

      {comparison && (
        <section className="dash-card" aria-labelledby="referenceHeading">
          <h2 className="text-base font-semibold" id="referenceHeading">Сверка с проверенной выгрузкой</h2>
          <p className="mt-2 text-sm break-words">{comparison.matched_files.join(", ")}</p>
          {Object.keys(comparison.tables).length === 0 && <p className="mt-2 text-sm">Одноимённых таблиц для сверки нет.</p>}
          <ul className="list-disc pl-5 mt-2 space-y-1 text-sm">
            {Object.entries(comparison.tables).map(([name, status]) => (
              <li key={name}>{tableName(name)} — {({
                match: "совпадает",
                different: "есть расхождения",
                empty_current: "текущая таблица пуста для этих статей",
                empty_reference: "проверенная таблица пуста для этих статей",
                schema_mismatch: "недостаточно общих полей",
              })[status]}</li>
            ))}
          </ul>
          {comparisonFile && <a className="btn btn-sm mt-3" href={comparisonFile.url}>Скачать отчёт сверки</a>}
        </section>
      )}

      {result.warnings.length > 0 && (
        <section className="dash-card" aria-labelledby="warningsHeading">
          <h2 className="text-base font-semibold" id="warningsHeading">Замечания модели</h2>
          <ul className="list-disc pl-5 mt-2 space-y-1 text-sm text-[var(--navy-600)]">
            {result.warnings.map((warning, i) => <li key={i}>{warning}</li>)}
          </ul>
        </section>
      )}

      {tables.length > 0 && <ResultTable key={job.job_id} tables={tables} sources={result.sources}
        selectedUrl={result.selected_url} dashboardUrl={result.dashboard_url} />}
      {tables.length === 0 && new URLSearchParams(window.location.search).has("row") && (
        <p className="dash-card text-sm" role="status">Строка по ссылке недоступна.</p>
      )}
      {result.sources.length > 0 && <FindingsMap key={job.job_id} jobId={job.job_id} sources={result.sources} />}
    </div>
  );
}
