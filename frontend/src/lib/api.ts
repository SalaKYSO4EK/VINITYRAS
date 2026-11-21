export type JobStatus = "queued" | "running" | "completed" | "failed";

export interface Job {
  job_id: string;
  status: JobStatus;
  source_files: string[];
  created_at: string;
  started_at?: string;
  finished_at?: string;
  error?: { code: string; message: string };
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
  archive_files?: string[];
  download_url: string | null;
}

export type CriteriaInput = string | { pdf: File; pages: string };

export interface TablePage {
  columns: string[];
  rows: Record<string, string>[];
  offset: number;
  total: number;
}

export const JOB_ID = /^[a-f0-9]{32}$/;

async function requestJson<T>(url: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Связь с локальным сервером прервалась.");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error?.message || "Не удалось выполнить запрос.");
  if (data === null) throw new Error("Сервер вернул неожиданный ответ.");
  return data as T;
}

export function getHealth(signal?: AbortSignal) {
  return requestJson<{ runner_configured: boolean }>("/api/health", { signal });
}

export function submitJob(files: File[], criteria: CriteriaInput) {
  const form = new FormData();
  if (typeof criteria === "string") {
    form.append("criteria", criteria);
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

export function getTable(file: CsvArtifact, offset: number, limit: number, signal: AbortSignal) {
  return requestJson<TablePage>(`${file.table_url}?offset=${offset}&limit=${limit}`, { signal });
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
