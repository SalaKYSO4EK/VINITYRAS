"use strict";

const $ = (id) => document.getElementById(id);
const fileInput = $("files");
const dropArea = $("drop-area");
const criteria = $("criteria");
const formMessage = $("form-message");
const tableSelect = $("table-select");
const pageSize = 25;
const uploadLimit = 128 * 1024 * 1024;
const files = [];
let modelReady = false;
let busy = false;
let result;
let offset = 0;
let tableRequest = 0;

const tableNames = {
  "publications.csv": "Публикации",
  "materials.csv": "Материалы",
  "devices.csv": "Образцы",
  "layers.csv": "Слои",
  "measurements.csv": "Измерения",
  "results.csv": "Результаты измерений",
  "Efficiency.csv": "КПД по публикациям",
  "issues.csv": "Замечания проверки",
};

const columnNames = {
  publication_id: "Публикация",
  metric_name: "Показатель",
  metric_value: "Значение",
  metric_unit: "Единица",
  result_id: "Код результата",
  raw_value: "Исходное значение",
  raw_unit: "Исходная единица",
  aggregation: "Агрегация",
  spread_value: "Разброс",
  spectrum: "Спектр освещения",
  light_intensity: "Интенсивность освещения",
  intensity_unit: "Единица интенсивности",
  temperature: "Температура измерения",
  efficiency_status: "Данные о КПД",
  device_label: "Образец",
  device_id: "Код образца",
  measurement_id: "Код измерения",
  source_file: "Файл",
  source_page: "Страница PDF",
  source_table: "Таблица в статье",
  source_row: "Строка таблицы",
  source_column: "Столбец таблицы",
  source_bbox: "Координаты ячейки в PDF",
  source_fragment: "Цитата",
  scan_direction: "Направление сканирования",
  title: "Название",
  note: "Примечание",
};

async function getJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new Error("Связь с локальным сервером прервалась.");
  }
  if (!response.headers.get("Content-Type")?.includes("application/json")) {
    throw new Error("Сервер вернул неожиданный ответ.");
  }
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "Не удалось выполнить запрос.");
    error.fileIssues = data.error?.file_issues;
    throw error;
  }
  return data;
}

function showJob(message, state = "") {
  const status = $("job-progress");
  status.dataset.state = state;
  if (status.textContent !== message) status.textContent = message;
}

function setBusy(value) {
  busy = value;
  fileInput.disabled = criteria.disabled = $("example").disabled = busy;
  $("submit").disabled = busy || !modelReady;
  $("submit").textContent = busy ? "Задание выполняется…" : "Обработать статьи";
  for (const button of $("file-list").querySelectorAll("button")) button.disabled = busy;
}

async function checkConnection() {
  const connection = $("connection");
  try {
    const health = await getJson("/api/health");
    modelReady = health.runner_configured;
    connection.textContent = modelReady ? "Пакет модели настроен" : "Пакет модели не подключён";
  } catch {
    modelReady = false;
    connection.textContent = "Сервер недоступен";
  }
  connection.dataset.state = modelReady ? "ready" : "offline";
  $("submit").disabled = busy || !modelReady;
}

function renderFiles() {
  const list = $("file-list");
  list.replaceChildren();
  list.hidden = files.length === 0;
  for (const file of files) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = file.name;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "text-button";
    remove.textContent = "Убрать";
    remove.setAttribute("aria-label", `Убрать ${file.name}`);
    remove.addEventListener("click", () => {
      files.splice(files.indexOf(file), 1);
      renderFiles();
    });
    item.append(name, remove);
    list.append(item);
  }
  $("file-meta").textContent = files.length ? `Выбрано файлов: ${files.length}` : "До 100 PDF · 128 МиБ на загрузку";
}

function addFiles(selection) {
  if (busy) return;
  formMessage.textContent = "";
  let total = files.reduce((size, file) => size + file.size, 0);
  for (const file of selection) {
    if (files.some((existing) => existing.name.toLowerCase() === file.name.toLowerCase())) {
      formMessage.textContent = `Файл «${file.name}» уже выбран.`;
      continue;
    }
    if (files.length >= 100 || total + file.size > uploadLimit) {
      formMessage.textContent = "За один раз можно загрузить до 100 файлов общим размером до 128 МиБ.";
      break;
    }
    files.push(file);
    total += file.size;
  }
  renderFiles();
}

function showWarnings(warnings) {
  const list = $("warnings-list");
  list.replaceChildren();
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = warning;
    list.append(item);
  }
  $("warnings").hidden = warnings.length === 0;
}

function showFileDiagnostics(skipped = [], outcomes = []) {
  const container = $("file-diagnostics");
  container.replaceChildren();
  for (const [title, items] of [["Файлы, пропущенные при загрузке", skipped], ["Результат по файлам", outcomes]]) {
    if (!items.length) continue;
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("ul");
    for (const entry of items) {
      const row = document.createElement("li");
      const name = entry.archive ? entry.archive + " / " + entry.file : entry.file;
      row.textContent = name + " — " + entry.reason;
      list.append(row);
    }
    container.append(heading, list);
  }
  container.hidden = !container.childElementCount;
}

function sourceLink(url, text) {
  const link = document.createElement("a");
  link.href = url;
  link.textContent = text;
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function showSources(sources) {
  const list = $("source-list");
  list.replaceChildren();
  for (const source of sources) {
    const item = document.createElement("li");
    item.append(sourceLink(source.url, source.name));
    list.append(item);
  }
  $("sources").hidden = sources.length === 0;
}

function fillTable(page, name) {
  const priority = name === "results.csv" ? ["metric_name", "metric_value", "metric_unit", "source_page", "source_table"] : [];
  const columns = [...priority.filter((column) => page.columns.includes(column)),
    ...page.columns.filter((column) => !priority.includes(column))];
  const heading = document.createElement("tr");
  for (const column of columns) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = Object.hasOwn(columnNames, column) ? columnNames[column] : column;
    heading.append(cell);
  }
  $("table-head").replaceChildren(heading);

  const sourcesByName = new Map(result.sources.map((source) => [source.name, source.url]));
  const sourcesById = new Map(result.sources.map((source) => [source.publication_id, source.url]));
  const body = document.createDocumentFragment();
  for (const row of page.rows) {
    const tr = document.createElement("tr");
    const source = sourcesByName.get(row.source_file) || sourcesById.get(row.publication_id);
    for (const column of columns) {
      const cell = document.createElement("td");
      const value = row[column];
      if (column === "source_page" && source && /^[1-9]\d*$/.test(value)) {
        cell.append(sourceLink(`${source}#page=${value}`, value));
      } else {
        cell.textContent = value || "—";
      }
      tr.append(cell);
    }
    body.append(tr);
  }
  $("table-body").replaceChildren(body);
}

async function openTable() {
  const request = ++tableRequest;
  const table = result.files.find((file) => file.name === tableSelect.value);
  if (!table) return;
  $("csv-link").href = table.url;
  $("csv-link").hidden = false;
  $("table-message").textContent = "Загружаем таблицу…";
  $("table-scroll").hidden = true;
  $("pagination").hidden = true;
  try {
    const page = await getJson(`${table.table_url}?offset=${offset}&limit=${pageSize}`);
    if (request !== tableRequest) return;
    fillTable(page, table.name);
    $("table-message").textContent = page.total ? "" : "В таблице пока только заголовки.";
    $("table-scroll").hidden = false;
    $("table-count").textContent = page.total ? `${page.offset + 1}–${page.offset + page.rows.length} из ${page.total} строк` : "0 строк";
    $("previous").disabled = page.offset === 0;
    $("next").disabled = page.offset + page.rows.length >= page.total;
    $("pagination").hidden = false;
  } catch (error) {
    if (request === tableRequest) $("table-message").textContent = error.message;
  }
}

async function showResult(statusUrl, job) {
  result = await getJson(`${statusUrl}/result`);
  showFileDiagnostics(job.upload_issues, result.file_outcomes);
  const tables = result.files.filter((file) => file.format === "csv");
  tableSelect.replaceChildren();
  for (const table of tables) {
    const label = tableNames[table.name] || table.name;
    tableSelect.add(new Option(`${label} (${table.row_count})`, table.name));
  }
  const first = tables.find((table) => table.name === "results.csv" && table.row_count > 0)
    || tables.find((table) => table.row_count > 0) || tables[0];
  tableSelect.value = first.name;
  offset = 0;
  showWarnings(result.warnings);
  showSources(result.sources);
  const comparison = result.files.find((file) => file.name === result.reference_comparison?.file);
  const referenceLink = $("reference-link");
  referenceLink.hidden = !comparison;
  if (comparison) referenceLink.href = comparison.url;
  else referenceLink.removeAttribute("href");
  $("dashboard-link").hidden = !result.dashboard_url;
  if (result.dashboard_url) $("dashboard-link").href = result.dashboard_url;
  const zipLink = $("zip-link");
  zipLink.hidden = !result.download_url;
  if (result.download_url) zipLink.href = result.download_url;
  else zipLink.removeAttribute("href");
  $("result-data").hidden = false;
  const total = tables.reduce((sum, table) => sum + table.row_count, 0);
  const summary = `Таблиц — ${tables.length} · строк во всех таблицах — ${total}`;
  let notice = "Готово.";
  if (job.reused) notice = "Использован сохранённый результат.";
  else if (job.progress?.phase_seconds?.reuse_devices !== undefined) notice = "Свойства образцов взяты из предыдущего анализа тех же PDF.";
  showJob(notice + " " + summary);
  await openTable();
}

function progressMessage(job) {
  const progress = job.progress;
  const details = [progress?.label || (job.status === "queued" ? "Задание в очереди…" : "Модель обрабатывает статьи…")];
  if (progress?.pdf_index && progress.pdf_total && progress.file) {
    details.push(`PDF ${progress.pdf_index} из ${progress.pdf_total} · ${progress.file}`);
  } else if (progress?.phase === "queued" && progress.uploaded_pdf_count) {
    details.push(`PDF в задании — ${progress.uploaded_pdf_count}`);
  }
  if (progress?.elapsed_seconds >= 10) details.push(`Этап длится ${progress.elapsed_seconds} с`);
  return details.join(" · ");
}

async function followJob(statusUrl) {
  while (true) {
    const job = await getJson(statusUrl);
    if (job.status === "completed") {
      await showResult(statusUrl, job);
      return;
    }
    showFileDiagnostics(job.upload_issues, job.file_outcomes);
    if (job.status === "failed") throw new Error(job.error?.message || "Не удалось обработать статьи.");
    showJob(progressMessage(job), "running");
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !modelReady) return;
  const task = criteria.value.trim();
  if (!files.length) {
    formMessage.textContent = "Выберите хотя бы один PDF или ZIP.";
    fileInput.focus();
    return;
  }
  formMessage.textContent = "";
  const body = new FormData();
  if (task) body.append("criteria", task);
  for (const file of files) body.append("files", file);
  setBusy(true);
  tableRequest++;
  showFileDiagnostics();
  $("zip-link").hidden = $("result-data").hidden = true;
  showJob("Загружаем и проверяем файлы…", "running");
  try {
    const job = await getJson("/api/jobs", { method: "POST", body });
    history.replaceState(null, "", `/?job=${job.job_id}`);
    await followJob(job.status_url);
  } catch (error) {
    if (error.fileIssues) showFileDiagnostics(error.fileIssues);
    showJob(error.message, "error");
  } finally {
    setBusy(false);
  }
});

fileInput.addEventListener("change", () => {
  addFiles(fileInput.files);
  fileInput.value = "";
});

dropArea.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (!busy) dropArea.setAttribute("data-dragging", "");
});
dropArea.addEventListener("dragleave", () => dropArea.removeAttribute("data-dragging"));
dropArea.addEventListener("drop", (event) => {
  event.preventDefault();
  dropArea.removeAttribute("data-dragging");
  addFiles(event.dataTransfer.files);
});

$("example").addEventListener("click", () => {
  criteria.value = "Извлеки PCE и Voc и укажи источники в PDF";
  criteria.focus();
});

tableSelect.addEventListener("change", () => { offset = 0; openTable(); });
$("previous").addEventListener("click", () => { offset -= pageSize; openTable(); });
$("next").addEventListener("click", () => { offset += pageSize; openTable(); });

checkConnection();
const savedJob = new URLSearchParams(location.search).get("job");
if (/^[0-9a-f]{32}$/.test(savedJob)) {
  setBusy(true);
  followJob(`/api/jobs/${savedJob}`).catch((error) => showJob(error.message, "error"))
    .finally(() => setBusy(false));
}
