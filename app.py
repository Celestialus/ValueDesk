"""Web Data Viewer — Single-file standalone desktop application.

Fetches any URL, extracts HTML tables, repeated cards/lists, JSON APIs, or
readable text (with automatic headless Playwright fallback for JS-rendered sites),
and displays results in a searchable, sortable dark-mode table.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import customtkinter as ctk
import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ======================================================================
# Storage (Saved URLs & Per-URL Cache)
# ======================================================================


@dataclass
class SavedUrl:
    """Represents a user-bookmarked URL entry."""

    name: str
    url: str
    created_at: str


class StorageManager:
    """Manages local JSON storage for saved URLs and cached page extractions."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir: Path = base_dir or (Path.home() / ".web_data_viewer")
        self.cache_dir: Path = self.base_dir / "cache"
        self.saved_urls_file: Path = self.base_dir / "saved_urls.json"

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load_saved_urls(self) -> list[SavedUrl]:
        """Load all saved favorite URLs from disk."""
        if not self.saved_urls_file.exists():
            return []
        try:
            raw = json.loads(self.saved_urls_file.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return []
            items: list[SavedUrl] = []
            for entry in raw:
                if isinstance(entry, dict) and "name" in entry and "url" in entry:
                    items.append(
                        SavedUrl(
                            name=str(entry["name"]),
                            url=str(entry["url"]),
                            created_at=str(entry.get("created_at", "")),
                        )
                    )
            return items
        except (OSError, json.JSONDecodeError):
            return []

    def _write_saved_urls(self, items: list[SavedUrl]) -> None:
        """Persist the saved URLs list to disk atomically."""
        payload = [asdict(item) for item in items]
        tmp_path = self.saved_urls_file.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(self.saved_urls_file)

    def save_url(self, name: str, url: str) -> list[SavedUrl]:
        """Add or update a favorite URL by name and return the updated list."""
        clean_name = name.strip()
        clean_url = url.strip()
        if not clean_name or not clean_url:
            raise ValueError("Both name and URL are required.")

        items = self.load_saved_urls()
        now_iso = datetime.now(timezone.utc).isoformat()

        updated = False
        for item in items:
            if item.name.lower() == clean_name.lower():
                item.name = clean_name
                item.url = clean_url
                item.created_at = now_iso
                updated = True
                break

        if not updated:
            items.append(SavedUrl(name=clean_name, url=clean_url, created_at=now_iso))

        self._write_saved_urls(items)
        return items

    def delete_url(self, name: str) -> list[SavedUrl]:
        """Remove a favorite URL by name and return the updated list."""
        items = self.load_saved_urls()
        filtered = [item for item in items if item.name.lower() != name.strip().lower()]
        self._write_saved_urls(filtered)
        return filtered

    def _cache_path_for_url(self, url: str) -> Path:
        """Return a deterministic cache file path for a given URL."""
        digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def get_cached_result(self, url: str) -> Optional[dict[str, Any]]:
        """Retrieve the last cached extraction result for a URL, if available."""
        path = self._cache_path_for_url(url)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "columns" in data and "rows" in data:
                return data
            return None
        except (OSError, json.JSONDecodeError):
            return None

    def save_cached_result(self, url: str, result: dict[str, Any]) -> None:
        """Save an extraction result to the local cache for a URL."""
        path = self._cache_path_for_url(url)
        payload = {
            **result,
            "url": url.strip(),
            "cached_at": time.time(),
            "cached_at_iso": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def clear_cache(self, url: Optional[str] = None) -> None:
        """Clear cache for a specific URL, or all cached entries if url is None."""
        if url is not None:
            path = self._cache_path_for_url(url)
            if path.exists():
                path.unlink()
            return

        for file_path in self.cache_dir.glob("*.json"):
            try:
                file_path.unlink()
            except OSError:
                pass


# ======================================================================
# Scraper (Fetching, Playwright JS/API Fallback & Structured Parsing)
# ======================================================================

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MIN_DOMAIN_DELAY_SECONDS = 1.5


class ScraperError(Exception):
    """Raised when fetching or parsing fails with a user-friendly message."""


@dataclass
class ScrapeResult:
    """Normalized output of a page fetch and extraction."""

    url: str
    title: str
    columns: list[str]
    rows: list[list[str]]
    raw_text: str
    fetch_method: str
    source_info: str
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "url": self.url,
            "title": self.title,
            "columns": self.columns,
            "rows": self.rows,
            "raw_text": self.raw_text,
            "fetch_method": self.fetch_method,
            "source_info": self.source_info,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScrapeResult":
        """Reconstruct a ScrapeResult from a cached dictionary."""
        return cls(
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            columns=[str(c) for c in data.get("columns", [])],
            rows=[[str(cell) for cell in row] for row in data.get("rows", [])],
            raw_text=str(data.get("raw_text", "")),
            fetch_method=str(data.get("fetch_method", "cache")),
            source_info=str(data.get("source_info", "Cached")),
            fetched_at=float(data.get("fetched_at", time.time())),
        )


def validate_and_normalize_url(raw_url: str) -> str:
    """Validate a user-entered URL and prepend https:// if missing a scheme."""
    url = raw_url.strip()
    if not url:
        raise ScraperError("Please enter a URL.")

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = f"https://{url}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ScraperError(f"Unsupported URL scheme '{parsed.scheme}'. Use http or https.")

    if not parsed.netloc or ("." not in parsed.netloc and not parsed.netloc.startswith("localhost")):
        raise ScraperError(f"Invalid URL: '{raw_url}'. Please include a valid domain name.")

    return url


class WebScraper:
    """Fetches web pages with polite rate limiting, Playwright fallback, and structured extraction."""

    def __init__(self, min_domain_delay: float = MIN_DOMAIN_DELAY_SECONDS) -> None:
        self.min_domain_delay = min_domain_delay
        self._domain_timestamps: dict[str, float] = {}
        self._rate_lock = threading.Lock()
        self._session = self._build_session()

    @staticmethod
    def _build_session() -> requests.Session:
        """Configure a requests Session with realistic headers and retry policy."""
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.8,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/json;q=0.8,*/*;q=0.7"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        return session

    def _respect_rate_limit(self, url: str) -> None:
        """Ensure a polite delay between consecutive requests to the same domain."""
        domain = urlparse(url).netloc.lower()
        with self._rate_lock:
            last_ts = self._domain_timestamps.get(domain)
            if last_ts is not None:
                elapsed = time.time() - last_ts
                if elapsed < self.min_domain_delay:
                    time.sleep(self.min_domain_delay - elapsed)
            self._domain_timestamps[domain] = time.time()

    def fetch(self, raw_url: str, force_playwright: bool = False) -> ScrapeResult:
        """Fetch a URL and return structured data, falling back to Playwright if needed."""
        url = validate_and_normalize_url(raw_url)
        self._respect_rate_limit(url)

        if force_playwright:
            return self._fetch_with_playwright(url)

        try:
            response = self._session.get(url, timeout=(10, 20))
        except requests.exceptions.Timeout as exc:
            raise ScraperError("Request timed out while waiting for the server.") from exc
        except requests.exceptions.ConnectionError as exc:
            raise ScraperError(
                "Could not connect to the server. Check your internet connection or the URL."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ScraperError(f"Network error: {exc}") from exc

        status = response.status_code

        if status == 429:
            raise ScraperError(
                "Rate limited by the server (HTTP 429). Please wait before trying again."
            )

        if status == 403:
            if self._looks_like_cloudflare(response.text, dict(response.headers)):
                try:
                    return self._fetch_with_playwright(url)
                except ScraperError:
                    raise ScraperError(
                        "Access blocked by Cloudflare / anti-bot protection (HTTP 403)."
                    )
            try:
                return self._fetch_with_playwright(url)
            except ScraperError:
                raise ScraperError(
                    "Access denied by the server (HTTP 403). The site may block automated requests."
                )

        if status >= 400:
            raise ScraperError(f"Server returned HTTP {status} ({response.reason}).")

        content_type = response.headers.get("Content-Type", "").lower()
        if "application/json" in content_type:
            return self._parse_json_response(url, response.text, method="requests (JSON)")

        html = response.text
        if self._looks_like_cloudflare(html, dict(response.headers)):
            return self._fetch_with_playwright(url)

        result = self._parse_html(url, html, method="requests")

        if self._needs_js_fallback(html, result):
            try:
                return self._fetch_with_playwright(url)
            except ScraperError as pw_err:
                if result.raw_text.strip():
                    result.source_info += f" (JS fallback unavailable: {pw_err})"
                    return result
                raise

        return result

    @staticmethod
    def _looks_like_cloudflare(html: str, headers: dict[str, str]) -> bool:
        """Detect common Cloudflare challenge or block signatures."""
        lower_html = html[:8000].lower()
        server = headers.get("server", "").lower() or headers.get("Server", "").lower()
        cf_markers = (
            "cf-browser-verification",
            "attention required! | cloudflare",
            "just a moment...",
            "checking your browser before accessing",
            "/cdn-cgi/challenge-platform/",
        )
        if any(marker in lower_html for marker in cf_markers):
            return True
        if "cloudflare" in server and ("enable javascript" in lower_html or "ray id" in lower_html):
            return True
        return False

    @staticmethod
    def _needs_js_fallback(html: str, result: ScrapeResult) -> bool:
        """Determine whether a page likely requires JavaScript execution to render content."""
        if result.rows and len(result.rows) >= 2:
            return False

        text_len = len(result.raw_text.strip())
        if text_len < 180:
            return True

        lower_html = html.lower()
        spa_shells = (
            '<div id="root"></div>',
            '<div id="app"></div>',
            '<div id="__next"></div>',
            "enable javascript to run this app",
            "you need to enable javascript",
        )
        if any(shell in lower_html for shell in spa_shells) and text_len < 600:
            return True

        return False

    def _fetch_with_playwright(self, url: str) -> ScrapeResult:
        """Render a page in headless Chromium and intercept JSON API responses."""
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScraperError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            ) from exc

        captured_json_payloads: list[tuple[str, Any]] = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=DEFAULT_USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                    locale="en-US",
                )
                page = context.new_page()

                def on_response(resp: Any) -> None:
                    try:
                        ctype = (resp.headers or {}).get("content-type", "").lower()
                        if "application/json" in ctype and resp.status == 200:
                            data = resp.json()
                            captured_json_payloads.append((resp.url, data))
                    except Exception:
                        pass

                page.on("response", on_response)

                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=25000)
                except PlaywrightError as exc:
                    browser.close()
                    raise ScraperError(f"Headless browser failed to load page: {exc}") from exc

                if resp is not None and resp.status == 429:
                    browser.close()
                    raise ScraperError("Rate limited by server (HTTP 429).")

                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightError:
                    pass
                page.wait_for_timeout(1500)

                html = page.content()
                page_title = page.title() or url
                browser.close()
        except ScraperError:
            raise
        except Exception as exc:
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                raise ScraperError(
                    "Playwright Chromium browser is missing. Run: playwright install chromium"
                ) from exc
            raise ScraperError(f"Headless browser error: {msg}") from exc

        if self._looks_like_cloudflare(html, {}):
            raise ScraperError(
                "Blocked by Cloudflare protection even in headless browser mode."
            )

        best_api = self._pick_best_json_payload(captured_json_payloads)
        if best_api is not None:
            api_url, api_data = best_api
            parsed_api = self._json_to_table(api_data)
            if parsed_api is not None:
                columns, rows = parsed_api
                raw_text = self._extract_clean_text(BeautifulSoup(html, "lxml"))
                return ScrapeResult(
                    url=url,
                    title=page_title,
                    columns=columns,
                    rows=rows,
                    raw_text=raw_text,
                    fetch_method="playwright + API",
                    source_info=f"Intercepted JSON API ({len(rows)} rows) from {api_url}",
                )

        return self._parse_html(url, html, method="playwright", fallback_title=page_title)

    def _parse_json_response(self, url: str, text: str, method: str) -> ScrapeResult:
        """Parse a direct JSON endpoint response into a ScrapeResult."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScraperError(f"Invalid JSON response: {exc}") from exc

        table = self._json_to_table(data)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        if table is not None:
            columns, rows = table
            return ScrapeResult(
                url=url,
                title=url,
                columns=columns,
                rows=rows,
                raw_text=pretty,
                fetch_method=method,
                source_info=f"JSON Endpoint ({len(rows)} rows)",
            )

        return ScrapeResult(
            url=url,
            title=url,
            columns=["Content"],
            rows=[[line] for line in pretty.splitlines() if line.strip()],
            raw_text=pretty,
            fetch_method=method,
            source_info="JSON (raw text)",
        )

    @classmethod
    def _pick_best_json_payload(
        cls, payloads: list[tuple[str, Any]]
    ) -> Optional[tuple[str, Any]]:
        """Select the richest intercepted JSON payload containing tabular/list records."""
        best_score = 0
        best_item: Optional[tuple[str, Any]] = None

        for api_url, data in payloads:
            lower_u = api_url.lower()
            if any(skip in lower_u for skip in ("analytics", "sentry", "gtag", "metrics", "telemetry")):
                continue
            table = cls._json_to_table(data)
            if table is None:
                continue
            cols, rows = table
            score = len(rows) * len(cols)
            if len(rows) >= 3 and len(cols) >= 2 and score > best_score:
                best_score = score
                best_item = (api_url, data)

        return best_item

    @classmethod
    def _find_record_list_in_json(cls, data: Any, depth: int = 0) -> Optional[list[dict[str, Any]]]:
        """Recursively locate the largest list of dictionaries inside a JSON structure."""
        if depth > 4:
            return None

        if isinstance(data, list):
            dict_items = [x for x in data if isinstance(x, dict)]
            if len(dict_items) >= 2:
                return dict_items
            return None

        if isinstance(data, dict):
            best: Optional[list[dict[str, Any]]] = None
            for value in data.values():
                candidate = cls._find_record_list_in_json(value, depth + 1)
                if candidate is not None:
                    if best is None or len(candidate) > len(best):
                        best = candidate
            return best

        return None

    @classmethod
    def _json_to_table(cls, data: Any) -> Optional[tuple[list[str], list[list[str]]]]:
        """Convert a JSON list of objects into (columns, rows)."""
        records = cls._find_record_list_in_json(data)
        if not records:
            return None

        columns: list[str] = []
        seen: set[str] = set()
        for rec in records[:200]:
            for key, val in rec.items():
                if isinstance(val, (dict, list)) and len(str(val)) > 120:
                    continue
                key_str = str(key)
                if key_str not in seen:
                    seen.add(key_str)
                    columns.append(key_str)

        if not columns:
            return None

        rows: list[list[str]] = []
        for rec in records:
            row: list[str] = []
            for col in columns:
                val = rec.get(col, "")
                if val is None:
                    row.append("")
                elif isinstance(val, (dict, list)):
                    row.append(json.dumps(val, ensure_ascii=False))
                else:
                    row.append(str(val))
            rows.append(row)

        return columns, rows

    def _parse_html(
        self,
        url: str,
        html: str,
        method: str,
        fallback_title: Optional[str] = None,
    ) -> ScrapeResult:
        """Extract tables, repeated cards/lists, or readable text from HTML."""
        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        title = (
            title_tag.get_text(strip=True)
            if title_tag and title_tag.get_text(strip=True)
            else (fallback_title or url)
        )
        raw_text = self._extract_clean_text(soup)

        table_result = self._extract_best_html_table(html)
        if table_result is not None:
            columns, rows = table_result
            return ScrapeResult(
                url=url,
                title=title,
                columns=columns,
                rows=rows,
                raw_text=raw_text,
                fetch_method=method,
                source_info=f"HTML Table ({len(rows)} rows)",
            )

        card_result = self._extract_repeated_cards(soup, base_url=url)
        if card_result is not None:
            columns, rows = card_result
            return ScrapeResult(
                url=url,
                title=title,
                columns=columns,
                rows=rows,
                raw_text=raw_text,
                fetch_method=method,
                source_info=f"Repeated Cards/List ({len(rows)} items)",
            )

        next_data_script = soup.find("script", id="__NEXT_DATA__")
        if next_data_script and next_data_script.string:
            try:
                next_json = json.loads(next_data_script.string)
                next_table = self._json_to_table(next_json)
                if next_table is not None and len(next_table[1]) >= 3:
                    columns, rows = next_table
                    return ScrapeResult(
                        url=url,
                        title=title,
                        columns=columns,
                        rows=rows,
                        raw_text=raw_text,
                        fetch_method=f"{method} (__NEXT_DATA__)",
                        source_info=f"Embedded Page JSON ({len(rows)} rows)",
                    )
            except json.JSONDecodeError:
                pass

        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        return ScrapeResult(
            url=url,
            title=title,
            columns=["Line #", "Text"],
            rows=[[str(idx), line] for idx, line in enumerate(lines, start=1)],
            raw_text=raw_text,
            fetch_method=method,
            source_info=f"Readable Text ({len(lines)} lines)",
        )

    @staticmethod
    def _extract_best_html_table(html: str) -> Optional[tuple[list[str], list[list[str]]]]:
        """Parse HTML tables with pandas.read_html and return the best structured table."""
        try:
            tables = pd.read_html(io.StringIO(html), flavor="lxml")
        except ValueError:
            return None
        except Exception:
            return None

        best_df: Optional[pd.DataFrame] = None
        best_score = 0

        for df in tables:
            if df.empty or df.shape[0] < 2 or df.shape[1] < 2:
                continue
            score = df.shape[0] * df.shape[1]
            if score > best_score:
                best_score = score
                best_df = df

        if best_df is None:
            return None

        if isinstance(best_df.columns, pd.MultiIndex):
            cols = [
                " - ".join(str(part).strip() for part in col if str(part) != "nan").strip()
                for col in best_df.columns
            ]
        else:
            cols = [str(c).strip() for c in best_df.columns]

        seen_counts: dict[str, int] = {}
        clean_cols: list[str] = []
        for idx, col in enumerate(cols, start=1):
            base = col if col and not col.startswith("Unnamed:") else f"Column {idx}"
            count = seen_counts.get(base, 0) + 1
            seen_counts[base] = count
            clean_cols.append(base if count == 1 else f"{base} ({count})")

        clean_df = best_df.fillna("")
        rows = [[str(cell).strip() for cell in row] for row in clean_df.values.tolist()]
        return clean_cols, rows

    @classmethod
    def _extract_repeated_cards(
        cls, soup: BeautifulSoup, base_url: str
    ) -> Optional[tuple[list[str], list[list[str]]]]:
        """Detect repeated card/list item containers and extract structured fields."""
        body = soup.body or soup
        best_records: list[dict[str, str]] = []
        best_score = 0

        for container in body.find_all(["div", "ul", "ol", "section", "main"]):
            if container.find_parent(["nav", "header", "footer"]):
                continue

            children = [
                c
                for c in container.find_all(recursive=False)
                if isinstance(c, Tag) and c.name in ("div", "li", "article", "a")
            ]
            if len(children) < 3:
                continue

            signatures = [
                (child.name, tuple(sorted(child.get("class") or []))) for child in children
            ]
            most_common_sig, sig_count = Counter(signatures).most_common(1)[0]
            if sig_count < 3:
                continue

            matched_cards = [
                child
                for child, sig in zip(children, signatures)
                if sig == most_common_sig
            ]

            records: list[dict[str, str]] = []
            for card in matched_cards:
                rec = cls._parse_single_card(card, base_url=base_url)
                if rec and len(rec) >= 2:
                    records.append(rec)

            if len(records) < 3:
                continue

            avg_fields = sum(len(r) for r in records) / len(records)
            score = int(len(records) * avg_fields)
            if avg_fields >= 2.0 and score > best_score:
                best_score = score
                best_records = records

        if not best_records:
            return None

        columns: list[str] = []
        seen: set[str] = set()
        for rec in best_records:
            for key in rec:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)

        rows = [[rec.get(col, "") for col in columns] for rec in best_records]
        return columns, rows

    @staticmethod
    def _parse_single_card(card: Tag, base_url: str) -> dict[str, str]:
        """Extract name, labeled metrics, text fields, and image URL from a single card element."""
        record: dict[str, str] = {}

        title_el = card.find(["h1", "h2", "h3", "h4", "h5", "h6", "strong"])
        if title_el and title_el.get_text(strip=True):
            record["Name"] = title_el.get_text(" ", strip=True)

        raw_chunks = [chunk.strip() for chunk in card.stripped_strings if chunk.strip()]
        chunks: list[str] = []
        for ch in raw_chunks:
            if not chunks or chunks[-1] != ch:
                chunks.append(ch)

        if "Name" not in record and chunks:
            record["Name"] = chunks[0]
            remaining_chunks = chunks[1:]
        else:
            name_val = record.get("Name", "")
            remaining_chunks = [c for c in chunks if c != name_val]

        field_idx = 1
        i = 0
        known_labels = {
            "value",
            "demand",
            "trend",
            "price",
            "type",
            "rarity",
            "category",
            "status",
            "belis",
            "robux",
            "stock",
            "change",
        }
        while i < len(remaining_chunks):
            chunk = remaining_chunks[i]
            if ":" in chunk:
                left, right = chunk.split(":", 1)
                left_clean = left.strip()
                right_clean = right.strip()
                if left_clean and right_clean and len(left_clean) <= 30:
                    record[left_clean] = right_clean
                    i += 1
                    continue

            if (
                chunk.rstrip(":").lower() in known_labels
                and i + 1 < len(remaining_chunks)
            ):
                label = chunk.rstrip(":").strip().title()
                record[label] = remaining_chunks[i + 1]
                i += 2
                continue

            record[f"Field {field_idx}"] = chunk
            field_idx += 1
            i += 1

        img = card.find("img")
        if img:
            src = img.get("src") or img.get("data-src") or ""
            if src:
                record["Image URL"] = urljoin(base_url, str(src))

        if card.name == "a" and card.get("href"):
            record["Link"] = urljoin(base_url, str(card["href"]))
        else:
            link_tag = card.find("a", href=True)
            if link_tag and link_tag.get("href"):
                record["Link"] = urljoin(base_url, str(link_tag["href"]))

        return record

    @staticmethod
    def _extract_clean_text(soup: BeautifulSoup) -> str:
        """Extract readable plain text from a BeautifulSoup document."""
        clone = BeautifulSoup(str(soup), "lxml")
        for tag in clone(["head", "script", "style", "noscript", "svg", "iframe"]):
            tag.decompose()
        text = clone.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines()]
        cleaned_lines: list[str] = []
        blank_streak = 0
        for line in lines:
            if line:
                cleaned_lines.append(line)
                blank_streak = 0
            elif blank_streak == 0:
                cleaned_lines.append("")
                blank_streak += 1
        return "\n".join(cleaned_lines).strip()


# ======================================================================
# GUI (CustomTkinter Dark Mode)
# ======================================================================

MIN_REFRESH_MINUTES = 1  # 60 seconds minimum to avoid hammering servers


def parse_numeric_value(text: str) -> Optional[float]:
    """Parse human-readable numbers (including $, commas, %, K/M/B/T suffixes) for sorting & diffs."""
    if not text:
        return None
    cleaned = text.strip().replace(",", "").replace("$", "").replace("%", "")
    cleaned = cleaned.lstrip("▲▼").strip()
    match = re.match(r"^(-?\d+(?:\.\d+)?)\s*([kmbtKMBT])?$", cleaned)
    if not match:
        return None
    num = float(match.group(1))
    suffix = (match.group(2) or "").lower()
    multipliers = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}
    return num * multipliers.get(suffix, 1.0)


class WebDataViewerApp(ctk.CTk):
    """Main application window."""

    def __init__(self, storage: StorageManager, scraper: WebScraper) -> None:
        super().__init__()

        self.storage = storage
        self.scraper = scraper

        self.title("Web Data Viewer")
        self.geometry("1180x760")
        self.minsize(900, 580)

        self._saved_urls: list[SavedUrl] = []
        self._current_result: Optional[ScrapeResult] = None
        self._previous_rows_by_key: dict[str, list[str]] = {}
        self._row_diff_tags: list[str] = []
        self._sort_col_index: Optional[int] = None
        self._sort_descending: bool = False
        self._is_fetching: bool = False
        self._auto_refresh_job: Optional[str] = None

        self._configure_treeview_style()
        self._build_layout()
        self._load_favorites_into_dropdown()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_treeview_style(self) -> None:
        """Configure ttk.Treeview to match CustomTkinter's dark mode."""
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "Dark.Treeview",
            background="#1e1f26",
            foreground="#e4e6eb",
            fieldbackground="#1e1f26",
            rowheight=30,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", "#1f538d")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background="#2b2d38",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
            padding=(8, 6),
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", "#3a3d4d")],
        )

    def _build_layout(self) -> None:
        """Construct all UI controls."""
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Row 0: URL Input + Fetch + Saved URLs
        top_frame = ctk.CTkFrame(self, corner_radius=8)
        top_frame.grid(row=0, column=0, padx=14, pady=(14, 6), sticky="ew")
        top_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top_frame, text="URL:", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=(12, 6), pady=10
        )

        self.url_entry = ctk.CTkEntry(
            top_frame,
            placeholder_text="Paste any URL (e.g. https://bloxfruitsvalues.com)...",
            height=36,
        )
        self.url_entry.grid(row=0, column=1, padx=6, pady=10, sticky="ew")
        self.url_entry.bind("<Return>", lambda _e: self.start_fetch())

        self.fetch_btn = ctk.CTkButton(
            top_frame, text="Fetch", width=100, height=36, command=self.start_fetch
        )
        self.fetch_btn.grid(row=0, column=2, padx=(6, 12), pady=10)

        ctk.CTkLabel(top_frame, text="Saved:").grid(row=1, column=0, padx=(12, 6), pady=(0, 10))

        self.favorites_var = ctk.StringVar(value="Select a saved URL...")
        self.favorites_menu = ctk.CTkOptionMenu(
            top_frame,
            variable=self.favorites_var,
            values=["No saved URLs"],
            command=self._on_favorite_selected,
            width=260,
        )
        self.favorites_menu.grid(row=1, column=1, padx=6, pady=(0, 10), sticky="w")

        fav_btn_frame = ctk.CTkFrame(top_frame, fg_color="transparent")
        fav_btn_frame.grid(row=1, column=2, padx=(6, 12), pady=(0, 10), sticky="e")

        self.save_fav_btn = ctk.CTkButton(
            fav_btn_frame,
            text="Save URL",
            width=85,
            fg_color="#2e7d32",
            hover_color="#1b5e20",
            command=self._save_current_favorite,
        )
        self.save_fav_btn.pack(side="left", padx=(0, 6))

        self.del_fav_btn = ctk.CTkButton(
            fav_btn_frame,
            text="Delete",
            width=70,
            fg_color="#8e2424",
            hover_color="#641616",
            command=self._delete_selected_favorite,
        )
        self.del_fav_btn.pack(side="left")

        # Row 1: Search, Auto-Refresh, Export Controls
        toolbar = ctk.CTkFrame(self, corner_radius=8)
        toolbar.grid(row=1, column=0, padx=14, pady=6, sticky="ew")
        toolbar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(toolbar, text="Filter:").grid(row=0, column=0, padx=(12, 6), pady=8)
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._render_table())
        self.search_entry = ctk.CTkEntry(
            toolbar,
            textvariable=self.search_var,
            placeholder_text="Search rows...",
            width=240,
        )
        self.search_entry.grid(row=0, column=1, padx=6, pady=8, sticky="ew")

        refresh_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        refresh_box.grid(row=0, column=2, padx=10, pady=8)

        self.auto_refresh_var = ctk.BooleanVar(value=False)
        self.auto_refresh_chk = ctk.CTkCheckBox(
            refresh_box,
            text="Auto-refresh every",
            variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh,
        )
        self.auto_refresh_chk.pack(side="left", padx=(0, 6))

        self.interval_var = ctk.StringVar(value="5")
        self.interval_entry = ctk.CTkEntry(
            refresh_box, textvariable=self.interval_var, width=48, justify="center"
        )
        self.interval_entry.pack(side="left")
        ctk.CTkLabel(refresh_box, text="min").pack(side="left", padx=(4, 0))

        export_box = ctk.CTkFrame(toolbar, fg_color="transparent")
        export_box.grid(row=0, column=3, padx=(6, 12), pady=8)

        self.export_csv_btn = ctk.CTkButton(
            export_box, text="Export CSV", width=95, command=self._export_csv
        )
        self.export_csv_btn.pack(side="left", padx=(0, 6))

        self.export_json_btn = ctk.CTkButton(
            export_box, text="Export JSON", width=95, command=self._export_json
        )
        self.export_json_btn.pack(side="left")

        # Row 2: Tabbed Content (Data Table / Raw Text)
        self.tabview = ctk.CTkTabview(self, corner_radius=8)
        self.tabview.grid(row=2, column=0, padx=14, pady=6, sticky="nsew")

        self.table_tab = self.tabview.add("Data Table")
        self.raw_tab = self.tabview.add("Raw Text")

        self.table_tab.grid_rowconfigure(0, weight=1)
        self.table_tab.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            self.table_tab, style="Dark.Treeview", show="headings", selectmode="browse"
        )
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(self.table_tab, orient="vertical", command=self.tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(self.table_tab, orient="horizontal", command=self.tree.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.tag_configure("increased", background="#1b4332", foreground="#b7e4c7")
        self.tree.tag_configure("decreased", background="#4a151b", foreground="#ffb3c1")
        self.tree.tag_configure("even", background="#1e1f26")
        self.tree.tag_configure("odd", background="#252731")

        self.raw_tab.grid_rowconfigure(0, weight=1)
        self.raw_tab.grid_columnconfigure(0, weight=1)
        self.raw_textbox = ctk.CTkTextbox(
            self.raw_tab, wrap="word", font=("Consolas", 12)
        )
        self.raw_textbox.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.raw_textbox.configure(state="disabled")

        # Row 3: Status Bar & Progress Indicator
        status_frame = ctk.CTkFrame(self, height=34, corner_radius=6)
        status_frame.grid(row=3, column=0, padx=14, pady=(4, 12), sticky="ew")
        status_frame.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            status_frame,
            text="Ready. Paste a URL and click Fetch.",
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, padx=12, pady=4, sticky="ew")

        self.progress_bar = ctk.CTkProgressBar(status_frame, mode="indeterminate", width=160)
        self.progress_bar.grid(row=0, column=1, padx=12, pady=4)
        self.progress_bar.grid_remove()

    def _load_favorites_into_dropdown(self) -> None:
        self._saved_urls = self.storage.load_saved_urls()
        if self._saved_urls:
            names = [item.name for item in self._saved_urls]
            self.favorites_menu.configure(values=names)
            self.favorites_var.set("Select a saved URL...")
        else:
            self.favorites_menu.configure(values=["No saved URLs"])
            self.favorites_var.set("No saved URLs")

    def _on_favorite_selected(self, selected_name: str) -> None:
        for item in self._saved_urls:
            if item.name == selected_name:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, item.url)
                self.start_fetch()
                return

    def _save_current_favorite(self) -> None:
        raw_url = self.url_entry.get().strip()
        try:
            normalized = validate_and_normalize_url(raw_url)
        except ScraperError as exc:
            messagebox.showwarning("Invalid URL", str(exc))
            return

        dialog = ctk.CTkInputDialog(
            text="Enter a name for this saved URL:", title="Save Favorite URL"
        )
        name = dialog.get_input()
        if not name or not name.strip():
            return

        self.storage.save_url(name.strip(), normalized)
        self._load_favorites_into_dropdown()
        self.favorites_var.set(name.strip())
        self._set_status(f"Saved '{name.strip()}' to favorites.")

    def _delete_selected_favorite(self) -> None:
        selected = self.favorites_var.get()
        if not self._saved_urls or selected in ("Select a saved URL...", "No saved URLs"):
            return
        self.storage.delete_url(selected)
        self._load_favorites_into_dropdown()
        self._set_status(f"Removed '{selected}' from favorites.")

    def start_fetch(self) -> None:
        """Validate URL, show cached data immediately if available, and fetch in background."""
        if self._is_fetching:
            return

        raw_url = self.url_entry.get().strip()
        try:
            url = validate_and_normalize_url(raw_url)
        except ScraperError as exc:
            self._set_status(f"Error: {exc}")
            messagebox.showerror("Invalid URL", str(exc))
            return

        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)

        if self._current_result and self._current_result.url == url:
            self._snapshot_previous_rows(self._current_result)
        else:
            self._previous_rows_by_key = {}
            cached = self.storage.get_cached_result(url)
            if cached:
                cached_result = ScrapeResult.from_dict(cached)
                self._snapshot_previous_rows(cached_result)
                self._apply_result(cached_result, is_cached=True)

        self._is_fetching = True
        self.fetch_btn.configure(state="disabled", text="Fetching...")
        self.progress_bar.grid()
        self.progress_bar.start()

        if self._current_result and self._current_result.url == url:
            ts = datetime.fromtimestamp(self._current_result.fetched_at).strftime("%H:%M:%S")
            self._set_status(f"Showing cached data ({ts}) — refreshing {url}...")
        else:
            self._set_status(f"Fetching {url}...")

        worker = threading.Thread(target=self._fetch_worker, args=(url,), daemon=True)
        worker.start()

    def _fetch_worker(self, url: str) -> None:
        """Background thread worker for network fetching and parsing."""
        try:
            result = self.scraper.fetch(url)
            self.storage.save_cached_result(url, result.to_dict())
            self.after(0, lambda: self._on_fetch_success(result))
        except ScraperError as exc:
            self.after(0, lambda msg=str(exc): self._on_fetch_error(msg))
        except Exception as exc:
            self.after(0, lambda msg=f"Unexpected error: {exc}": self._on_fetch_error(msg))

    def _on_fetch_success(self, result: ScrapeResult) -> None:
        self._stop_loading_ui()
        self._apply_result(result, is_cached=False)

    def _on_fetch_error(self, message: str) -> None:
        self._stop_loading_ui()
        self._set_status(f"Fetch failed: {message}")
        if not self.auto_refresh_var.get():
            messagebox.showerror("Fetch Error", message)

    def _stop_loading_ui(self) -> None:
        self._is_fetching = False
        self.fetch_btn.configure(state="normal", text="Fetch")
        self.progress_bar.stop()
        self.progress_bar.grid_remove()

    def _snapshot_previous_rows(self, result: ScrapeResult) -> None:
        """Index previous rows by their first column (item name) for change detection."""
        snapshot: dict[str, list[str]] = {}
        for idx, row in enumerate(result.rows):
            key = row[0].strip().lower() if row and row[0].strip() else f"__row_{idx}"
            snapshot[key] = row
        self._previous_rows_by_key = snapshot

    def _compute_row_diffs(self, result: ScrapeResult) -> tuple[list[str], int, int]:
        """Compare new rows against previous snapshot and return (tags, inc_count, dec_count)."""
        tags: list[str] = []
        inc_count = 0
        dec_count = 0

        if not self._previous_rows_by_key:
            return ["normal"] * len(result.rows), 0, 0

        for idx, row in enumerate(result.rows):
            key = row[0].strip().lower() if row and row[0].strip() else f"__row_{idx}"
            prev_row = self._previous_rows_by_key.get(key)
            if not prev_row:
                tags.append("normal")
                continue

            row_tag = "normal"
            for col_i in range(min(len(row), len(prev_row))):
                new_num = parse_numeric_value(row[col_i])
                old_num = parse_numeric_value(prev_row[col_i])
                if new_num is not None and old_num is not None and new_num != old_num:
                    if new_num > old_num:
                        row_tag = "increased"
                        inc_count += 1
                    else:
                        row_tag = "decreased"
                        dec_count += 1
                    break
            tags.append(row_tag)

        return tags, inc_count, dec_count

    def _apply_result(self, result: ScrapeResult, is_cached: bool) -> None:
        """Populate both the Data Table and Raw Text views."""
        if not is_cached:
            tags, inc_count, dec_count = self._compute_row_diffs(result)
        else:
            tags, inc_count, dec_count = ["normal"] * len(result.rows), 0, 0

        self._current_result = result
        self._row_diff_tags = tags
        self._sort_col_index = None
        self._sort_descending = False

        self._configure_tree_columns(result.columns)
        self._render_table()
        self._render_raw_text(result)

        if not is_cached:
            ts = datetime.fromtimestamp(result.fetched_at).strftime("%H:%M:%S")
            diff_note = ""
            if inc_count or dec_count:
                diff_note = f" | Changes: +{inc_count} increased, -{dec_count} decreased"
            self._set_status(
                f"Loaded via {result.fetch_method} — {result.source_info} at {ts}{diff_note}"
            )

    def _configure_tree_columns(self, columns: list[str]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = columns
        for idx, col in enumerate(columns):
            self.tree.heading(
                col,
                text=col,
                command=lambda c_idx=idx: self._sort_by_column(c_idx),
            )
            width = 160 if idx == 0 else 125
            self.tree.column(col, width=width, minwidth=75, stretch=True)

    def _render_table(self) -> None:
        """Filter, sort, and render rows into the Treeview."""
        if not self._current_result:
            return

        query = self.search_var.get().strip().lower()
        columns = self._current_result.columns
        indexed_rows = list(zip(self._current_result.rows, self._row_diff_tags))

        if query:
            indexed_rows = [
                (row, tag)
                for row, tag in indexed_rows
                if any(query in cell.lower() for cell in row)
            ]

        if self._sort_col_index is not None and self._sort_col_index < len(columns):
            c_idx = self._sort_col_index

            def sort_key(pair: tuple[list[str], str]) -> tuple[int, float, str]:
                row = pair[0]
                val = row[c_idx] if c_idx < len(row) else ""
                num = parse_numeric_value(val)
                if num is not None:
                    return (0, num, "")
                return (1, 0.0, val.lower())

            indexed_rows.sort(key=sort_key, reverse=self._sort_descending)

        for idx, col in enumerate(columns):
            arrow = ""
            if idx == self._sort_col_index:
                arrow = " ▼" if self._sort_descending else " ▲"
            self.tree.heading(col, text=f"{col}{arrow}")

        self.tree.delete(*self.tree.get_children())
        for display_idx, (row, diff_tag) in enumerate(indexed_rows):
            if diff_tag in ("increased", "decreased"):
                tag = diff_tag
            else:
                tag = "even" if display_idx % 2 == 0 else "odd"
            padded = row + [""] * max(0, len(columns) - len(row))
            self.tree.insert("", "end", values=padded[: len(columns)], tags=(tag,))

    def _sort_by_column(self, col_index: int) -> None:
        if self._sort_col_index == col_index:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_col_index = col_index
            self._sort_descending = False
        self._render_table()

    def _render_raw_text(self, result: ScrapeResult) -> None:
        self.raw_textbox.configure(state="normal")
        self.raw_textbox.delete("1.0", "end")
        header = f"Title: {result.title}\nURL: {result.url}\nSource: {result.source_info}\n{'=' * 60}\n\n"
        self.raw_textbox.insert("1.0", header + result.raw_text)
        self.raw_textbox.configure(state="disabled")

    def _toggle_auto_refresh(self) -> None:
        if self._auto_refresh_job is not None:
            self.after_cancel(self._auto_refresh_job)
            self._auto_refresh_job = None

        if self.auto_refresh_var.get():
            self._schedule_next_refresh()
        else:
            self._set_status("Auto-refresh disabled.")

    def _schedule_next_refresh(self) -> None:
        if not self.auto_refresh_var.get():
            return
        try:
            minutes = float(self.interval_var.get().strip())
        except ValueError:
            minutes = 5.0

        if minutes < MIN_REFRESH_MINUTES:
            minutes = float(MIN_REFRESH_MINUTES)
            self.interval_var.set(str(MIN_REFRESH_MINUTES))
            self._set_status(f"Minimum refresh interval is {MIN_REFRESH_MINUTES} minute (60s).")

        delay_ms = int(minutes * 60 * 1000)
        self._auto_refresh_job = self.after(delay_ms, self._run_auto_refresh)

    def _run_auto_refresh(self) -> None:
        self._auto_refresh_job = None
        if not self.auto_refresh_var.get():
            return
        if self.url_entry.get().strip() and not self._is_fetching:
            self.start_fetch()
        self._schedule_next_refresh()

    def _get_visible_rows(self) -> tuple[list[str], list[list[str]]]:
        """Return the currently filtered/sorted rows shown in the Treeview."""
        if not self._current_result:
            return [], []
        cols = self._current_result.columns
        rows = [list(self.tree.item(iid, "values")) for iid in self.tree.get_children()]
        return cols, rows

    def _export_csv(self) -> None:
        cols, rows = self._get_visible_rows()
        if not cols or not rows:
            messagebox.showinfo("Nothing to Export", "Fetch a page with data first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Export Table to CSV",
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            writer.writerows(rows)
        self._set_status(f"Exported {len(rows)} rows to CSV.")

    def _export_json(self) -> None:
        cols, rows = self._get_visible_rows()
        if not cols or not rows:
            messagebox.showinfo("Nothing to Export", "Fetch a page with data first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Export Table to JSON",
        )
        if not path:
            return

        records = [dict(zip(cols, row)) for row in rows]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2, ensure_ascii=False)
        self._set_status(f"Exported {len(records)} rows to JSON.")

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _on_close(self) -> None:
        if self._auto_refresh_job is not None:
            self.after_cancel(self._auto_refresh_job)
        self.destroy()


# ======================================================================
# Entry Point
# ======================================================================


def main() -> None:
    """Initialize theme, storage, scraper, and launch the GUI event loop."""
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    storage = StorageManager()
    scraper = WebScraper()
    app = WebDataViewerApp(storage=storage, scraper=scraper)
    app.mainloop()


if __name__ == "__main__":
    main()