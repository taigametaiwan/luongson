from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin
from zoneinfo import ZoneInfo

TZ_VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")

EXPLICIT_ATTR_RE = re.compile(
    r'''(?is)(?:src|data-src|data-url|data-stream|data-hls|data-flv|file|source|streamUrl|stream_url)\s*[=:]\s*["']([^"']+)["']'''
)
IFRAME_RE = re.compile(r'''(?is)<iframe\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']''')
QUERY_STREAM_RE = re.compile(r'''(?i)(?:[?&]|\\u0026)(?:streamUrl|stream_url)=([^&"'<>\s]+)''')


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def decode_repeated(value: str, rounds: int = 5) -> str:
    current = html.unescape(clean_text(value)).replace("\\/", "/")
    current = current.replace("\\u0026", "&").replace("\\u003d", "=")
    for _ in range(rounds):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current.strip().strip('"\'')


def extract_explicit_references(text: str, base_url: str) -> list[dict[str, str]]:
    """Chỉ lấy URL nằm trong thuộc tính player rõ ràng; không quét danh sách JS toàn cục."""
    if not text:
        return []
    normalized = html.unescape(text).replace("\\/", "/")
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(raw: str, kind: str) -> None:
        value = decode_repeated(raw)
        if not value:
            return
        absolute = urljoin(base_url, value)
        key = (absolute, kind)
        if key in seen:
            return
        seen.add(key)
        found.append({"url": absolute, "kind": kind})

    for match in IFRAME_RE.finditer(normalized):
        add(match.group(1), "iframe")
    for match in EXPLICIT_ATTR_RE.finditer(normalized):
        raw = match.group(1)
        decoded = decode_repeated(raw)
        kind = "stream" if ".m3u8" in decoded.lower() or ".flv" in decoded.lower() or "streamurl=" in decoded.lower() else "reference"
        add(raw, kind)
        for query_match in QUERY_STREAM_RE.finditer(decoded):
            add(query_match.group(1), "stream")
    for match in QUERY_STREAM_RE.finditer(normalized):
        add(match.group(1), "stream")

    return found


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("matches", {}) if isinstance(payload, dict) else {}
    return rows if isinstance(rows, dict) else {}


def save_state(path: Path, rows: dict[str, dict[str, Any]], source: str) -> None:
    now = datetime.now(TZ_VIETNAM)
    cutoff = now - timedelta(days=2)
    cleaned: dict[str, dict[str, Any]] = {}
    for key, row in rows.items():
        if not isinstance(row, dict):
            continue
        kickoff_raw = clean_text(row.get("kickoff_iso"))
        if kickoff_raw:
            try:
                kickoff = datetime.fromisoformat(kickoff_raw)
                if kickoff.tzinfo is None:
                    kickoff = kickoff.replace(tzinfo=TZ_VIETNAM)
                if kickoff < cutoff:
                    continue
            except ValueError:
                pass
        cleaned[key] = row
    payload = {
        "version": 1,
        "source": source,
        "updated_at": now.isoformat(),
        "matches": cleaned,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_iso(value: Any) -> datetime | None:
    raw = clean_text(value)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ_VIETNAM)
    return parsed.astimezone(TZ_VIETNAM)


def should_scan_now(match: dict[str, Any], state_row: dict[str, Any] | None, now: datetime | None = None, near_minutes: int = 45) -> tuple[bool, str]:
    now = now or datetime.now(TZ_VIETNAM)
    if not state_row:
        return True, "new-match"
    delta = match.get("minutes_to_kickoff")
    if not isinstance(delta, int) or delta <= near_minutes:
        return True, "near-or-live"
    if state_row.get("has_stream") or state_row.get("has_verified"):
        return True, "cached-stream-needs-recheck"
    next_scan = parse_iso(state_row.get("next_scan_at"))
    if next_scan and now < next_scan:
        remaining = max(1, int((next_scan - now).total_seconds() // 60))
        return False, f"delta-wait-{remaining}m"
    return True, "delta-due"


def next_scan_delay_minutes(delta: int | None, has_stream: bool) -> int:
    if has_stream:
        if delta is None or delta <= 30:
            return 5
        return 10
    if delta is None:
        return 10
    if delta > 120:
        return 45
    if delta > 45:
        return 20
    if delta > 15:
        return 10
    return 5


def update_state_from_results(
    state: dict[str, dict[str, Any]],
    matches: list[dict[str, Any]],
    key_func: Any,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(TZ_VIETNAM)
    for match in matches:
        key = clean_text(key_func(match.get("url", "")))
        if not key:
            continue
        streams = match.get("streams") or []
        verified = [item for item in streams if isinstance(item, dict) and item.get("playability") == "verified"]
        selected_streams = [item for item in streams if isinstance(item, dict) and item.get("url")]
        delta = match.get("minutes_to_kickoff") if isinstance(match.get("minutes_to_kickoff"), int) else None
        delay = next_scan_delay_minutes(delta, bool(verified))
        state[key] = {
            "url": match.get("url", ""),
            "match_name": match.get("match_name") or match.get("raw_title") or "",
            "kickoff_iso": match.get("kickoff_iso") or "",
            "minutes_to_kickoff": delta,
            "last_scan_at": now.isoformat(),
            "next_scan_at": (now + timedelta(minutes=delay)).isoformat(),
            "has_stream": bool(selected_streams),
            "has_verified": bool(verified),
            "verified_urls": [item.get("url", "") for item in verified[:2]],
            "scan_decision": match.get("scan_decision") or "scanned",
        }
