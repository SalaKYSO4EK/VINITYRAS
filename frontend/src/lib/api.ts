export type JobStatus = "queued" | "running" | "completed" | "failed";

export interface FileIssue {
  file: string;
  reason: string;
  archive?: string;
  status?: "found" | "no_findings" | "processing_limited" | "unreadable";
}

export interface JobProgress {
  phase: string;
  phase_seconds?: Record<string, number>;
  label: string;
  elapsed_seconds?: number;
  file?: string;
  pdf_index?: number;
  pdf_total?: number;
  uploaded_pdf_count?: number;
}

export function reuseNotice(reused?: boolean, progress?: JobProgress) {
  if (reused) return "Использован сохранённый результат.";
  if (progress?.phase_seconds?.reuse_devices !== undefined) return "Свойства образцов взяты из предыдущего анализа тех же PDF.";
  return "";
}

export interface Job {
  job_id: string;
  status: JobStatus;
  source_files: string[];
  created_at: string;
  started_at?: string;
  finished_at?: string;
  error?: { code: string; message: string };
  upload_issues?: FileIssue[];
  file_outcomes?: FileIssue[];
  progress?: JobProgress;
  reused?: boolean;
}

export function progressDetails(progress?: JobProgress) {
  if (!progress) return "";
  const parts: string[] = [];
  if (progress.pdf_index && progress.pdf_total && progress.file) {
    parts.push(`PDF ${progress.pdf_index} из ${progress.pdf_total} · ${progress.file}`);
  } else if (progress.phase === "queued" && progress.uploaded_pdf_count) {
    parts.push(`PDF в задании — ${progress.uploaded_pdf_count}`);
  }
  if (typeof progress.elapsed_seconds === "number" && progress.elapsed_seconds >= 10) {
    parts.push(`Этап длится ${progress.elapsed_seconds} с`);
  }
  return parts.join(" · ");
}

export interface CsvArtifact {
  name: string;
  format: "csv";
  row_count: number;
  populated_columns?: string[];
  url: string;
  table_url: string;
}

export interface Result {
  files: (CsvArtifact | { name: string; format: "json"; url: string })[];
  sources: { name: string; publication_id: string; url: string }[];
  warnings: string[];
  file_outcomes?: FileIssue[];
  archive_files?: string[];
  reference_comparison?: {
    file: string;
    matched_files: string[];
    tables: Record<string, "match" | "different" | "empty_current" | "empty_reference" | "schema_mismatch">;
  };
  download_url: string | null;
  selected_url?: string;
  dashboard_url?: string;
}

export type CriteriaInput = string | { pdf: File; pages: string };

export interface TablePage {
  columns: string[];
  rows: Record<string, string>[];
  row_ids: string[];
  offset: number;
  total: number;
}

export interface TableQuery {
  sort?: string;
  direction?: "asc" | "desc";
  filter?: string;
  filter_column?: string;
}

export interface SelectedRow {
  table: string;
  row_id: string;
}

export const JOB_ID = /^[a-f0-9]{32}$/;

export class RequestError extends Error {
  constructor(message: string, public readonly status: number, public readonly fileIssues: FileIssue[] = []) {
    super(message);
  }
}

async function request(url: string, options?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Связь с локальным сервером прервалась.");
  }
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    if (response.status === 401 || response.status === 403) {
      const detail = typeof error?.error?.message === "string" ? " " + error.error.message : "";
      throw new RequestError("Доступ к заданию закрыт." + detail, response.status);
    }
    throw new RequestError(error?.error?.message || "Не удалось выполнить запрос.", response.status,
      Array.isArray(error?.error?.file_issues) ? error.error.file_issues : []);
  }
  return response;
}

async function requestJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await request(url, options);
  const data = await response.json().catch(() => null);
  if (data === null) throw new Error("Сервер вернул неожиданный ответ.");
  return data as T;
}

export function getHealth(signal?: AbortSignal) {
  return requestJson<{ runner_configured: boolean }>("/api/health", { signal });
}

export function submitJob(files: File[], criteria: CriteriaInput) {
  const form = new FormData();
  if (typeof criteria === "string") {
    if (criteria) form.append("criteria", criteria);
  } else {
    form.append("criteria_pdf", criteria.pdf);
    if (criteria.pages) form.append("criteria_pages", criteria.pages);
  }
  for (const file of files) form.append("files", file);
  return requestJson<Job>("/api/jobs", { method: "POST", body: form });
}

export function getJob(id: string, signal: AbortSignal) {
  return requestJson<Job>(`/api/jobs/${id}`, { signal });
}

export function getResult(id: string, signal: AbortSignal) {
  return requestJson<Result>(`/api/jobs/${id}/result`, { signal });
}

export function getTable(file: CsvArtifact, offset: number, limit: number, signal: AbortSignal, options: TableQuery = {}) {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  for (const [key, value] of Object.entries(options)) {
    if (value) params.set(key, value);
  }
  return requestJson<TablePage>(`${file.table_url}?${params}`, { signal });
}

export function locateTableRow(file: CsvArtifact, rowId: string, limit: number, signal: AbortSignal) {
  const params = new URLSearchParams({ row_id: rowId, limit: String(limit) });
  return requestJson<{ row_id: string; offset: number }>(`${file.table_url}/locate?${params}`, { signal });
}

export async function getSelectedCsv(url: string, rows: SelectedRow[], signal: AbortSignal) {
  const response = await request(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rows }), signal,
  });
  return response.blob();
}

export async function populatedColumns(file: CsvArtifact, page: TablePage, signal: AbortSignal) {
  if (file.populated_columns) return file.populated_columns;

  // Results saved before column metadata was added still use the paged table API.
  const empty = new Set(page.columns);
  function checkRows(rows: TablePage["rows"]) {
    for (const row of rows) {
      for (const column of empty) {
        if (row[column]?.trim()) empty.delete(column);
      }
      if (!empty.size) break;
    }
  }

  checkRows(page.rows);
  const batchSize = 200;
  const start = page.offset === 0 ? page.rows.length : 0;
  for (let offset = start; offset < page.total && empty.size; offset += batchSize) {
    const batch = await getTable(file, offset, batchSize, signal);
    checkRows(batch.rows);
  }
  return page.columns.filter((column) => !empty.has(column));
}

export function csvArtifacts(result: Result) {
  return result.files.filter((file): file is CsvArtifact => file.format === "csv");
}

export const statusLabel: Record<JobStatus, string> = {
  queued: "В очереди",
  running: "Обрабатывается",
  completed: "Готово",
  failed: "Ошибка",
};
