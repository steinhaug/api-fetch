"""MySQL connection pool and cache queries for the WebFetch API.

pymysql ships no native pool, so this module implements a small thread-safe
pool over a queue of live connections (size = `config.DB_POOL_SIZE`). All cache
reads/writes for the `pages` and `searches` tables live here.

The two tables are created on first use via `init_db()` (CREATE TABLE IF NOT
EXISTS), so a fresh database needs no manual schema step beyond CREATE DATABASE.
"""

import json
import queue
import threading
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

import config

# ── Schema ───────────────────────────────────────────────────────────────────

_CREATE_PAGES = """
CREATE TABLE IF NOT EXISTS pages (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    url             TEXT NOT NULL,
    url_hash        CHAR(64) NOT NULL,
    domain          VARCHAR(255) NOT NULL,
    title           VARCHAR(1000),
    author          VARCHAR(500),
    published_date  DATE,
    raw_html        LONGTEXT,
    stripped_text   LONGTEXT,
    links_json      JSON,
    summary         TEXT,
    page_size_chars INT UNSIGNED,
    page_size_tokens INT UNSIGNED,
    fetch_mode      ENUM('httpx','playwright') NOT NULL,
    fetch_mode_reason VARCHAR(100) NOT NULL,
    source_tier     ENUM('tier1','tier2','unknown') NOT NULL DEFAULT 'unknown',
    is_premium_source TINYINT(1) NOT NULL DEFAULT 0,
    cached_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_url_hash (url_hash),
    INDEX idx_domain (domain),
    INDEX idx_cached_at (cached_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_CREATE_SEARCHES = """
CREATE TABLE IF NOT EXISTS searches (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    query_hash      CHAR(64) NOT NULL,
    query_text      VARCHAR(1000) NOT NULL,
    date_from       DATE,
    date_to         DATE,
    max_results     TINYINT UNSIGNED NOT NULL DEFAULT 10,
    domains_filter  JSON,
    results_json    JSON NOT NULL,
    result_count    TINYINT UNSIGNED NOT NULL,
    cached_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_query_hash (query_hash),
    INDEX idx_cached_at (cached_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


# ── Connection pool ───────────────────────────────────────────────────────────


def _new_connection() -> pymysql.connections.Connection:
    """Open a fresh autocommit connection using config credentials."""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


class _ConnectionPool:
    """Lazily-filled, thread-safe pool of pymysql connections."""

    def __init__(self, size: int):
        self._size = size
        self._pool: queue.Queue = queue.Queue(maxsize=size)
        self._created = 0
        self._lock = threading.Lock()

    def acquire(self) -> pymysql.connections.Connection:
        """Get a live connection from the pool, creating one if room remains."""
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._size:
                    self._created += 1
                    return _new_connection()
            # Pool is at capacity and all checked out — block for a free one.
            conn = self._pool.get()
        # Replace connections dropped by MySQL `wait_timeout`. pymysql deprecated
        # ping(reconnect=...), so probe with a trivial query and rebuild on fail.
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = _new_connection()
        return conn

    def release(self, conn: pymysql.connections.Connection) -> None:
        """Return a connection to the pool."""
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            conn.close()


_pool: _ConnectionPool | None = None


def _get_pool() -> _ConnectionPool:
    """Return the process-wide pool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = _ConnectionPool(config.DB_POOL_SIZE)
    return _pool


@contextmanager
def get_connection():
    """Yield a pooled connection for raw queries, returning it on exit."""
    pool = _get_pool()
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


def _ensure_column(cur, table: str, column: str, ddl: str) -> None:
    """Add `column` to `table` if absent (MySQL lacks ADD COLUMN IF NOT EXISTS)."""
    cur.execute(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (config.DB_NAME, table, column),
    )
    if cur.fetchone()["n"] == 0:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """Create the tables if absent and apply idempotent column migrations."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_PAGES)
            cur.execute(_CREATE_SEARCHES)
            # Migration: token count for the verbatim-size contract (task-20).
            _ensure_column(
                cur, "pages", "page_size_tokens",
                "page_size_tokens INT UNSIGNED AFTER page_size_chars",
            )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _age_hours_from_seconds(age_seconds) -> float | None:
    """Convert a DB-computed age in seconds to hours, or None."""
    if age_seconds is None:
        return None
    return round(float(age_seconds) / 3600.0, 4)


# ── Page cache ─────────────────────────────────────────────────────────────────


def get_cached_page(url_hash: str) -> dict | None:
    """Return the cached `pages` row for `url_hash`, or None if absent.

    `links_json` is decoded to a Python list and a computed `cache_age_hours`
    field is added for the caller's freshness checks.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT *, TIMESTAMPDIFF(SECOND, cached_at, NOW()) AS _age_seconds "
                "FROM pages WHERE url_hash = %s",
                (url_hash,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row.get("links_json"), str):
        try:
            row["links_json"] = json.loads(row["links_json"])
        except (ValueError, TypeError):
            row["links_json"] = None
    row["cache_age_hours"] = _age_hours_from_seconds(row.pop("_age_seconds", None))
    return row


def upsert_page(data: dict) -> None:
    """Insert or update a `pages` row keyed on `url_hash`.

    `links` (a list) is JSON-encoded into `links_json`. `cached_at` is set to
    the current time on every write so freshness reflects the latest fetch.
    """
    links_json = json.dumps(data.get("links")) if data.get("links") is not None else None
    sql = """
        INSERT INTO pages (
            url, url_hash, domain, title, author, published_date,
            raw_html, stripped_text, links_json, summary, page_size_chars,
            page_size_tokens, fetch_mode, fetch_mode_reason, source_tier,
            is_premium_source, cached_at
        ) VALUES (
            %(url)s, %(url_hash)s, %(domain)s, %(title)s, %(author)s,
            %(published_date)s, %(raw_html)s, %(stripped_text)s, %(links_json)s,
            %(summary)s, %(page_size_chars)s, %(page_size_tokens)s,
            %(fetch_mode)s, %(fetch_mode_reason)s, %(source_tier)s,
            %(is_premium_source)s, NOW()
        )
        ON DUPLICATE KEY UPDATE
            url = VALUES(url),
            domain = VALUES(domain),
            title = VALUES(title),
            author = VALUES(author),
            published_date = VALUES(published_date),
            raw_html = VALUES(raw_html),
            stripped_text = VALUES(stripped_text),
            links_json = VALUES(links_json),
            summary = VALUES(summary),
            page_size_chars = VALUES(page_size_chars),
            page_size_tokens = VALUES(page_size_tokens),
            fetch_mode = VALUES(fetch_mode),
            fetch_mode_reason = VALUES(fetch_mode_reason),
            source_tier = VALUES(source_tier),
            is_premium_source = VALUES(is_premium_source),
            cached_at = NOW()
    """
    params = {
        "url": data.get("url"),
        "url_hash": data.get("url_hash"),
        "domain": data.get("domain"),
        "title": data.get("title"),
        "author": data.get("author"),
        "published_date": data.get("published_date"),
        "raw_html": data.get("raw_html"),
        "stripped_text": data.get("stripped_text"),
        "links_json": links_json,
        "summary": data.get("summary"),
        "page_size_chars": data.get("page_size_chars"),
        "page_size_tokens": data.get("page_size_tokens"),
        "fetch_mode": data.get("fetch_mode"),
        "fetch_mode_reason": data.get("fetch_mode_reason"),
        "source_tier": data.get("source_tier", "unknown"),
        "is_premium_source": 1 if data.get("is_premium_source") else 0,
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def update_page(url_hash: str, **fields) -> None:
    """Update specific columns on a `pages` row without touching `cached_at`.

    Used for lazy backfills (summary, token count) on cache hits. Only a small
    whitelist of columns may be set, to keep this safe from arbitrary keys.
    """
    allowed = {"summary", "page_size_tokens", "links_json"}
    cols = {k: v for k, v in fields.items() if k in allowed}
    if not cols:
        return
    set_clause = ", ".join(f"{k} = %s" for k in cols)
    params = list(cols.values()) + [url_hash]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE pages SET {set_clause} WHERE url_hash = %s", params
            )


# ── Search cache ───────────────────────────────────────────────────────────────


def get_cached_search(query_hash: str) -> dict | None:
    """Return the cached `searches` row for `query_hash`, or None if absent.

    `results_json` and `domains_filter` are decoded to Python objects and a
    computed `cache_age_hours` field is added.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT *, TIMESTAMPDIFF(SECOND, cached_at, NOW()) AS _age_seconds "
                "FROM searches WHERE query_hash = %s",
                (query_hash,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    for col in ("results_json", "domains_filter"):
        if isinstance(row.get(col), str):
            try:
                row[col] = json.loads(row[col])
            except (ValueError, TypeError):
                row[col] = None
    row["cache_age_hours"] = _age_hours_from_seconds(row.pop("_age_seconds", None))
    return row


def upsert_search(data: dict) -> None:
    """Insert or update a `searches` row keyed on `query_hash`."""
    results_json = json.dumps(data.get("results", []))
    domains_filter = (
        json.dumps(data.get("domains")) if data.get("domains") is not None else None
    )
    sql = """
        INSERT INTO searches (
            query_hash, query_text, date_from, date_to, max_results,
            domains_filter, results_json, result_count, cached_at
        ) VALUES (
            %(query_hash)s, %(query_text)s, %(date_from)s, %(date_to)s,
            %(max_results)s, %(domains_filter)s, %(results_json)s,
            %(result_count)s, NOW()
        )
        ON DUPLICATE KEY UPDATE
            query_text = VALUES(query_text),
            date_from = VALUES(date_from),
            date_to = VALUES(date_to),
            max_results = VALUES(max_results),
            domains_filter = VALUES(domains_filter),
            results_json = VALUES(results_json),
            result_count = VALUES(result_count),
            cached_at = NOW()
    """
    params = {
        "query_hash": data.get("query_hash"),
        "query_text": data.get("query_text"),
        "date_from": data.get("date_from"),
        "date_to": data.get("date_to"),
        "max_results": data.get("max_results", config.EXA_DEFAULT_RESULTS),
        "domains_filter": domains_filter,
        "results_json": results_json,
        "result_count": data.get("result_count", 0),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
