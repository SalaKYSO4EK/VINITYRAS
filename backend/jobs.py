"""Persist jobs and call a local extraction runner one job at a time."""

import csv
import json
import logging
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

from uploads import MIB, stage_pdfs, valid_filename


JOB_ID = re.compile(r"[0-9a-f]{32}")
MAX_WAITING = 8
MAX_OUTPUT = 64 * MIB


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


def archive_files(output, result):
    if "archive_files" in result:
        return result["archive_files"]
    entries = result["files"]
    tables = [item["name"] for item in entries
              if item["format"] == "csv" and filled_csv(output / item["name"], item)]
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
    if not files:
        raise ValueError("Модель не создала CSV в каталоге вывода")

    warnings = []
    report = output / "report.json"
    if report.exists() or report.is_symlink():
        if report.is_symlink() or not report.is_file() or report.stat().st_size > MIB:
            raise ValueError("report.json должен быть файлом размером до 1 МиБ")
        document = read_json(report)
        if not isinstance(document, dict):
            raise ValueError("report.json должен содержать объект JSON")
        warnings = document.get("warnings", [])
        if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
            raise ValueError("В report.json поле warnings должно быть массивом строк")
        files.append({"name": report.name, "format": "json", "size_bytes": report.stat().st_size})

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
    return {"files": files, "warnings": warnings}


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

    def enqueue(self, criteria, uploads, criteria_pdf=None, criteria_pages=None):
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        with tempfile.TemporaryDirectory(dir=self.root, prefix="upload-") as temporary:
            staging = Path(temporary)
            input_dir = staging / "input"
            input_dir.mkdir()
            names = stage_pdfs(uploads, input_dir)
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
            state = {"job_id": job_id, "status": "queued", "created_at": now(), "source_files": names}
            write_json(staging / "status.json", state)
            with self.lock:
                if self.pending.full():
                    raise queue.Full
                staging.rename(directory)
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
            self._update(job_id, status="failed", finished_at=now(), error={"code": error.code, "message": str(error)})
        except (OSError, ValueError):
            logging.exception("Не удалось записать состояние задания %s", job_id)

    def _work(self):
        while not self.stopping.is_set():
            try:
                job_id = self.pending.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._update(job_id, status="running", started_at=now())
                directory = self.root / job_id
                output = directory / "output"
                output.mkdir()
                self._invoke(directory, output)
                self._publish(directory, output)
                self._update(job_id, status="completed", finished_at=now())
            except JobFailure as exc:
                self._fail(job_id, exc)
            except Exception:
                logging.exception("Не удалось обработать задание %s", job_id)
                self._fail(job_id, JobFailure("internal_error", "Не удалось сохранить результаты задания."))
            finally:
                self.pending.task_done()

    def _invoke(self, directory, output):
        entry = [str(self.runner)]
        if (directory / "criteria.pdf").is_file():
            entry = [str(Path(__file__).with_name("pdf_criteria_runner.py")), "--runner", str(self.runner)]
        command = [str(self.model_python), "-X", "utf8", "-u", *entry, "--request", str(directory / "request.json"),
                   "--out", str(output)]
        with (directory / "runner.log").open("w+b") as log:
            try:
                process = subprocess.Popen(command, cwd=self.runner.parent, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
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
                criteria_error = directory / "criteria-error.txt"
                if criteria_error.is_file():
                    raise JobFailure("criteria_pdf_error", criteria_error.read_text(encoding="utf-8"))
                end = log.seek(0, os.SEEK_END)
                log.seek(max(0, end - 8192))
                if b"OSError: [WinError 4551]" in log.read():
                    raise JobFailure("runner_blocked",
                                     "Политика Windows заблокировала компонент модели (WinError 4551). "
                                     "Передайте сообщение администратору компьютера.")
                raise JobFailure("runner_failed", f"Пакет модели завершился с кодом {process.returncode}.")

    def _publish(self, directory, output):
        try:
            result = inspect_output(output)
        except (ValueError, csv.Error) as exc:
            with (directory / "runner.log").open("a", encoding="utf-8") as log:
                print(f"\nОшибка контракта выгрузки: {exc}", file=log)
            raise JobFailure("invalid_result", "Модель вернула выгрузку неверного формата.") from exc
        names = archive_files(output, result)
        if names:
            write_archive(output, names, directory / "submission.zip")
        result["archive_files"] = names
        write_json(directory / "result.json", result)
