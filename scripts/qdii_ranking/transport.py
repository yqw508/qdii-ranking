"""HTTP transport and request metrics shared by ranking sources."""

from __future__ import annotations

import time
import json
import urllib.parse
import urllib.error
import urllib.request
from threading import Lock
from typing import Any, Callable, Type


class TransportError(RuntimeError):
    """Raised when a source request cannot be completed after retries."""


class HttpTransport:
    def __init__(
        self,
        retries: int = 4,
        timeout: int = 30,
        user_agent: str = "",
        category_resolver: Callable[[str], str] | None = None,
        error_type: Type[Exception] = TransportError,
    ) -> None:
        self.retries = retries
        self.timeout = timeout
        self.user_agent = user_agent
        self._category_resolver = category_resolver or (lambda _url: "other")
        self._error_type = error_type
        self._category_metrics: dict[str, dict[str, float | int]] = {}
        self._category_lock = Lock()

    def _record_call(self, category: str) -> None:
        with self._category_lock:
            item = self._category_metrics.setdefault(
                category,
                {
                    "calls": 0,
                    "attempts": 0,
                    "retries": 0,
                    "bytes": 0,
                    "not_modified": 0,
                    "seconds": 0.0,
                },
            )
            item["calls"] = int(item["calls"]) + 1

    def _record_attempt(
        self,
        category: str,
        elapsed: float,
        body_size: int = 0,
        not_modified: bool = False,
        retry: bool = False,
    ) -> None:
        with self._category_lock:
            item = self._category_metrics[category]
            item["attempts"] = int(item["attempts"]) + 1
            item["retries"] = int(item["retries"]) + int(retry)
            item["bytes"] = int(item["bytes"]) + body_size
            item["not_modified"] = int(item["not_modified"]) + int(not_modified)
            item["seconds"] = round(float(item["seconds"]) + elapsed, 3)

    def _request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        referer: str | None = None,
        extra_headers: dict[str, str] | None = None,
        allow_not_modified: bool = False,
    ) -> tuple[int, bytes, dict[str, str]]:
        headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        category = self._category_resolver(url)
        self._record_call(category)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_body = response.read()
                    self._record_attempt(
                        category,
                        time.perf_counter() - started,
                        len(response_body),
                        retry=attempt > 0,
                    )
                    return response.status, response_body, dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                if allow_not_modified and exc.code == 304:
                    self._record_attempt(
                        category,
                        time.perf_counter() - started,
                        not_modified=True,
                        retry=attempt > 0,
                    )
                    return 304, b"", dict(exc.headers.items())
                last_error = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            self._record_attempt(category, time.perf_counter() - started, retry=attempt > 0)
            if attempt + 1 < self.retries:
                time.sleep(0.5 * (2**attempt))
        raise self._error_type(f"Failed to fetch {url}: {last_error}") from last_error

    def get_bytes(
        self,
        url: str,
        referer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self._request_bytes(url, referer=referer, extra_headers=headers)[1]

    def get_conditional_text(
        self,
        url: str,
        referer: str | None = None,
        last_modified: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> tuple[int, str | None, str | None]:
        headers = {"If-Modified-Since": last_modified} if last_modified else None
        status, body, response_headers = self._request_bytes(
            url,
            referer=referer,
            extra_headers=headers,
            allow_not_modified=bool(last_modified),
        )
        response_last_modified = next(
            (value for key, value in response_headers.items() if key.lower() == "last-modified"),
            None,
        )
        return (
            status,
            None if status == 304 else body.decode(encoding, errors="replace"),
            response_last_modified or last_modified,
        )

    def get_text(self, url: str, referer: str | None = None, encoding: str = "utf-8-sig") -> str:
        return self._request_bytes(url, referer=referer)[1].decode(encoding, errors="replace")

    def get_json(self, url: str, referer: str | None = None) -> dict[str, Any]:
        try:
            return json.loads(self.get_text(url, referer=referer))
        except json.JSONDecodeError as exc:
            raise self._error_type(f"Invalid JSON from {url}: {exc}") from exc

    def post_form_json(
        self, url: str, fields: dict[str, str], referer: str | None = None
    ) -> Any:
        body = urllib.parse.urlencode(fields).encode("ascii")
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        category = self._category_resolver(url)
        self._record_call(category)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_body = response.read()
                result = json.loads(response_body.decode("utf-8-sig", errors="replace"))
                self._record_attempt(
                    category,
                    time.perf_counter() - started,
                    len(response_body),
                    retry=attempt > 0,
                )
                return result
            except (
                json.JSONDecodeError,
                urllib.error.HTTPError,
                urllib.error.URLError,
                TimeoutError,
            ) as exc:
                last_error = exc
                self._record_attempt(
                    category, time.perf_counter() - started, retry=attempt > 0
                )
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        raise self._error_type(f"Failed to fetch {url}: {last_error}") from last_error

    def metrics_snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._category_lock:
            return {category: dict(values) for category, values in self._category_metrics.items()}
