"""A small local dashboard server backed directly by SQLite.

The static export could only ever embed a slice of the data — every filter in
the browser then operated on that slice, so a job that existed in SQLite and in
the CSV could still be invisible on the page.  This serves the *complete*
canonical opportunity set instead: filtering, counting, sorting and pagination
all happen in SQL, and the browser is handed one page of cards at a time.

Everything here is standard library.  There is no framework, no build step, no
external service, and nothing leaves the machine: the socket is bound to
127.0.0.1 and the only data source is the local database file.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from .config import AppConfig
from .dashboard import render_shell
from .db import Database


LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Closed allow-lists.  Anything outside them falls back to the default rather
#: than reaching a query, so a hand-typed URL cannot produce a 500.
_SORTS = tuple(Database.SORT_ORDERS)
_PAGE_SIZES = Database.PAGE_SIZES
_GEO_TAGS = ("nyc", "new_jersey", "remote_us", "other_us")
_EXPERIENCE_BUCKETS = (
    "new_grad",
    "zero_experience",
    "zero_to_one",
    "unknown",
    "one_plus",
    "two_plus",
    "blocker",
)
_CATEGORIES = ("strong_match", "possible_match", "weak_match", "likely_ineligible")
_CONFIDENCE = ("low", "medium", "high")
_BLOCKERS = ("clear", "review", "blocked")
_AUTH_STATES = ("clear", "uncertain", "restriction", "warning", "flagged")
_STATUSES = ("active", "closed", "all")
_SOFTWARE = ("software", "other", "all")
_REMOTE = ("remote", "onsite", "all")
_FRESHNESS = ("all", "today", "1", "3", "7", "14", "30")


def _one(values: Mapping[str, list[str]], key: str, default: str = "") -> str:
    found = values.get(key)
    return found[0].strip() if found and found[0] is not None else default


def _many(values: Mapping[str, list[str]], key: str, allowed: tuple[str, ...]) -> list[str]:
    """Read a repeated or comma-separated parameter, keeping only known values."""

    raw: list[str] = []
    for item in values.get(key, []):
        raw.extend(part.strip() for part in item.split(","))
    return [value for value in dict.fromkeys(raw) if value in allowed]


def _choice(values: Mapping[str, list[str]], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = _one(values, key)
    return value if value in allowed else default


def _integer(values: Mapping[str, list[str]], key: str, default: int, low: int, high: int) -> int:
    try:
        number = int(_one(values, key, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _freshness_epoch(value: str, now: datetime | None = None) -> int | None:
    """Convert a freshness choice into an epoch-millisecond cutoff."""

    if value == "all" or not value:
        return None
    reference = now or datetime.now(timezone.utc)
    if value == "today":
        start = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return int((reference - timedelta(days=days)).timestamp() * 1000)


def parse_query(query: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate a query string into keyword arguments for the database layer.

    Every value is checked against an allow-list or clamped to a range, so an
    invalid parameter degrades to the default instead of raising.
    """

    values = parse_qs(query, keep_blank_values=True)
    freshness = _choice(values, "freshness", _FRESHNESS, "all")
    source = _one(values, "freshness_source", "published")
    cutoff = _freshness_epoch(freshness, now)
    parsed: dict[str, Any] = {
        "page": _integer(values, "page", 1, 1, 1_000_000),
        "page_size": _integer(values, "page_size", Database.DEFAULT_PAGE_SIZE, 1, 100),
        "sort": _choice(values, "sort", _SORTS, "best"),
        "search": _one(values, "search")[:200],
        "title": _one(values, "title")[:200],
        "location": _one(values, "location")[:200],
        "company": _one(values, "company")[:200],
        "category": _choice(values, "category", (*_CATEGORIES, "all"), "all"),
        "min_score": _integer(values, "min_score", 0, 0, 100),
        "confidence": _choice(values, "confidence", (*_CONFIDENCE, "all"), "all"),
        "experience": _many(values, "experience", _EXPERIENCE_BUCKETS),
        "geo": _many(values, "geo", _GEO_TAGS),
        "blocker": _choice(values, "blocker", (*_BLOCKERS, "all"), "all"),
        "authorization": _choice(values, "authorization", _AUTH_STATES, "all"),
        "status": _choice(values, "status", _STATUSES, "active"),
        "software": _choice(values, "software", _SOFTWARE, "software"),
        "remote": _choice(values, "remote", _REMOTE, "all"),
        "new_only": _one(values, "new_only").lower() in {"1", "true", "yes", "on"},
    }
    if parsed["page_size"] not in _PAGE_SIZES:
        parsed["page_size"] = Database.DEFAULT_PAGE_SIZE
    if cutoff is not None:
        key = "discovered_after" if source == "discovered" else "published_after"
        parsed[key] = cutoff
    return parsed


class DashboardServer:
    """Owns the socket, the shared database handle, and the lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.config = config
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(_DashboardHandler):
            dashboard = server

        return Handler

    def start(self) -> str:
        """Bind and serve in a background thread; returns the dashboard URL."""

        with self._lock:
            if self._httpd is not None:
                return self.url
            ThreadingHTTPServer.allow_reuse_address = True
            self._httpd = ThreadingHTTPServer((self.host, self.port), self._handler())
            self.port = self._httpd.server_address[1]
            self._thread = threading.Thread(
                target=self._httpd.serve_forever,
                name="jobfinder-dashboard",
                daemon=True,
            )
            self._thread.start()
        LOGGER.info("Dashboard serving at %s", self.url)
        return self.url

    def serve_forever(self) -> None:
        """Serve on this thread until interrupted."""

        self.start()
        assert self._thread is not None
        try:
            while self._thread.is_alive():
                self._thread.join(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
            LOGGER.info("Dashboard stopped")
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> DashboardServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # -- data ------------------------------------------------------------
    def _database(self) -> Database:
        """A fresh handle per request; SQLite connections are per-thread."""

        database = Database(self.config.database_path)
        database.initialize()
        return database

    def jobs(self, query: str) -> dict[str, Any]:
        parameters = parse_query(query)
        database = self._database()
        try:
            return database.query_opportunities(**parameters)
        finally:
            database.close()

    def facets(self, query: str) -> dict[str, Any]:
        parameters = parse_query(query)
        for key in ("page", "page_size", "sort"):
            parameters.pop(key, None)
        database = self._database()
        try:
            return database.opportunity_facets(**parameters)
        finally:
            database.close()

    def detail(self, opportunity_id: int) -> dict[str, Any] | None:
        database = self._database()
        try:
            return database.opportunity_detail(opportunity_id)
        finally:
            database.close()

    def summary(self) -> dict[str, Any]:
        database = self._database()
        try:
            counts = database.summary_counts()
            counts["opportunities"] = database.opportunity_count()
            return counts
        finally:
            database.close()


class _DashboardHandler(BaseHTTPRequestHandler):
    """Routes.  Only GET, only these paths, only from the local socket."""

    dashboard: DashboardServer
    server_version = "jobfinder"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        LOGGER.debug("%s - %s", self.address_string(), format % args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A purely local page: deny embedding and keep the browser from
        # reaching anywhere else on its behalf.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        try:
            if path == "/":
                body = render_shell(self.dashboard.summary()).encode("utf-8")
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            elif path == "/api/jobs":
                self._json(self.dashboard.jobs(parts.query))
            elif path == "/api/facets":
                self._json(self.dashboard.facets(parts.query))
            elif path == "/api/summary":
                self._json(self.dashboard.summary())
            elif path == "/api/health":
                self._json({"status": "ok", "service": "jobfinder"})
            elif path.startswith("/api/jobs/"):
                identifier = path.rsplit("/", 1)[-1]
                if not identifier.isdigit():
                    self._json({"error": "invalid id"}, HTTPStatus.BAD_REQUEST)
                    return
                detail = self.dashboard.detail(int(identifier))
                if detail is None:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(detail)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except BrokenPipeError:  # the browser navigated away mid-response
            LOGGER.debug("Client disconnected during %s", path)
        except Exception as exc:  # one bad request must never kill the server
            LOGGER.exception("Dashboard request failed: %s", path)
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()


def is_running(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.4) -> bool:
    """True when something is already listening locally on that port."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def serve(
    config: AppConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    background: bool = False,
) -> DashboardServer:
    """Start the dashboard, refreshing the opportunity table if it is empty."""

    from .pipeline import refresh_opportunities

    database = Database(config.database_path)
    database.initialize()
    try:
        if database.opportunity_count() == 0:
            LOGGER.info("Building the dashboard's opportunity set for the first time…")
            refresh_opportunities(config, database)
    finally:
        database.close()

    server = DashboardServer(config, host=host, port=port)
    if background:
        server.start()
    return server


def data_directory(config: AppConfig) -> Path:
    return config.database_path.parent


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DashboardServer",
    "data_directory",
    "is_running",
    "parse_query",
    "serve",
]
