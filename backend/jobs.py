"""Persist jobs and call a local extraction runner one job at a time."""

import csv
import json
import logging
import math
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from uploads import MIB, InvalidUpload, stage_pdfs, valid_filename
from result_cache import ResultCache
import reference_check


JOB_ID = re.compile(r"[0-9a-f]{32}")
MAX_WAITING = 8
MAX_OUTPUT = 64 * MIB
QWEN_API_EXIT_CODE = 3
PDF_INPUT_EXIT_CODE = 4
PDF_UNREADABLE_REASON = "Не удалось прочитать PDF. Проверьте, что он открывается и не защищён паролем."
QWEN_API_MARKER = "QWEN_API_ERROR: "
QWEN_API_DEFAULT_MESSAGE = "Запрос к Qwen API завершился ошибкой. Повторите задание или проверьте настройки API."
WORKER_PHASES = {
    "read_criteria_pdf", "read_pdfs", "criteria", "criteria_default", "extract_pdf",
    "extract_titles", "extract_devices", "reuse_devices", "extract_custom", "recheck", "export",
}
PROGRESS_LABELS = {
    "queued": "Задание в очереди",
    "starting": "Запускаем обработку",
    "read_criteria_pdf": "Читаем PDF с критериями",
    "read_pdfs": "Проверяем и читаем PDF",
    "criteria": "Qwen разбирает критерии",
    "criteria_default": "Готовим стандартную схему",
    "extract_pdf": "Извлекаем данные из PDF",
    "extract_titles": "Проверяем названия статей",
    "extract_devices": "Извлекаем свойства образцов",
    "reuse_devices": "Читаем ранее проверенные свойства образцов",
    "extract_custom": "Извлекаем данные по вашим критериям",
    "recheck": "Обе модели повторно проверяют расхождения по PDF",
    "export": "Собираем и проверяем таблицы",
    "publish": "Готовим файлы к скачиванию",
    "cache_hit": "Используем сохранённый результат",
    "completed": "Готово",
    "failed": "Ошибка обработки",
}
SOLAR_TEMPLATE_CSV = {
    "publications.csv", "materials.csv", "devices.csv", "layers.csv",
    "measurements.csv", "results.csv", "issues.csv",
}
SOLAR_SUBMISSION_FORMAT = "solar-v1"
SOLAR_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "model_bundle" / "submission_template"


class JobFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def current_progress(directory, state):
    base = state.get("progress")
    if not isinstance(base, dict):
        return None
    progress = dict(base, phase_seconds=dict(base["phase_seconds"]))
    if progress["phase"] != "starting":
        return progress
    path = directory / "progress.json"
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 8192:
            return progress
        worker = read_json(path)
        worker_start = worker["worker_started_at"]
        phase_start = worker["phase_started_at"]
        if (worker["phase"] not in WORKER_PHASES or not math.isfinite(worker_start)
                or not math.isfinite(phase_start) or not progress["phase_started_at"] <= worker_start <= phase_start):
            return progress
        timings = {phase: worker["phase_seconds"][phase] for phase in WORKER_PHASES
                   if phase in worker["phase_seconds"] and math.isfinite(worker["phase_seconds"][phase])
                   and worker["phase_seconds"][phase] >= 0}
        progress["phase_seconds"]["starting"] = round(worker_start - progress["phase_started_at"], 3)
        progress["phase_seconds"].update(timings)
        progress.update(phase=worker["phase"], phase_started_at=phase_start)
        for key in ("pdf_index", "pdf_total", "file", "qwen_requests", "qwen_request_bytes"):
            if key in worker:
                progress[key] = worker[key]
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        pass
    return progress


def public_status(directory, state):
    progress = current_progress(directory, state)
    if progress is None:
        return state
    progress["label"] = PROGRESS_LABELS.get(progress["phase"], "Обрабатываем задание")
    if state["status"] in ("queued", "running"):
        progress["elapsed_seconds"] = int(max(0, time.time() - progress["phase_started_at"]))
    return {**state, "progress": progress}


def publish_upload(staging, directory):
    for delay in (0.05, 0.1, 0.2, 0.4, None):
        try:
            staging.rename(directory)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            # A Windows background scan can hold an uploaded file open after it was closed.
            if delay is None:
                logging.warning("Не удалось сохранить загруженные файлы задания.", exc_info=True)
                raise InvalidUpload("Не удалось сохранить загрузку. Повторите отправку. Если ошибка повторится, "
                                    "проверьте доступ к папке заданий.", status=503) from error
            time.sleep(delay)


def qwen_api_error_message(tail):
    for line in reversed(tail.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(QWEN_API_MARKER):
            message = line.removeprefix(QWEN_API_MARKER).strip()
            if message.isprintable() and len(message) <= 512 and "\ufffd" not in message:
                return message
            break
    return QWEN_API_DEFAULT_MESSAGE


def csv_rows(stream):
    return (row for row in csv.reader(stream, strict=True) if row)


def table_page(path, offset, limit):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv_rows(stream)
        columns = next(rows, [])
        return [dict(zip(columns, row)) for row in islice(rows, offset, offset + limit)]


def filled_csv(path, entry):
    if not entry["row_count"]:
        return False
    if "populated_columns" in entry:
        return bool(entry["populated_columns"])
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv_rows(stream)
        next(rows, None)
        return any(any(value.strip() for value in row) for row in rows)


def visible_csv(output, result):
    requested = result.get("requested_tables")
    if requested is None:
        # Previously generated jobs retain their original files and derive the view from their saved plan.
        plan_file = output / "criteria-plan.json"
        if plan_file.is_file() and plan_file.stat().st_size <= MIB:
            plan = read_json(plan_file)
            left, right = plan.get("left", {}), plan.get("right", {})
            if left or right:
                requested = left.keys() & right.keys() & plan.get("tables", {}).keys()
    return [item["name"] for item in result["files"]
            if item["format"] == "csv"
            and (requested is None or Path(item["name"]).stem.casefold() in requested)
            and (item["name"] != "publications.csv" or set(item.get("populated_columns", ())) != {"publication_id"})
            and filled_csv(output / item["name"], item)]


def archive_files(output, result):
    if "archive_files" in result:
        return result["archive_files"]
    entries = result["files"]
    csv_entries = [item for item in entries if item["format"] == "csv"]
    if result.get("download_populated_only"):
        return visible_csv(output, result)
    if result.get("submission_format") == SOLAR_SUBMISSION_FORMAT:
        populated = {item["name"] for item in csv_entries if filled_csv(output / item["name"], item)}
        if not populated:
            return []
        return [item["name"] for item in csv_entries if item["name"] in populated | SOLAR_TEMPLATE_CSV]
    if result.get("profile") == "dual-qwen":
        return [item["name"] for item in csv_entries if filled_csv(output / item["name"],item)]
    csv_names = {item["name"] for item in csv_entries}
    if SOLAR_TEMPLATE_CSV <= csv_names:
        required = csv_names
    else:
        required = {"Efficiency.csv"}
    tables = [
        item["name"] for item in csv_entries
        if item["name"] in required or filled_csv(output / item["name"], item)
    ]
    if tables:
        tables.extend(item["name"] for item in entries
                      if item["name"] == "records.json" or (item["name"] == "report.json" and result["warnings"]))
    return tables


def write_archive(output, names, destination):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for name in names:
            archive.write(output / name, arcname=name)


def inspect_output(output):
    files = []
    total_bytes = 0
    seen = set()
    for path in sorted(output.iterdir()):
        if path.suffix.lower() != ".csv":
            continue
        if not valid_filename(path.name) or path.is_symlink() or not path.is_file():
            raise ValueError(f"Недопустимый CSV: {path.name}")
        if path.name.casefold() in seen:
            raise ValueError(f"Повторяющееся имя CSV: {path.name}")
        seen.add(path.name.casefold())
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_OUTPUT:
            raise ValueError("CSV превышают лимит выгрузки в 64 МиБ")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = csv_rows(stream)
            columns = next(rows, [])
            if not columns or not all(col.strip() for col in columns) or len(set(columns)) != len(columns):
                raise ValueError(f"Пустые или повторяющиеся заголовки в {path.name}")
            count = 0
            empty_columns = set(range(len(columns)))
            for row in rows:
                if len(row) != len(columns):
                    raise ValueError(f"Число полей не совпадает с заголовком в {path.name}")
                if empty_columns:
                    empty_columns = {index for index in empty_columns if not row[index].strip()}
                count += 1
        files.append({"name": path.name, "format": "csv", "size_bytes": size, "columns": columns, "row_count": count,
                      "populated_columns": [name for index, name in enumerate(columns) if index not in empty_columns]})
    warnings = []
    file_outcomes = []
    allow_empty = False
    solar_submission = False
    download_populated_only = False
    requested_tables = None
    report = output / "report.json"
    if report.exists() or report.is_symlink():
        if report.is_symlink() or not report.is_file() or report.stat().st_size > MIB:
            raise ValueError("report.json должен быть файлом размером до 1 МиБ")
        document = read_json(report)
        if not isinstance(document, dict):
            raise ValueError("report.json должен содержать объект JSON")
        allow_empty = document.get("profile") == "dual-qwen"
        solar_submission = document.get("submission_format") == SOLAR_SUBMISSION_FORMAT
        download_populated_only = document.get("download_populated_only") is True
        requested_tables = document.get("requested_tables")
        if requested_tables is not None and (
            not isinstance(requested_tables, list) or not all(isinstance(name, str) for name in requested_tables)
        ):
            raise ValueError("В report.json поле requested_tables должно быть массивом строк")
        warnings = document.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise ValueError("В report.json поле warnings должно быть массивом строк")
        file_outcomes = document.get("file_outcomes", [])
        if not isinstance(file_outcomes, list) or any(
            not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("file", "status", "reason"))
            or not valid_filename(item["file"])
            for item in file_outcomes
        ):
            raise ValueError("В report.json неверный формат статусов PDF")
        files.append({"name": report.name, "format": "json", "size_bytes": report.stat().st_size})
    if solar_submission:
        output_columns = {entry["name"]: entry["columns"] for entry in files if entry["format"] == "csv"}
        for filename in sorted(SOLAR_TEMPLATE_CSV):
            if filename not in output_columns:
                raise ValueError(f"В солнечной выгрузке отсутствует {filename}")
            with (SOLAR_TEMPLATE_DIR / filename).open(encoding="utf-8-sig", newline="") as stream:
                expected = next(csv_rows(stream), [])
            if output_columns[filename] != expected:
                raise ValueError(f"Заголовки {filename} отличаются от солнечного шаблона")
    if not allow_empty and not any(f["format"] == "csv" for f in files):
        raise ValueError("Модель не создала CSV в каталоге вывода")

    evidence = output / "records.json"
    if evidence.exists() or evidence.is_symlink():
        try:
            if evidence.is_symlink() or not evidence.is_file():
                raise ValueError
            size = evidence.stat().st_size
            if not 0 < size <= MAX_OUTPUT or not isinstance(read_json(evidence), dict):
                raise ValueError
        except (OSError, ValueError):
            warnings.append("Файл с привязками к источникам недоступен или превышает 64 МиБ.")
        else:
            files.append({"name": evidence.name, "format": "json", "size_bytes": size})
    result = {"files": files, "warnings": warnings, "file_outcomes": file_outcomes}
    if allow_empty:
        result["profile"] = "dual-qwen"
    if solar_submission:
        result["submission_format"] = SOLAR_SUBMISSION_FORMAT
    if download_populated_only:
        result["download_populated_only"] = True
    if requested_tables is not None:
        result["requested_tables"] = requested_tables
    return result


def stop_runner(process):
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            logging.warning("Не удалось остановить дерево процессов модели, PID %s", process.pid)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


class Jobs:
    def __init__(self, root, runner, model_python, timeout):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner.resolve()
        self.model_python = model_python.resolve()
        self.cache = ResultCache(self.root, self.runner, self.model_python)
        self.timeout = timeout
        self.pending = queue.Queue(maxsize=MAX_WAITING)
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._work, name="model-worker", daemon=True)

    @property
    def configured(self):
        return self.runner.is_file() and self.model_python.is_file()

    def start(self):
        for directory in self.root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue
            try:
                state = self.status(directory.name)
            except (OSError, ValueError):
                logging.exception("Не удалось прочитать состояние задания %s", directory.name)
                continue
            if state and state["status"] in ("queued", "running"):
                self._fail(directory.name, JobFailure("interrupted", "Сервер перезапустился. Отправьте задание снова."))
        self.worker.start()

    def close(self):
        self.stopping.set()
        self.worker.join()

    def enqueue(self, criteria, uploads, criteria_pdf=None, criteria_pages=None, *,
                upload_bytes=None, upload_seconds=None):
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        with tempfile.TemporaryDirectory(dir=self.root, prefix="upload-") as temporary:
            staging = Path(temporary)
            input_dir = staging / "input"
            input_dir.mkdir()
            stage_start = time.perf_counter()
            upload_issues = []
            names = stage_pdfs(uploads, input_dir, file_issues=upload_issues)
            staged_seconds = round(time.perf_counter() - stage_start, 3)
            request = {
                "schema_version": 1,
                "job_id": job_id,
                "criteria": criteria,
                "files": [{"name": name, "path": str(directory / "input" / name)} for name in names],
            }
            if criteria_pdf:
                name, contents = criteria_pdf
                (staging / "criteria.pdf").write_bytes(contents)
                request["criteria"] = "Извлеки данные по критериям из приложенного PDF."
                request["criteria_pdf"] = {"name": name, "path": str(directory / "criteria.pdf"), "pages": criteria_pages}
            write_json(staging / "request.json", request)
            timings = {"stage_uploads": staged_seconds}
            if upload_seconds is not None:
                timings["receive_upload"] = round(upload_seconds, 3)
            progress = {"phase": "queued", "phase_started_at": time.time(), "phase_seconds": timings,
                        "uploaded_pdf_count": len(names),
                        "pdf_bytes": sum((input_dir / name).stat().st_size for name in names)}
            if upload_bytes is not None:
                progress["upload_bytes"] = upload_bytes
            state = {"job_id": job_id, "status": "queued", "created_at": now(), "source_files": names,
                     "progress": progress, "upload_issues": upload_issues}
            write_json(staging / "status.json", state)
            with self.lock:
                if self.pending.full():
                    raise queue.Full
                publish_upload(staging, directory)
                self.pending.put_nowait(job_id)
        return state

    def status(self, job_id):
        if not JOB_ID.fullmatch(job_id) or (self.root / job_id).is_symlink():
            return None
        with self.lock:
            try:
                return read_json(self.root / job_id / "status.json")
            except FileNotFoundError:
                return None

    def _update(self, job_id, **fields):
        path = self.root / job_id / "status.json"
        with self.lock:
            state = read_json(path)
            state.update(fields)
            temporary = path.with_suffix(".tmp")
            write_json(temporary, state)
            temporary.replace(path)

    def _fail(self, job_id, error):
        try:
            fields = {"error": {"code": error.code, "message": str(error)}}
            if error.code == "invalid_pdf":
                state = self.status(job_id) or {}
                fields["file_outcomes"] = [
                    {"file": name, "status": "unreadable", "reason": PDF_UNREADABLE_REASON}
                    for name in state.get("source_files", [])
                ]
            self._progress(self.root / job_id, "failed", status="failed", finished_at=now(), **fields)
        except (OSError, ValueError):
            logging.exception("Не удалось записать состояние задания %s", job_id)

    def _progress(self, directory, phase, **fields):
        state = self.status(directory.name) or {}
        current = time.time()
        progress = current_progress(directory, state) or {"phase_seconds": {}}
        previous = progress.get("phase")
        if previous and previous != phase:
            timings = progress["phase_seconds"]
            timings[previous] = round(timings.get(previous, 0) +
                                      max(0, current - progress["phase_started_at"]), 3)
        if phase == "failed" and previous:
            progress["failed_phase"] = previous
        if phase == "cache_hit":
            progress.update(qwen_requests=0, qwen_request_bytes=0)
        progress.update(phase=phase, phase_started_at=current)
        for key in ("pdf_index", "pdf_total", "file"):
            progress.pop(key, None)
        self._update(directory.name, progress=progress, **fields)

    def _work(self):
        while not self.stopping.is_set():
            try:
                job_id = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                directory = self.root / job_id
                self._progress(directory, "starting", status="running", started_at=now())
                output = directory / "output"
                engine, key = self.cache.keys(directory)
                reused = self.cache.restore(key, output)
                if reused:
                    self._progress(directory, "cache_hit", reused=True)
                    (directory / "runner.log").write_text(
                        "Использована сохранённая выгрузка для этих PDF, критериев и версии моделей.\n",
                        encoding="utf-8")
                else:
                    output.mkdir()
                    criteria_cache = self.cache.index / "criteria" / engine if engine else None
                    self._invoke(directory, output, criteria_cache=criteria_cache,
                                 device_cache=self.cache.device_store())
                self._progress(directory, "publish")
                self._publish(directory, output)
                self._progress(directory, "completed", status="completed", finished_at=now())
                if not reused:
                    self.cache.remember(key, directory)
            except JobFailure as exc:
                self._fail(job_id, exc)
            except Exception:
                logging.exception("Не удалось обработать задание %s", job_id)
                self._fail(job_id, JobFailure("internal_error", "Не удалось сохранить результаты задания."))
            finally:
                self.pending.task_done()

    def _invoke(self, directory, output, *, criteria_cache=None, device_cache=None):
        entry = [str(self.runner)]
        if (directory / "criteria.pdf").is_file():
            entry = [str(Path(__file__).with_name("pdf_criteria_runner.py")), "--runner", str(self.runner)]
        command = [str(self.model_python), "-X", "utf8", "-u", *entry, "--request", str(directory / "request.json"),
                   "--out", str(output)]
        env = dict(os.environ, NEXUS_JOB_PROGRESS=str(directory / "progress.json"))
        for key, store in (("NEXUS_CRITERIA_CACHE", criteria_cache), ("NEXUS_DEVICE_CACHE", device_cache)):
            if store is None:
                env.pop(key, None)
            else:
                env[key] = str(store)
        with (directory / "runner.log").open("w+b") as log:
            try:
                process = subprocess.Popen(command, cwd=self.runner.parent, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=os.name != "nt",
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as exc:
                logging.exception("Не удалось запустить модель для %s", directory.name)
                if getattr(exc, "winerror", None) == 4551:
                    raise JobFailure("runner_blocked", "Политика Windows заблокировала запуск Python для модели.") from exc
                raise JobFailure("runner_unavailable", "Не удалось запустить пакет модели.") from exc
            deadline = time.monotonic() + self.timeout
            try:
                while process.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise JobFailure("timeout", "Модель превысила лимит времени обработки.")
                    if self.stopping.wait(min(remaining, 0.25)):
                        raise JobFailure("interrupted", "Обработка прервана остановкой сервера.")
            finally:
                if process.poll() is None:
                    stop_runner(process)
            if process.returncode:
                end = log.seek(0, os.SEEK_END)
                log.seek(max(0, end - 8192))
                tail = log.read()
                if process.returncode == QWEN_API_EXIT_CODE:
                    raise JobFailure("qwen_api_error", qwen_api_error_message(tail))
                if process.returncode == PDF_INPUT_EXIT_CODE:
                    raise JobFailure("invalid_pdf", "Не удалось прочитать загруженные PDF. Проверьте, что файлы открываются, и загрузите их снова.")
                criteria_error = directory / "criteria-error.txt"
                if criteria_error.is_file():
                    raise JobFailure("criteria_pdf_error", criteria_error.read_text(encoding="utf-8"))
                if b"OSError: [WinError 4551]" in tail:
                    raise JobFailure("runner_blocked",
                                     "Политика Windows заблокировала компонент модели (WinError 4551). "
                                     "Передайте сообщение администратору компьютера.")
                raise JobFailure("runner_failed", f"Пакет модели завершился с кодом {process.returncode}.")

    def _publish(self, directory, output):
        try:
            result = inspect_output(output)
            if result.get("submission_format") == SOLAR_SUBMISSION_FORMAT:
                expected = {path.stem for path in (directory / "input").iterdir() if
                            path.is_file() and path.suffix.lower() == ".pdf"}
                with (output / "publications.csv").open(encoding="utf-8-sig", newline="") as stream:
                    publication_ids = [row["publication_id"] for row in csv.DictReader(stream)]
                if len(publication_ids) != len(expected) or set(publication_ids) != expected:
                    raise ValueError("В publications.csv требуется одна строка на каждый входной PDF")
        except (OSError, ValueError, csv.Error) as exc:
            with (directory / "runner.log").open("a", encoding="utf-8") as log:
                print(f"\nОшибка контракта выгрузки: {exc}", file=log)
            raise JobFailure("invalid_result", "Модель вернула выгрузку неверного формата.") from exc
        try:
            comparison = reference_check.compare(directory / "input", output)
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
            logging.exception("Не завершена сверка задания %s", directory.name)
            raise JobFailure("reference_check_failed",
                             "Не удалось сверить CSV с проверенной выгрузкой. Повторите задание после проверки эталона.") from exc
        names = archive_files(output, result)
        if comparison is not None:
            path = output / reference_check.REPORT_FILE
            with tempfile.NamedTemporaryFile(dir=output, suffix=".tmp", delete=False) as pending:
                temporary = Path(pending.name)
            try:
                write_json(temporary, comparison)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            result["files"].append({"name": path.name, "format": "json", "size_bytes": path.stat().st_size})
            result["reference_comparison"] = {
                "file": path.name, "matched_files": comparison["matched_files"],
                "tables": {name: details["status"] for name, details in comparison["tables"].items()},
            }
            if names and not result.get("download_populated_only"):
                names.append(path.name)
        if names:
            write_archive(output, names, directory / "submission.zip")
        result["archive_files"] = names
        write_json(directory / "result.json", result)
