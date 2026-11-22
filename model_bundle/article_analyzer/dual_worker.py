"""LAN worker for receiving PDFs and running a local Qwen HTTP service."""
import argparse
import base64
from functools import partial
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from .dual_qwen import analyze
from .qwen_api import QwenApi, QwenApiError

PDF_BYTES = 64 * 1024 * 1024
REQUEST_BYTES = 4 * ((PDF_BYTES + 2) // 3) + 256 * 1024
RESPONSE_BYTES = 16 * 1024 * 1024
WORKER_PROTOCOL = 2


def connection_url(address):
    url = urlsplit(address)
    if url.scheme not in {"http","https"} or not url.hostname or url.username or url.query or url.fragment:
        raise ValueError("Нужен адрес LAN-воркера вида http://<IPv4>:8091.")
    return url


def available_worker(qwen_address):
    url = connection_url(qwen_address)
    host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
    address = f"http://{host}:8091"
    conn = HTTPConnection(url.hostname,8091,timeout=2)
    try:
        conn.request("GET","/health")
        reply=conn.getresponse()
        if reply.status==200 and json.loads(reply.read(4096)).get("nexus_pdf_worker")==WORKER_PROTOCOL:
            return address
    except (OSError, HTTPException, ValueError, AttributeError):
        pass
    finally:
        conn.close()
    return None


def send_pdf(address, token, timeout, path, criteria, plan, lane, retry_pages=None):
    url = connection_url(address)
    if path.stat().st_size > PDF_BYTES:
        raise QwenApiError(f"{path.name}. Размер PDF для второго ноутбука превышает 64 МиБ.")
    payload = json.dumps({"pdf":base64.b64encode(path.read_bytes()).decode("ascii"),
                          "criteria":criteria, "plan":plan, "lane":lane, "pages":retry_pages},
                         ensure_ascii=False).encode("utf-8")
    client = HTTPSConnection if url.scheme=="https" else HTTPConnection
    conn = client(url.hostname,url.port,timeout=timeout)
    try:
        conn.request("POST",url.path.rstrip("/")+"/v1/analyze",body=payload,
                     headers={"Content-Type":"application/json","Authorization":"Bearer "+token})
        res = conn.getresponse()
        if res.status != 200:
            raise QwenApiError(f"LAN-воркер второго ноутбука вернул HTTP {res.status}. Проверьте токен и журнал воркера.")
        raw=res.read(RESPONSE_BYTES+1)
        if len(raw)>RESPONSE_BYTES:
            raise QwenApiError("LAN-воркер вернул слишком большой ответ.")
        result=json.loads(raw)
        if (not isinstance(result,dict) or result.get("worker_protocol")!=WORKER_PROTOCOL or
                not {"rows","errors","pages","input_sha256"} <= result.keys()):
            raise ValueError("response shape")
        return result
    except (OSError, HTTPException, ValueError) as exc:
        raise QwenApiError("Не удалось получить ответ LAN-воркера. Проверьте подключение второго ноутбука.") from exc
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def response(self, status, data):
        reply = json.dumps(data,ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(reply)))
        self.end_headers()
        try:
            self.wfile.write(reply)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.path == "/health":
            self.response(200,{"status":"ok","nexus_pdf_worker":WORKER_PROTOCOL})
        else:
            self.response(404,{"error":"not found"})

    def do_POST(self):
        if self.path != "/v1/analyze":
            return self.response(404,{"error":"not found"})
        supplied = self.headers.get("Authorization","").encode()
        if not secrets.compare_digest(supplied, ("Bearer "+self.server.worker_token).encode()):
            return self.response(401,{"error":"invalid token"})
        if not self.server.worker_slot.acquire(blocking=False):
            return self.response(429,{"error":"worker busy"})
        try:
            self.connection.settimeout(30)
            length=int(self.headers.get("Content-Length",0))
            if not 0 < length <= REQUEST_BYTES:
                return self.response(413,{"error":"invalid request size"})
            args=json.loads(self.rfile.read(length))
            if not isinstance(args,dict) or set(args) != {"pdf","criteria","plan","lane","pages"}:
                raise ValueError("invalid request")
            pdf=base64.b64decode(args["pdf"],validate=True)
            if len(pdf)>PDF_BYTES or b"%PDF-" not in pdf[:1024]:
                raise ValueError("invalid PDF")
            plan,criteria,pages = args["plan"],args["criteria"],args["pages"]
            if (not isinstance(plan,dict) or not 0 < len(plan) <= 32 or not isinstance(criteria,str) or
                    len(criteria)>100000 or args["lane"] not in {"a","b"} or
                    any(not isinstance(k,str) or not isinstance(v,list) or len(v)>80 or
                        not all(isinstance(c,str) for c in v) for k,v in plan.items()) or
                    (pages is not None and (not isinstance(pages,list) or any(type(n) is not int or n<1 for n in pages)))):
                raise ValueError("invalid criteria, columns or pages")
            with TemporaryDirectory(prefix="nexus-lan-") as directory:
                path=Path(directory)/"article.pdf"
                path.write_bytes(pdf)
                result=analyze(path,criteria,plan,args["lane"],self.server.completion,pages)
            result["worker_protocol"]=WORKER_PROTOCOL
            self.response(200,result)
        except Exception as exc:
            self.log_error("%s",type(exc).__name__)
            self.response(422,{"error":"analysis failed"})
        finally:
            self.server.worker_slot.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8091)
    parser.add_argument("--qwen",default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model",default="nexus-qwen3-4b")
    args=parser.parse_args()
    token=os.getenv("NEXUS_DUAL_WORKER_TOKEN","")
    if len(token)<24:
        parser.error("Set NEXUS_DUAL_WORKER_TOKEN to a shared secret of at least 24 characters.")
    with ThreadingHTTPServer((args.bind,args.port),Handler) as server:
        server.worker_token=token
        server.worker_slot=threading.BoundedSemaphore(1)
        server.completion=QwenApi(args.qwen,args.model,180).complete
        print(f"NEXUS PDF worker http://{args.bind}:{server.server_port}",flush=True)
        server.serve_forever()


if __name__=="__main__":
    main()
