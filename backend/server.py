"""Local HTTP API for scientific PDF extraction. Start with python backend/server.py."""

import argparse
import json
import logging
import queue
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import SpooledTemporaryFile
from urllib.parse import parse_qs, quote, unquote, urlsplit

# Embedded Python uses an isolated module search path on Windows.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobs import MAX_WAITING, Jobs, archive_files, read_json, table_page, write_archive
from uploads import MIB, MAX_UPLOAD, InvalidUpload, read_form


BACKEND_DIR = Path(__file__).resolve().parent
WEB_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


class ApiServer(ThreadingHTTPServer):
    def __init__(self, address, jobs, origins, *, frontend_dir=None, site_origin=None):
        super().__init__(address, Handler)
        self.jobs = jobs
        self.origins = set(origins)
        self.uploads = threading.BoundedSemaphore(2)
        self.frontend_dir = frontend_dir
        self.site_origin = site_origin


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def _handle(self, method):
        self.allowed_origin = None
        length = self.headers.get("Content-Length", "")
        self.body_remaining = int(length) if method == "POST" and len(length) <= 18 and length.isdecimal() else 0
        try:
            self._check_origin()
            target = urlsplit(self.path)
            parts = target.path.strip("/").split("/")
            asset = self._web_asset(target.path) if method == "GET" else None
            if asset:
                filename, content_type = asset
                self._file(filename, content_type, disposition=None)
            elif method == "GET" and parts == ["api", "jobs"]:
                self._headers(303, 0, extra={"Location": "/"})
            elif method == "OPTIONS" and parts[0] == "api":
                self._headers(204, 0, extra={"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                            "Access-Control-Allow-Headers": "Content-Type"})
            elif method == "GET" and parts == ["api", "health"]:
                jobs = self.server.jobs
                self._json(200, {"status": "ok", "runner_configured": jobs.configured,
                                 "queued_jobs": jobs.pending.qsize(), "queue_limit": MAX_WAITING})
            elif method == "POST" and parts == ["api", "jobs"]:
                self._create_job()
            elif method == "GET" and len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
                self._read_job(parts[2], parts[3:], target.query)
            else:
                raise ApiError(404, "not_found", "Такого адреса нет.")
        except InvalidUpload as exc:
            self._json(exc.status, {"error": {"code": "invalid_upload", "message": str(exc)}})
        except ApiError as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except TimeoutError:
            self._json(408, {"error": {"code": "request_timeout", "message": "Время запроса истекло."}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.exception("Ошибка API при обработке %s", method)
            self._json(500, {"error": {"code": "internal_error", "message": "Не удалось обработать запрос."}})

    def _web_asset(self, path):
        frontend = self.server.frontend_dir
        if frontend is None:
            if path in WEB_ASSETS:
                name, mime = WEB_ASSETS[path]
                return BACKEND_DIR / "web" / name, mime
            return None
        if path == "/" or re.fullmatch(r"/dashboard/[a-f0-9]{32}/?", path):
            return frontend / "index.html", "text/html; charset=utf-8"
        asset = re.fullmatch(r"/assets/([A-Za-z0-9_-]+\.(js|css))", path)
        if asset:
            filename = frontend / "assets" / asset[1]
            if filename.is_file() and not filename.is_symlink():
                mime = "text/javascript" if asset[2] == "js" else "text/css"
                return filename, f"{mime}; charset=utf-8"
        return None

    def _check_origin(self):
        local_ip = self.connection.getsockname()[0]
        port = self.server.server_port
        host = self.headers.get("Host", "").lower()
        local_names = {"localhost", "127.0.0.1", local_ip}
        allowed_hosts = {f"{name}:{port}" for name in local_names}
        if port == 80:
            allowed_hosts.update(local_names)
        site = self.server.site_origin
        site_host = site is not None and site.netloc.lower() == host
        if host not in allowed_hosts and not site_host:
            raise ApiError(403, "host_forbidden", "Используйте адрес, указанный при запуске сервера.")
        origin = self.headers.get("Origin")
        same_origin = f"{site.scheme if site_host else 'http'}://{host}"
        if origin and origin != same_origin and origin not in self.server.origins:
            raise ApiError(403, "origin_forbidden", "Этот адрес интерфейса не подключён к API.")
        self.allowed_origin = origin

    def _create_job(self):
        jobs = self.server.jobs
        if not jobs.configured:
            raise ApiError(503, "runner_unavailable", "Пакет модели ещё не подключён.")
        if jobs.pending.full():
            raise ApiError(429, "queue_full", "Очередь заполнена. Повторите отправку позже.")
        if not self.server.uploads.acquire(timeout=5):
            raise ApiError(429, "upload_busy", "Сервер принимает другие файлы. Повторите отправку позже.")
        try:
            criteria, files, criteria_pdf, criteria_pages = read_form(self.headers, self._read_body)
            try:
                state = jobs.enqueue(criteria, files, criteria_pdf, criteria_pages)
            except queue.Full as exc:
                raise ApiError(429, "queue_full", "Очередь заполнена. Повторите отправку позже.") from exc
        finally:
            self.server.uploads.release()
        state["status_url"] = f"/api/jobs/{state['job_id']}"
        self._json(202, state, extra={"Location": state["status_url"]})

    def _read_body(self, size):
        data = self.rfile.read(size)
        self.body_remaining -= len(data)
        return data

    def _discard_body(self):
        # Closing a Windows socket with unread request data can reset the HTTP response.
        if not 0 < self.body_remaining <= MAX_UPLOAD:
            return
        deadline = time.monotonic() + 2
        try:
            while self.body_remaining:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                data = self.rfile.read1(min(self.body_remaining, MIB))
                if not data:
                    break
                self.body_remaining -= len(data)
        except OSError:
            pass
        finally:
            self.connection.settimeout(45)

    def _read_job(self, job_id, route, query):
        jobs = self.server.jobs
        state = jobs.status(job_id)
        if state is None:
            raise ApiError(404, "job_not_found", "Задание не найдено.")
        if not route:
            self._json(200, state)
            return
        if state["status"] != "completed":
            raise ApiError(409, "result_unavailable", "Результат задания пока недоступен.")

        directory = jobs.root / job_id
        url = f"/api/jobs/{job_id}"
        if len(route) == 2 and route[0] == "sources":
            name = unquote(route[1])
            if name not in state["source_files"]:
                raise ApiError(404, "source_not_found", "Такой статьи нет в задании.")
            self._file(directory / "input" / name, "application/pdf", disposition="inline")
            return

        result = read_json(directory / "result.json")
        if route == ["download"]:
            self._download(directory, result)
            return
        if route == ["result"]:
            for file in result["files"]:
                encoded_name = quote(file["name"], safe="")
                file["url"] = f"{url}/files/{encoded_name}"
                if file["format"] == "csv":
                    file["table_url"] = f"{url}/tables/{encoded_name}"
            result["sources"] = [{"name": name, "publication_id": Path(name).stem,
                                  "url": f"{url}/sources/{quote(name, safe='')}"} for name in state["source_files"]]
            result["archive_files"] = archive_files(directory / "output", result)
            result["download_url"] = f"{url}/download" if result["archive_files"] else None
            self._json(200, result)
            return
        if len(route) == 2 and route[0] in ("files", "tables"):
            name = unquote(route[1])
            file = next((item for item in result["files"] if item["name"] == name), None)
            if file is None:
                raise ApiError(404, "file_not_found", "Такого файла нет в выгрузке.")
            path = directory / "output" / name
            if route[0] == "files":
                mime = {"csv": "text/csv; charset=utf-8", "json": "application/json; charset=utf-8"}
                self._file(path, mime[file["format"]])
                return
            if file["format"] == "csv":
                self._table(path, file, query)
                return
        raise ApiError(404, "not_found", "Такого адреса нет.")

    def _download(self, directory, result):
        names = archive_files(directory / "output", result)
        if not names:
            raise ApiError(404, "empty_export", "В задании нет заполненных CSV для скачивания.")
        if "archive_files" in result:
            self._file(directory / "submission.zip", "application/zip")
            return
        with SpooledTemporaryFile(max_size=8 * MIB) as stream:
            write_archive(directory / "output", names, stream)
            size = stream.tell()
            stream.seek(0)
            self._headers(200, size, "application/zip", {"Content-Disposition": 'attachment; filename="submission.zip"'})
            self._copy_response(stream)

    def _table(self, path, file, query):
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=8)
            (offset,) = params.get("offset", ["0"])
            (limit,) = params.get("limit", ["50"])
            offset, limit = int(offset), int(limit)
            if offset < 0 or not 1 <= limit <= 200:
                raise ValueError
        except ValueError as exc:
            raise ApiError(400, "invalid_pagination", "Нужны offset от 0 и limit от 1 до 200.") from exc
        rows = table_page(path, offset, limit) if offset < file["row_count"] else []
        self._json(200, {"columns": file["columns"], "rows": rows, "offset": offset, "total": file["row_count"]})

    def _file(self, path, content_type, disposition="attachment"):
        with path.open("rb") as stream:
            headers = None
            if disposition:
                name = quote(path.name, safe="")
                headers = {"Content-Disposition": f"{disposition}; filename*=UTF-8''{name}"}
            self._headers(200, path.stat().st_size, content_type, headers)
            self._copy_response(stream)

    def _copy_response(self, stream):
        try:
            shutil.copyfileobj(stream, self.wfile, 64 * 1024)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _json(self, status, data, extra=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            if status >= 400:
                self._discard_body()
            self._headers(status, len(body), "application/json; charset=utf-8", extra)
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _headers(self, status, length, content_type=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Vary", "Origin")
        if content_type:
            self.send_header("Content-Type", content_type)
        if self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
            self.send_header("Access-Control-Expose-Headers", "Content-Disposition, Location")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, default=BACKEND_DIR.parent / "model_bundle" / "run_inference.py")
    parser.add_argument("--model-python", type=Path, help="Model interpreter; uses the bundled runtime when present")
    parser.add_argument("--jobs-dir", type=Path, default=BACKEND_DIR / "runs")
    parser.add_argument("--frontend-dir", type=Path, help="Serve a built frontend from this directory")
    parser.add_argument("--site-origin", help="Canonical website origin behind the reverse proxy")
    parser.add_argument("--cors-origin", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum model runtime in seconds")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open-browser", action="store_true", help="Open the site after binding its port")
    args = parser.parse_args()
    if args.timeout < 1 or not 0 <= args.port <= 65535:
        parser.error("Use a positive --timeout and a port from 0 to 65535")
    frontend_dir = args.frontend_dir.resolve() if args.frontend_dir else None
    if frontend_dir is not None and not (frontend_dir / "index.html").is_file():
        parser.error("Build the frontend and pass its output directory to --frontend-dir")
    site = None
    if args.site_origin:
        try:
            site = urlsplit(args.site_origin)
            if (site.scheme not in ("http", "https") or not site.hostname or site.username is not None
                    or site.path not in ("", "/") or site.query or site.fragment):
                raise ValueError
            _ = site.port
        except ValueError:
            parser.error("--site-origin must be an http(s) origin without a path or query")
    model_python = args.model_python
    if model_python is None:
        bundled_python = args.runner.resolve().parent / "runtime" / "python.exe"
        model_python = bundled_python if bundled_python.is_file() else Path(sys.executable)
    jobs = Jobs(args.jobs_dir, args.runner, model_python, args.timeout)
    model_state = "готов" if jobs.configured else "ожидает подключения"
    with ApiServer((args.host, args.port), jobs, args.cors_origin, frontend_dir=frontend_dir, site_origin=site) as server:
        jobs.start()
        print(f"API http://{args.host}:{server.server_port} | пакет модели {model_state}", flush=True)
        if args.open_browser:
            import webbrowser
            webbrowser.open(f"http://{args.host}:{server.server_port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            jobs.close()


if __name__ == "__main__":
    main()
