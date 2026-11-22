"""LAN transport for the Qwen criteria interpreter (Python standard library)."""
from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit


class QwenApiError(RuntimeError):
    """Safe to show in the web job. Never include request text or server output."""


@dataclass(frozen=True)
class QwenApi:
    base_url: str
    model: str
    timeout_seconds: float

    @classmethod
    def configured(cls, path: Path):
        keys = {"base_url": "NEXUS_QWEN_API_BASE", "model": "NEXUS_QWEN_API_MODEL",
                "timeout_seconds": "NEXUS_QWEN_API_TIMEOUT"}
        overrides = {key: os.environ[name].strip() for key, name in keys.items() if os.getenv(name, "").strip()}
        if not path.exists() and not overrides:
            return None
        try:
            values = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
            if not isinstance(values, dict) or values.keys() - keys.keys():
                raise ValueError
            values.update(overrides)
            address = values["base_url"].strip().rstrip("/")
            model = values["model"].strip()
            timeout = values.get("timeout_seconds", 180)
            if isinstance(timeout, bool):
                raise ValueError
            timeout = float(timeout)
            url = urlsplit(address)
            if (not model or not address.isascii() or any(char.isspace() for char in address)
                    or url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment or not url.path.endswith("/v1")
                    or url.port == 0 or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError, OSError) as exc:
            raise QwenApiError("Проверьте настройки Qwen в qwen-api.json или NEXUS_QWEN_API_*: нужны "
                               "адрес API с /v1, идентификатор модели и положительный таймаут в секундах.") from exc
        return cls(address, model, timeout)

    def complete(self, system, user, schema, max_tokens):
        output_format = ({"type": "json_schema", "json_schema": {"name": "criteria_rules",
                          "strict": True, "schema": schema}} if schema else {"type": "json_object"})
        payload = {"model": self.model, "messages": [{"role": "system", "content": system},
                   {"role": "user", "content": user}], "temperature": 0, "max_tokens": max_tokens,
                   "stream": False, "response_format": output_format}
        if "qwen3" in self.model.casefold():
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        url = urlsplit(self.base_url)
        # Connect directly to the selected host, including on laptops with a corporate proxy.
        connection_type = HTTPSConnection if url.scheme == "https" else HTTPConnection
        conn = connection_type(url.hostname, url.port, timeout=self.timeout_seconds)
        try:
            conn.request("POST", url.path + "/chat/completions", body=json.dumps(payload, ensure_ascii=False).encode(),
                         headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"})
            response = conn.getresponse()
            if response.status != 200:
                if response.status in (408, 504):
                    raise TimeoutError
                hint = {400: "Проверьте идентификатор модели, поддержку JSON Schema и размер контекста.",
                        401: "Сервис требует авторизации.", 403: "Сервис запретил доступ.",
                        404: "Проверьте адрес API с /v1 и идентификатор модели.",
                        429: "Сервер занят.", 503: "Модель ещё загружается или сервер занят."}
                raise QwenApiError(f"API Qwen вернул HTTP {response.status}. " +
                                   hint.get(response.status, "Проверьте журнал сервера на втором ноутбуке."))
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise QwenApiError("Ответ API Qwen превысил допустимый размер.")
            if response.length:
                raise QwenApiError("Связь с API Qwen прервалась во время передачи ответа. "
                                   "Проверьте соединение между ноутбуками и повторите задание.")
        except TimeoutError as exc:
            raise QwenApiError(f"API Qwen не ответил за {self.timeout_seconds:g} с. Проверьте загрузку "
                               "второго ноутбука и значение timeout_seconds.") from exc
        except (OSError, HTTPException) as exc:
            raise QwenApiError("Не удалось связаться с API Qwen. Проверьте адрес, работу сервера на втором "
                               "ноутбуке и соединение по локальной сети.") from exc
        finally:
            conn.close()
        try:
            choice = json.loads(body)["choices"][0]
            if choice.get("finish_reason") == "length":
                raise QwenApiError("Ответ Qwen оборвался по лимиту токенов. Сократите страницу критериев "
                                   "или проверьте размер контекста на сервере.")
            raw = choice["message"]["content"]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise QwenApiError("API Qwen вернул ответ неверного формата. Ожидается текст "
                               "в choices[0].message.content.") from exc
        return raw.strip(), 0
