import asyncio
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Route, async_playwright

try:
    from .hybrid_support import (
        extract_explicit_references,
        load_state as load_delta_state,
        save_state as save_delta_state,
        should_scan_now,
        update_state_from_results,
    )
except ImportError:  # chạy trực tiếp: python sources/<scanner>.py
    from hybrid_support import (
        extract_explicit_references,
        load_state as load_delta_state,
        save_state as save_delta_state,
        should_scan_now,
        update_state_from_results,
    )


# =========================
# CẤU HÌNH
# =========================
DEFAULT_HOME_URLS = (
    "https://catbee.io/",
    "https://hygenie.io/",
)
TARGET_URL = DEFAULT_HOME_URLS[0]
PLAYER_ORIGIN_FALLBACK = TARGET_URL.rstrip("/")
OUTPUT_M3U = "hygenie_live.m3u"
OUTPUT_PIPE_M3U = "hygenie_live_pipe.m3u"
OUTPUT_VLC_M3U = "hygenie_live_vlc.m3u"
OUTPUT_DEBUG = "hygenie_debug.json"
OUTPUT_HOME_DEBUG_HTML = "hygenie_home_debug.html"
OUTPUT_HOME_DEBUG_PNG = "hygenie_home_debug.png"
SCANNER_VERSION = "4.3.0-LUONGSON-HYBRID-DELTA"


def read_env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    print(f"⚠️ {name}={raw!r} không hợp lệ; dùng mặc định {default}.")
    return default


def read_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"⚠️ {name}={raw!r} không hợp lệ; dùng mặc định {default}.")
        return default
    return max(minimum, min(value, maximum))


def read_env_urls(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(defaults)
    normalized: list[str] = []
    for value in values:
        if not value.startswith(("http://", "https://")):
            continue
        fixed = value.rstrip("/") + "/"
        if fixed not in normalized:
            normalized.append(fixed)
    return tuple(normalized or defaults)


HOME_URLS = read_env_urls("HYGENIE_HOME_URLS", DEFAULT_HOME_URLS)
TARGET_URL = HOME_URLS[0]
PLAYER_ORIGIN_FALLBACK = TARGET_URL.rstrip("/")


CONCURRENCY_LIMIT = read_env_int(
    "HYGENIE_MATCH_CONCURRENCY", 4, minimum=1, maximum=12
)
HOME_WAIT_MS = read_env_int(
    "HYGENIE_HOME_WAIT_MS", 6000, minimum=1000, maximum=30000
)
STREAM_WAIT_SECONDS = read_env_int(
    "HYGENIE_ROOM_WAIT_SECONDS", 20, minimum=5, maximum=120
)
EXTRA_WAIT_AFTER_FIRST_STREAM = 5.0
FULL_SCAN = read_env_bool("HYGENIE_FULL_SCAN", True)
VERIFY_STREAMS = read_env_bool("HYGENIE_VERIFY_STREAMS", True)
VERIFY_TIMEOUT_SECONDS = read_env_int("HYGENIE_VERIFY_TIMEOUT_SECONDS", 8, minimum=3, maximum=20)
MAX_VERIFY_CANDIDATES = read_env_int("HYGENIE_MAX_VERIFY_CANDIDATES", 6, minimum=2, maximum=12)
MAX_OUTPUT_STREAMS_PER_MATCH = read_env_int("HYGENIE_MAX_OUTPUT_STREAMS_PER_MATCH", 2, minimum=1, maximum=4)
SCAN_PAST_MINUTES = read_env_int("HYGENIE_SCAN_PAST_MINUTES", 150, minimum=0, maximum=1440)
SCAN_FUTURE_MINUTES = read_env_int("HYGENIE_SCAN_FUTURE_MINUTES", 240, minimum=0, maximum=1440)
SCAN_UNKNOWN_LIVE = read_env_bool("HYGENIE_SCAN_UNKNOWN_LIVE", True)
UPCOMING_FAR_THRESHOLD_MINUTES = read_env_int("HYGENIE_UPCOMING_FAR_THRESHOLD_MINUTES", 45, minimum=5, maximum=240)
UPCOMING_FAR_WAIT_SECONDS = read_env_int("HYGENIE_UPCOMING_FAR_WAIT_SECONDS", 7, minimum=3, maximum=30)
UPCOMING_NEAR_WAIT_SECONDS = read_env_int("HYGENIE_UPCOMING_NEAR_WAIT_SECONDS", 12, minimum=5, maximum=60)
HYBRID_HTTP_FIRST = read_env_bool("HYGENIE_HYBRID_HTTP_FIRST", True)
HTTP_DISCOVERY_TIMEOUT_SECONDS = read_env_int("HYGENIE_HTTP_DISCOVERY_TIMEOUT_SECONDS", 8, minimum=3, maximum=20)
HTTP_DISCOVERY_MAX_FOLLOWS = read_env_int("HYGENIE_HTTP_DISCOVERY_MAX_FOLLOWS", 4, minimum=1, maximum=10)
DELTA_SCAN_ENABLED = read_env_bool("HYGENIE_DELTA_SCAN_ENABLED", True)
DELTA_NEAR_MINUTES = read_env_int("HYGENIE_DELTA_NEAR_MINUTES", 45, minimum=5, maximum=180)
STATE_PATH = Path(os.getenv("HYGENIE_STATE_PATH", "hygenie_state.json"))
HEADLESS = True
PROBE_CACHE: dict[tuple[str, str, str, str], dict[str, Any]] = {}

# Dùng đúng User-Agent đã được kiểm chứng phát được bằng VLC.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

STREAM_EXTENSIONS = (".m3u8", ".flv")
AD_MARKERS = (
    "doubleclick.",
    "googleads.",
    "/ads/",
    "/advert",
    "imasdk",
)

PLAY_SELECTORS = (
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".play-button",
    ".btn-play",
    "button[aria-label*='Play' i]",
    "button[title*='Play' i]",
    "[class*='play'][role='button']",
)

TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:h.]([0-5]\d)(?!\d)", re.I)

BLV_ALIASES = {
    "lubo": "Lữ Bố",
    "lubo246": "Lữ Bố",
}
BLV_ID_ALIASES = {
    "246": "Lữ Bố",
}

QUALITY_TEXT_RE = re.compile(
    r"(?i)\b(4k|uhd|2160p?|full\s*hd|fhd|1080p?|hd|720p?|sd|480p?|auto)\b"
)


SPORT_GROUP_ORDER = (
    "Bóng đá",
    "Bóng rổ",
    "Bóng chuyền",
    "Tennis",
    "Esports",
    "Khác",
)
SPORT_GROUP_RANK = {name: index for index, name in enumerate(SPORT_GROUP_ORDER)}
SPORT_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    "Esports": (
        ("esports", 12), ("e sports", 12), ("esport", 12),
        ("counter strike", 9), ("cs2", 9), ("csgo", 9),
        ("dota", 9), ("league of legends", 9), ("valorant", 9),
        ("pubg", 8), ("mobile legends", 8), ("lien quan", 8),
        ("efootball", 8), ("fifa online", 8), ("arena of valor", 8),
    ),
    "Tennis": (
        ("tennis", 12), ("quan vot", 12), ("atp", 8), ("wta", 8),
        ("challenger", 7), ("wimbledon", 8), ("roland garros", 8),
        ("australian open", 8), ("us open", 7), ("davis cup", 7),
    ),
    "Bóng rổ": (
        ("bong ro", 12), ("basketball", 12), ("nba", 9), ("wnba", 9),
        ("euroleague", 8), ("fiba", 8), ("ncaa", 7), ("vba", 7),
        ("cba", 6), ("basket", 6),
    ),
    "Bóng chuyền": (
        ("bong chuyen", 12), ("volleyball", 12), ("fivb", 9),
        ("volleyball nations league", 10), ("nations league women", 8),
        ("nations league men", 8), ("vnl", 8), ("pvl", 7),
        ("cev", 5),
    ),
    "Bóng đá": (
        ("bong da", 12), ("football", 11), ("soccer", 11),
        ("futsal", 10), ("premier league", 8), ("champions league", 8),
        ("europa league", 8), ("conference league", 8),
        ("world cup", 7), ("asian cup", 7), ("copa", 6),
        ("uefa", 6), ("afc", 5), ("fc ", 4), (" fc", 4),
    ),
    "Khác": (
        ("cau long", 12), ("badminton", 12), ("bong ban", 12),
        ("table tennis", 12), ("baseball", 10), ("ice hockey", 10),
        ("hockey", 8), ("handball", 9), ("boxing", 9), ("mma", 9),
        ("motogp", 9), ("formula 1", 9), ("f1 racing", 9),
    ),
}


def normalize_search_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value).lower().replace("đ", "d"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {clean_text(text)} "


def classify_sport(*values: str, default: str = "Bóng đá") -> str:
    """Phân loại theo tín hiệu gần card/trang trận; tín hiệu đầu tiên có độ tin cậy cao thắng."""
    for value in values:
        normalized = normalize_search_text(value)
        if not normalized.strip():
            continue
        scores: dict[str, int] = {}
        for group, keywords in SPORT_KEYWORDS.items():
            score = 0
            for keyword, weight in keywords:
                token = f" {keyword.strip()} "
                if token in normalized or (len(keyword.strip()) >= 6 and keyword.strip() in normalized):
                    score += weight
            if score:
                scores[group] = score
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0]
    return default if default in SPORT_GROUP_RANK else "Khác"


def match_key_from_url(value: str) -> str:
    """Khóa ổn định theo slug trận Hygenie, bỏ query BLV khỏi phần slug."""
    parsed = urlparse(value or "")
    slug = unquote(parsed.path.rstrip("/").split("/")[-1]).lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", slug).strip("-")
    if not slug:
        slug = value or "hygenie"
    return hashlib.sha1(slug.encode("utf-8")).hexdigest()[:12]


def channel_id_for(result: dict[str, Any], stream_url: str, index: int) -> str:
    base = match_key_from_url(result.get("url", ""))
    blv_id = re.sub(r"[^a-zA-Z0-9_-]+", "", str(result.get("blv_id", "")))
    suffix = f"-{blv_id}" if blv_id else ""
    return f"hygenie-{base}{suffix}-{index}"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _decode_javascript_escapes(value: str) -> str:
    text = value or ""
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    text = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    return text.replace("\\/", "/")


def decode_url_repeatedly(value: str, rounds: int = 5) -> str:
    current = html.unescape(value or "").strip()
    for _ in range(rounds):
        decoded = html.unescape(_decode_javascript_escapes(current))
        decoded = unquote(decoded)
        if decoded == current:
            current = decoded
            break
        current = decoded
    return current.strip()


def normalize_blv_name(value: str) -> str:
    raw = clean_text(decode_url_repeatedly(value))
    raw = re.sub(r"(?i)^\s*(?:blv|bình\s*luận\s*viên)\s*[:\-–—]?\s*", "", raw)
    raw = raw.strip(" -|•[]()")
    if not raw or len(raw) > 60 or raw.isdigit() or re.search(r"(?i)\bvs\b", raw):
        return ""

    key = normalize_search_text(raw).strip().replace(" ", "")
    if key in BLV_ALIASES:
        return BLV_ALIASES[key]

    if re.fullmatch(r"[a-zA-Z0-9_.-]+", raw):
        words = re.sub(r"[_\-.]+", " ", raw).split()
        return " ".join(word.capitalize() for word in words)
    return raw


def extract_blv_from_url(value: str) -> str:
    try:
        query = parse_qs(urlparse(decode_url_repeatedly(value)).query)
    except Exception:
        return ""
    for key in ("blvName", "blv_name", "commentator", "commentatorName", "blv"):
        values = query.get(key) or query.get(key.lower())
        if values:
            name = normalize_blv_name(values[0])
            if name:
                return name
    return ""


def normalize_quality_hint(value: str) -> str:
    text = clean_text(decode_url_repeatedly(value))
    if not text:
        return ""
    match = QUALITY_TEXT_RE.search(text)
    if not match:
        return ""
    token = match.group(1).lower().replace(" ", "")
    if token in {"4k", "uhd", "2160", "2160p"}:
        return "4K"
    if token in {"fullhd", "fhd", "1080", "1080p"}:
        return "FHD"
    if token in {"hd", "720", "720p"}:
        return "HD"
    if token in {"sd", "480", "480p"}:
        return "SD"
    if token == "auto":
        return "AUTO"
    return token.upper()


def parse_hls_variants(text: str, base_url: str) -> list[dict[str, str]]:
    if "#EXTM3U" not in (text or "") or "#EXT-X-STREAM-INF" not in text:
        return []
    lines = [line.strip() for line in text.splitlines()]
    variants: list[dict[str, str]] = []
    pending = ""
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = line.partition(":")[2]
            continue
        if not pending or not line or line.startswith("#"):
            continue
        quality = normalize_quality_hint(pending)
        resolution = re.search(r"RESOLUTION=\d+x(\d+)", pending, re.I)
        if resolution:
            height = int(resolution.group(1))
            quality = "4K" if height >= 1800 else "FHD" if height >= 1000 else "HD" if height >= 700 else "SD"
        variants.append({
            "url": urljoin(base_url, line),
            "quality": quality,
            "parent_url": base_url,
        })
        pending = ""
    return variants


def absolute_url(value: str, base: str = TARGET_URL) -> str:
    value = decode_url_repeatedly(value)
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return ""
    try:
        return urljoin(base, value)
    except Exception:
        return value


def origin_from_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def extract_time(value: str) -> str:
    text = clean_text(value)

    # Hygenie hiển thị giờ Việt Nam trực tiếp trên card/H1. Ưu tiên giờ nhìn thấy
    # để không lấy nhầm câu SEO UTC kiểu 17:30 ngày hôm trước.
    match = TIME_RE.search(text)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    iso_match = re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
        text,
    )
    if iso_match:
        try:
            iso_value = iso_match.group(0).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
            return parsed.strftime("%H:%M")
        except Exception:
            pass
    return ""


def extract_date(value: str) -> str:
    text = clean_text(value)
    match = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?(?!\d)", text)
    if not match:
        return ""
    day, month = int(match.group(1)), int(match.group(2))
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return ""
    year = match.group(3) or ""
    if year and len(year) == 2:
        year = "20" + year
    return f"{day:02d}/{month:02d}" + (f"/{year}" if year else "")


def extract_hygenie_datetime_from_url(value: str) -> tuple[str, str]:
    slug = unquote(urlparse(value or "").path.rstrip("/").split("/")[-1])
    match = re.search(
        r"-vao-luc-(\d{2})(\d{2})-(\d{2})-(\d{2})-(\d{4})(?:-|$)",
        slug,
        re.I,
    )
    if not match:
        return "", ""
    hour, minute, day, month, year = match.groups()
    return f"{day}/{month}/{year}", f"{hour}:{minute}"


VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def resolve_scan_kickoff(
    time_str: str,
    date_str: str = "",
    now: datetime | None = None,
) -> datetime | None:
    time_match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", time_str or "")
    if not time_match:
        return None
    now = now.astimezone(VN_TZ) if now and now.tzinfo else (
        now.replace(tzinfo=VN_TZ) if now else datetime.now(VN_TZ)
    )
    hour, minute = int(time_match.group(1)), int(time_match.group(2))
    date_match = re.fullmatch(
        r"\s*(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])(?:/(20\d{2}|\d{2}))?\s*",
        date_str or "",
    )
    candidates: list[datetime] = []
    if date_match:
        day, month = int(date_match.group(1)), int(date_match.group(2))
        raw_year = date_match.group(3)
        years = [int(raw_year) + (2000 if len(raw_year) == 2 else 0)] if raw_year else [now.year - 1, now.year, now.year + 1]
        for year in years:
            try:
                candidates.append(datetime(year, month, day, hour, minute, tzinfo=VN_TZ))
            except ValueError:
                pass
    else:
        for offset in (-1, 0, 1):
            day = now.date() + timedelta(days=offset)
            candidates.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=VN_TZ))
    return min(candidates, key=lambda value: abs((value - now).total_seconds())) if candidates else None


def _has_explicit_live_hint(match: dict[str, Any]) -> bool:
    raw = " ".join(
        clean_text(str(match.get(key, "")))
        for key in ("card_text", "sport_hint", "raw_title", "status_text")
    )
    normalized = normalize_search_text(raw)
    markers = (
        " dang dien ra ", " dang da ", " live now ", " currently live ",
        " in play ", " hiep 1 ", " hiep 2 ", " halftime ",
    )
    return any(marker in normalized for marker in markers)


def filter_links_by_scan_window(
    links: list[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Lọc danh sách card trước khi mở browser từng trận."""
    now = now.astimezone(VN_TZ) if now and now.tzinfo else (
        now.replace(tzinfo=VN_TZ) if now else datetime.now(VN_TZ)
    )
    kept: list[dict[str, Any]] = []
    stats = {
        "total": len(links), "window": 0, "unknown_live": 0,
        "past": 0, "future": 0, "unknown": 0,
    }
    for item in links:
        url_date, url_time = extract_hygenie_datetime_from_url(str(item.get("url", "")))
        time_str = clean_text(str(item.get("time") or item.get("raw_time") or url_time))
        date_str = clean_text(str(item.get("date") or url_date))
        item["time"] = extract_time(time_str) or url_time
        item["date"] = date_str
        kickoff = resolve_scan_kickoff(item["time"], item["date"], now)
        item["scan_time_iso"] = now.isoformat()
        item["kickoff_iso"] = kickoff.isoformat() if kickoff else ""
        delta = int(round((kickoff - now).total_seconds() / 60)) if kickoff else None
        item["minutes_to_kickoff"] = delta
        if isinstance(delta, int):
            if -SCAN_PAST_MINUTES <= delta <= SCAN_FUTURE_MINUTES:
                item["scan_window_reason"] = "time-window"
                kept.append(item)
                stats["window"] += 1
            elif delta < -SCAN_PAST_MINUTES:
                item["scan_window_reason"] = "too-old"
                stats["past"] += 1
            else:
                item["scan_window_reason"] = "too-early"
                stats["future"] += 1
            continue
        if SCAN_UNKNOWN_LIVE and _has_explicit_live_hint(item):
            item["scan_window_reason"] = "unknown-time-live"
            kept.append(item)
            stats["unknown_live"] += 1
        else:
            item["scan_window_reason"] = "unknown-time"
            stats["unknown"] += 1
    return kept, stats


def print_scan_window_summary(stats: dict[str, int]) -> None:
    print(
        "🕒 Lọc cửa sổ quét "
        f"[-{SCAN_PAST_MINUTES}, +{SCAN_FUTURE_MINUTES}] phút: "
        f"tổng={stats.get('total', 0)} | giữ={stats.get('window', 0) + stats.get('unknown_live', 0)} "
        f"(đúng giờ={stats.get('window', 0)}, LIVE thiếu giờ={stats.get('unknown_live', 0)}) | "
        f"loại quá cũ={stats.get('past', 0)} | quá sớm={stats.get('future', 0)} | "
        f"không rõ giờ={stats.get('unknown', 0)}",
        flush=True,
    )



def effective_stream_wait_seconds(match: dict[str, Any]) -> int:
    """Rút ngắn phiên cho trận còn xa; trận gần giờ/live vẫn quét đủ."""
    delta = match.get("minutes_to_kickoff")
    if isinstance(delta, int) and delta > 0:
        if delta > UPCOMING_FAR_THRESHOLD_MINUTES:
            return min(STREAM_WAIT_SECONDS, UPCOMING_FAR_WAIT_SECONDS)
        return min(STREAM_WAIT_SECONDS, UPCOMING_NEAR_WAIT_SECONDS)
    return STREAM_WAIT_SECONDS


def should_probe_quality_buttons(match: dict[str, Any], has_candidate: bool = False) -> bool:
    delta = match.get("minutes_to_kickoff")
    if has_candidate:
        return True
    if not isinstance(delta, int):
        return True
    return delta <= UPCOMING_FAR_THRESHOLD_MINUTES

def stream_expiry_epoch(value: str) -> int | None:
    try:
        query = parse_qs(urlparse(canonicalize_stream_url(value)).query)
    except Exception:
        return None
    for key in ("expire", "expires", "exp", "e"):
        raw = (query.get(key) or [""])[0]
        if raw and raw.isdigit() and len(raw) >= 9:
            return int(raw)
    return None


def is_stream_expired(value: str, now_epoch: int | None = None, safety_seconds: int = 30) -> bool:
    expiry = stream_expiry_epoch(value)
    if expiry is None:
        return False
    current = int(time.time() if now_epoch is None else now_epoch)
    return expiry <= current + max(0, safety_seconds)


def stream_kind(url: str, content_type: str = "") -> str:
    clean = decode_url_repeatedly(url)
    lower_path = urlparse(clean).path.lower()
    lower_type = (content_type or "").lower()

    if ".m3u8" in lower_path or any(marker in lower_type for marker in (
        "application/vnd.apple.mpegurl", "application/x-mpegurl",
        "audio/mpegurl", "audio/x-mpegurl",
    )):
        return "m3u8"
    if ".flv" in lower_path or any(marker in lower_type for marker in (
        "video/x-flv", "video/flv", "application/x-flv",
    )):
        return "flv"
    return ""


WRAPPER_QUERY_KEYS = {"autoplay", "ishome", "is_home", "muted", "controls"}


def canonicalize_stream_url(value: str) -> str:
    """Làm sạch URL media nhưng giữ nguyên query token/chữ ký hợp lệ."""
    clean = decode_url_repeatedly(value).strip().rstrip("),];'\"")
    if not clean:
        return ""
    match = re.match(
        r"(?is)^(https?://.*?\.(?:m3u8|flv))(?P<tail>[?&#].*)?$",
        clean,
    )
    if not match:
        return clean
    base = match.group(1)
    tail = match.group("tail") or ""
    if tail.startswith("&"):
        # Đây là tham số của URL embed bị nối nhầm sau streamUrl.
        return base
    if tail.startswith("#"):
        return base
    if tail.startswith("?"):
        raw_parts = [part for part in tail[1:].split("&") if part]
        kept = []
        for part in raw_parts:
            key = part.split("=", 1)[0].strip().lower()
            if key in WRAPPER_QUERY_KEYS:
                continue
            kept.append(part)
        return base + ("?" + "&".join(kept) if kept else "")
    return base


def stream_channel_key(url: str) -> str:
    """Ví dụ /live/angao/playlist.m3u8 -> angao; /live/chuoichao.flv -> chuoichao."""
    path = urlparse(canonicalize_stream_url(url)).path.strip("/")
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    last = parts[-1].lower()
    if last in {"playlist.m3u8", "index.m3u8", "master.m3u8"} and len(parts) >= 2:
        return re.sub(r"[^a-z0-9_-]+", "", parts[-2].lower())
    stem = re.sub(r"\.(?:m3u8|flv)$", "", last, flags=re.I)
    return re.sub(r"[^a-z0-9_-]+", "", stem.lower())


def stream_family_key(url: str) -> str:
    key = stream_channel_key(url)
    return re.sub(r"(?:[-_]?)(?:fullhd|fhd|1080p?|hd|720p?)$", "", key, flags=re.I)


def _entry_is_browser_observed(entry: dict[str, Any]) -> bool:
    for source in entry.get("sources") or []:
        if (
            source.startswith("request/")
            or source.startswith("http/")
            or source == "response"
            or source == "iframe/src"
            or source == "hls/variant"
            or source == "home-card/stream-hint"
            or source.startswith("dom/")
            or source.startswith("quality/")
        ):
            return True
    return False


def _entry_is_high_confidence_observed(entry: dict[str, Any]) -> bool:
    """Nguồn đủ chắc để fallback khi runner bị CDN chặn."""
    for source in entry.get("sources") or []:
        if (
            source.startswith("request/")
            or source.startswith("http/")
            or source == "response"
            or source == "iframe/src"
            or source == "hls/variant"
            or source == "home-card/stream-hint"
        ):
            return True
    return False


def shortlist_stream_candidates(
    stream_map: dict[str, dict[str, Any]],
    match: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Loại danh sách stream toàn cục và chỉ giữ nguồn có liên hệ với player trận hiện tại."""
    active_families: set[str] = set()
    blv_slug = (parse_qs(urlparse(match.get("url", "")).query).get("blv") or [""])[0]
    blv_family = re.sub(r"[^a-z0-9_-]+", "", blv_slug.lower())
    if blv_family:
        active_families.add(blv_family)

    for entry in stream_map.values():
        if _entry_is_browser_observed(entry):
            family = stream_family_key(entry.get("url", ""))
            if family:
                active_families.add(family)

    ranked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    source_weights = {
        "http/iframe": 132,
        "http/stream": 130,
        "http/reference": 118,
        "response": 120,
        "iframe/src": 115,
        "hls/variant": 110,
        "home-card/stream-hint": 108,
        "previous-playlist": 82,
        "metadata/quality-source": 72,
        "response/body": 5,
    }

    for original in stream_map.values():
        entry = dict(original)
        entry["sources"] = list(original.get("sources") or [])
        entry["url"] = canonicalize_stream_url(entry.get("url", ""))
        if not is_direct_stream_url(entry["url"], entry.get("content_type", "")):
            entry["reject_reason"] = "URL media không hợp lệ"
            rejected.append(entry)
            continue
        if is_stream_expired(entry["url"]):
            entry["reject_reason"] = "URL ký số đã hết hạn"
            rejected.append(entry)
            continue

        family = stream_family_key(entry["url"])
        sources = entry.get("sources") or []
        only_body = bool(sources) and all(source == "response/body" for source in sources)
        is_previous = "previous-playlist" in sources
        is_observed = _entry_is_browser_observed(entry)
        if only_body and family not in active_families:
            entry["reject_reason"] = "chỉ xuất hiện trong response body toàn cục, không thuộc player hiện tại"
            rejected.append(entry)
            continue
        if active_families and family and family not in active_families and not is_previous and not is_observed:
            entry["reject_reason"] = "khác family stream đang được player trận hiện tại sử dụng"
            rejected.append(entry)
            continue

        score = 0
        for source in sources:
            if source.startswith("http/"):
                score = max(score, 130)
            elif source.startswith("request/"):
                score = max(score, 125)
            elif source.startswith("dom/"):
                score = max(score, 100)
            elif source.startswith("quality/"):
                score = max(score, 105)
            else:
                score = max(score, source_weights.get(source, 20))
        statuses = [int(value) for value in (entry.get("statuses") or [])]
        if any(value in {200, 206} for value in statuses):
            score += 35
        if any(value == 204 for value in statuses):
            score -= 45
        if any(value in {404, 410} for value in statuses):
            score -= 90
        if family and family in active_families:
            score += 55
        if blv_family and (family == blv_family or stream_channel_key(entry["url"]).startswith(blv_family)):
            score += 70
        if entry.get("quality"):
            score += 8
        normalized_referer = normalize_playback_referer(entry.get("referer", ""))
        referer_host = urlparse(normalized_referer).netloc.lower()
        allowed_hosts = {urlparse(value).netloc.lower() for value in HOME_URLS}
        if any(referer_host == item or referer_host.endswith("." + item) for item in allowed_hosts if item):
            score += 8

        entry["candidate_score"] = score
        entry["observed_active"] = _entry_is_browser_observed(entry)
        entry["high_confidence_observed"] = _entry_is_high_confidence_observed(entry)
        entry["channel_key"] = stream_channel_key(entry["url"])
        entry["family_key"] = family
        ranked.append(entry)

    ranked.sort(
        key=lambda item: (
            int(item.get("candidate_score") or 0),
            bool(item.get("observed_active")),
            item.get("quality") in {"4K", "FHD", "HD"},
        ),
        reverse=True,
    )

    # Giữ số lượng nhỏ để tránh tự tạo 429 trong bước xác minh.
    shortlisted: list[dict[str, Any]] = []
    per_channel: Counter[str] = Counter()
    for entry in ranked:
        channel = entry.get("channel_key") or entry["url"]
        if per_channel[channel] >= 2:
            entry["reject_reason"] = "trùng quá nhiều biến thể cùng channel"
            rejected.append(entry)
            continue
        shortlisted.append(entry)
        per_channel[channel] += 1
        if len(shortlisted) >= MAX_VERIFY_CANDIDATES:
            break

    for entry in ranked[len(shortlisted):]:
        if entry not in shortlisted and "reject_reason" not in entry:
            entry["reject_reason"] = "vượt giới hạn ứng viên xác minh"
            rejected.append(entry)
    return shortlisted, rejected


def _http_read_sample(
    url: str,
    headers: dict[str, str],
    timeout: int,
    max_bytes: int,
    range_header: str = "",
) -> dict[str, Any]:
    request_headers = dict(headers)
    request_headers.setdefault("Connection", "close")
    if range_header:
        request_headers["Range"] = range_header
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()) or 0)
            data = response.read(max_bytes)
            return {
                "status": status,
                "data": data,
                "content_type": response.headers.get("Content-Type", ""),
                "final_url": response.geturl(),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        sample = b""
        try:
            sample = exc.read(min(max_bytes, 4096))
        except Exception:
            pass
        return {
            "status": int(exc.code or 0),
            "data": sample,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "final_url": exc.geturl() or url,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": 0,
            "data": b"",
            "content_type": "",
            "final_url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _first_hls_uri(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if not line.startswith("#"):
            return line
    # Hỗ trợ LL-HLS/fMP4 khi segment chỉ xuất hiện trong thuộc tính URI.
    for line in lines:
        if line.startswith(("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:", "#EXT-X-MAP:")):
            match = re.search(r'URI="([^"]+)"', line, re.I)
            if match:
                return match.group(1)
    return ""


def _looks_like_error_page(data: bytes) -> bool:
    sample = data.lstrip()[:200].lower()
    return sample.startswith((b"<html", b"<!doctype", b"{\"error", b"access denied"))


def _hls_child_urls(parent_url: str, child_uri: str) -> list[str]:
    """Tạo URL con HLS và kế thừa query ký số khi CDN yêu cầu."""
    direct = urljoin(parent_url, child_uri)
    candidates = [direct]
    parent = urlparse(parent_url)
    child = urlparse(direct)
    if parent.query and not child.query:
        inherited = child._replace(query=parent.query).geturl()
        if inherited not in candidates:
            candidates.append(inherited)
    return candidates


def _read_first_working_hls_child(
    parent_url: str,
    child_uri: str,
    headers: dict[str, str],
    timeout: int,
    max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    last: dict[str, Any] = {"status": 0, "data": b"", "final_url": "", "error": ""}
    for candidate in _hls_child_urls(parent_url, child_uri):
        current = _http_read_sample(candidate, headers, timeout, max_bytes)
        last = current
        if int(current.get("status") or 0) in {200, 206} and current.get("data"):
            return candidate, current
    return _hls_child_urls(parent_url, child_uri)[0], last


def probe_stream_sync(
    url: str,
    user_agent: str,
    referer: str,
    origin: str = "",
    cookie_header: str = "",
    timeout: int = 8,
) -> dict[str, Any]:
    """Xác minh manifest/segment HLS hoặc chữ ký FLV bằng đúng header của player."""
    canonical = canonicalize_stream_url(url)
    kind = stream_kind(canonical)
    headers = {
        "User-Agent": user_agent or UA,
        "Referer": referer or PLAYER_ORIGIN_FALLBACK + "/",
        "Accept": "*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    effective_origin = clean_text(origin)
    if not effective_origin:
        parsed_ref = urlparse(headers["Referer"])
        allowed_hosts = {urlparse(value).netloc.lower() for value in HOME_URLS}
        if any(parsed_ref.netloc.lower() == item or parsed_ref.netloc.lower().endswith("." + item) for item in allowed_hosts if item):
            effective_origin = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
    if effective_origin:
        headers["Origin"] = effective_origin
    if cookie_header:
        headers["Cookie"] = cookie_header

    if kind == "flv":
        result = _http_read_sample(canonical, headers, timeout, 4096, range_header="bytes=0-4095")
        if int(result.get("status") or 0) in {204, 416} or not (result.get("data") or b""):
            retry = _http_read_sample(canonical, headers, timeout, 4096)
            if int(retry.get("status") or 0) or retry.get("data"):
                result = retry
        status = int(result.get("status") or 0)
        data = result.get("data") or b""
        ctype = str(result.get("content_type") or "").lower()
        playable = status in {200, 206} and (data.startswith(b"FLV") or ("flv" in ctype and len(data) >= 3))
        state = "verified" if playable else (
            "blocked" if status in {401, 403, 429} else
            "dead" if status in {404, 410} else
            "empty" if status == 204 else "invalid"
        )
        return {
            **result, "playable": playable, "state": state, "kind": kind,
            "detail": "FLV signature/content-type OK" if playable else result.get("error") or "không có chữ ký FLV",
        }

    if kind == "m3u8":
        manifest = _http_read_sample(canonical, headers, timeout, 768_000)
        status = int(manifest.get("status") or 0)
        data = manifest.get("data") or b""
        text = data.decode("utf-8", errors="ignore").lstrip("\ufeff\r\n \t")
        if status not in {200, 206}:
            state = "blocked" if status in {401, 403, 429} else "dead" if status in {404, 410} else "invalid"
            return {**manifest, "playable": False, "state": state, "kind": kind,
                    "detail": manifest.get("error") or f"manifest HTTP {status}"}
        if not text.startswith("#EXTM3U"):
            return {**manifest, "playable": False, "state": "invalid", "kind": kind,
                    "detail": "nội dung không bắt đầu bằng #EXTM3U"}

        first_uri = _first_hls_uri(text)
        if not first_uri:
            return {**manifest, "playable": False, "state": "empty", "kind": kind,
                    "detail": "manifest chưa có variant/segment"}

        parent_url = str(manifest.get("final_url") or canonical)
        child_url, child = _read_first_working_hls_child(parent_url, first_uri, headers, timeout, 768_000)
        if "#EXT-X-STREAM-INF" in text:
            child_status = int(child.get("status") or 0)
            child_text = (child.get("data") or b"").decode("utf-8", errors="ignore").lstrip("\ufeff\r\n \t")
            if child_status not in {200, 206} or not child_text.startswith("#EXTM3U"):
                return {**manifest, "playable": False,
                        "state": "blocked" if child_status in {401, 403, 429} else "invalid",
                        "kind": kind, "detail": f"variant không tải được: HTTP {child_status}",
                        "child_url": child_url, "child_status": child_status}
            first_uri = _first_hls_uri(child_text)
            if not first_uri:
                return {**manifest, "playable": False, "state": "empty", "kind": kind,
                        "detail": "variant chưa có segment", "child_url": child_url,
                        "child_status": child_status}
            child_url, child = _read_first_working_hls_child(
                str(child.get("final_url") or child_url), first_uri, headers, timeout, 4096
            )

        segment = child
        if int(segment.get("status") or 0) in {204, 416} or not (segment.get("data") or b""):
            retry_segment = _http_read_sample(child_url, headers, timeout, 4096)
            if int(retry_segment.get("status") or 0) or retry_segment.get("data"):
                segment = retry_segment
        segment_status = int(segment.get("status") or 0)
        segment_data = segment.get("data") or b""
        playable = segment_status in {200, 206} and len(segment_data) >= 64 and not _looks_like_error_page(segment_data)
        return {
            **manifest, "playable": playable,
            "state": "verified" if playable else (
                "blocked" if segment_status in {401, 403, 429} else
                "dead" if segment_status in {404, 410} else "invalid"
            ),
            "kind": kind,
            "detail": "manifest + segment OK" if playable else f"segment HTTP {segment_status}",
            "segment_url": child_url, "segment_status": segment_status,
            "segment_bytes": len(segment_data),
        }

    return {"playable": False, "state": "invalid", "kind": "", "status": 0,
            "detail": "không nhận diện được loại stream"}

async def validate_stream_candidates(
    context: BrowserContext,
    candidates: list[dict[str, Any]],
    match: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not candidates:
        return [], []
    if not VERIFY_STREAMS:
        for entry in candidates:
            entry["playability"] = "not-checked"
        return candidates[:MAX_OUTPUT_STREAMS_PER_MATCH], []

    semaphore = asyncio.Semaphore(2)

    async def validate_one(entry: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            referer = normalize_playback_referer(
                entry.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"
            )
            user_agent = clean_text(entry.get("user_agent") or UA)
            origin = clean_text(entry.get("origin") or PLAYER_ORIGIN_FALLBACK)
            cookie_header = ""
            try:
                cookies = await context.cookies([entry["url"]])
                cookie_header = "; ".join(
                    f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies
                    if cookie.get("name")
                )
            except Exception:
                pass
            cache_key = (entry["url"], referer, user_agent, origin + "|" + cookie_header)
            cached = PROBE_CACHE.get(cache_key)
            if cached is None:
                probe = await asyncio.to_thread(
                    probe_stream_sync,
                    entry["url"],
                    user_agent,
                    referer,
                    origin,
                    cookie_header,
                    VERIFY_TIMEOUT_SECONDS,
                )
                sample_data = probe.pop("data", b"")
                probe["sample_bytes"] = len(sample_data) if isinstance(sample_data, (bytes, bytearray)) else 0
                if len(PROBE_CACHE) >= 500:
                    PROBE_CACHE.clear()
                PROBE_CACHE[cache_key] = dict(probe)
            else:
                probe = dict(cached)
                sample_data = b""
            probe.setdefault("sample_bytes", len(sample_data) if isinstance(sample_data, (bytes, bytearray)) else 0)
            entry["probe"] = probe
            entry["referer"] = referer
            entry["origin"] = origin
            entry["user_agent"] = user_agent
            return entry

    checked = await asyncio.gather(*(validate_one(dict(entry)) for entry in candidates))
    verified: list[dict[str, Any]] = []
    observed_fallback: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for entry in checked:
        probe = entry.get("probe") or {}
        state = probe.get("state", "invalid")
        status = int(probe.get("status") or 0)
        blocking_status = int(probe.get("segment_status") or probe.get("child_status") or status or 0)
        if probe.get("playable"):
            entry["playability"] = "verified"
            verified.append(entry)
            print(
                f"   ✅ ĐÃ XÁC MINH {stream_kind(entry['url']).upper()} | "
                f"HTTP {status} | {probe.get('detail')} | {entry['url']}",
                flush=True,
            )
            continue

        if state in {"blocked", "invalid"} and entry.get("high_confidence_observed") and blocking_status in {0, 401, 403, 429}:
            entry["playability"] = "browser-observed"
            observed_fallback.append(entry)
            print(
                f"   🟡 Runner chưa xác minh được ({probe.get('detail')}); "
                f"nhưng player đã thực sự tham chiếu URL: {entry['url']}",
                flush=True,
            )
            continue

        if "previous-playlist" in (entry.get("sources") or []):
            entry["playability"] = "rejected"
            entry["reject_reason"] = (
                "link playlist cũ không được player hiện tại gọi và chưa xác minh phát được"
            )
            rejected.append(entry)
            print(
                f"   ❌ Không giữ link cũ chưa xác minh: {entry['url']}",
                flush=True,
            )
            continue

        entry["playability"] = "rejected"
        entry["reject_reason"] = probe.get("detail") or state
        rejected.append(entry)
        print(
            f"   ❌ Loại link không phát được | {entry.get('reject_reason')} | {entry['url']}",
            flush=True,
        )

    # Có link xác minh thật thì không trộn link mơ hồ vào playlist chính.
    if verified:
        for entry in observed_fallback:
            entry["reject_reason"] = "đã có stream xác minh thật nên không dùng fallback"
        rejected.extend(observed_fallback)
        selected = verified
    else:
        selected = observed_fallback
    selected.sort(
        key=lambda item: (
            item.get("playability") == "verified",
            int(item.get("candidate_score") or 0),
            item.get("quality") in {"4K", "FHD", "HD"},
        ),
        reverse=True,
    )
    return selected[:MAX_OUTPUT_STREAMS_PER_MATCH], rejected + selected[MAX_OUTPUT_STREAMS_PER_MATCH:]


async def finalize_stream_map(
    context: BrowserContext,
    stream_map: dict[str, dict[str, Any]],
    match: dict[str, Any],
    *,
    log_prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, pre_rejected = shortlist_stream_candidates(stream_map, match)
    print(
        f"   🔎 {log_prefix}Ứng viên sau lọc quan hệ player: {len(candidates)}/"
        f"{len(stream_map)}; loại sớm={len(pre_rejected)}",
        flush=True,
    )
    streams, validation_rejected = await validate_stream_candidates(context, candidates, match)
    rejected = pre_rejected + validation_rejected
    variant_parents = {
        entry.get("parent_url") for entry in streams
        if entry.get("parent_url") and entry.get("quality")
    }
    if variant_parents:
        streams = [
            entry for entry in streams
            if entry.get("url") not in variant_parents or entry.get("quality")
        ]
    streams = sorted(
        streams,
        key=lambda item: (
            item.get("playability") == "verified",
            item.get("quality") in {"4K", "FHD", "HD"},
            stream_kind(item.get("url", "")) == "m3u8",
            int(item.get("candidate_score") or 0),
        ),
        reverse=True,
    )[:MAX_OUTPUT_STREAMS_PER_MATCH]
    return streams, rejected


def match_id_from_url(value: str) -> str:
    return match_key_from_url(value)


def _parse_previous_playlist_text(text: str, source_label: str) -> dict[str, list[dict[str, str]]]:
    mapping: dict[str, list[dict[str, str]]] = {}
    current_match_id = ""
    referer = PLAYER_ORIGIN_FALLBACK + "/"
    origin = PLAYER_ORIGIN_FALLBACK
    user_agent = UA
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF"):
            id_match = re.search(r'tvg-id="hygenie-([a-f0-9]{12})(?:-[^"-]+)?-\d+"', line)
            current_match_id = id_match.group(1) if id_match else ""
            referer = PLAYER_ORIGIN_FALLBACK + "/"
            origin = PLAYER_ORIGIN_FALLBACK
            user_agent = UA
        elif line.startswith("#EXTVLCOPT:http-referrer="):
            referer = line.split("=", 1)[1].strip()
        elif line.startswith("#EXTVLCOPT:http-user-agent="):
            user_agent = line.split("=", 1)[1].strip()
        elif line.startswith("#EXTHTTP:"):
            try:
                header_data = json.loads(line.split(":", 1)[1])
                origin = str(header_data.get("Origin") or origin)
                referer = str(header_data.get("Referer") or referer)
                user_agent = str(header_data.get("User-Agent") or user_agent)
            except Exception:
                pass
        elif line.startswith(("http://", "https://")) and current_match_id:
            url = canonicalize_stream_url(line.split("|", 1)[0])
            if is_direct_stream_url(url) and not is_stream_expired(url):
                mapping.setdefault(current_match_id, []).append({
                    "url": url,
                    "referer": referer,
                    "origin": origin,
                    "user_agent": user_agent,
                    "history_source": source_label,
                })
            current_match_id = ""
    return mapping


def load_previous_playlist_streams(path: str = OUTPUT_M3U) -> dict[str, list[dict[str, str]]]:
    """Đọc playlist hiện tại và tối đa 2 commit trước để cứu link từng chạy tốt."""
    sources: list[tuple[str, str]] = []
    playlist = Path(path)
    if playlist.exists():
        try:
            sources.append(("working-tree", playlist.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            pass

    # Workflow checkout dùng fetch-depth=0, nên có thể kiểm tra lại playlist trước
    # khi bản parser mới ghi đè. Mọi link lịch sử vẫn phải qua bước probe.
    for revision in ("HEAD~1", "HEAD~2"):
        try:
            completed = subprocess.run(
                ["git", "show", f"{revision}:{path}"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=5,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                sources.append((revision, completed.stdout))
        except Exception:
            continue

    merged: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for source_label, text in sources:
        parsed = _parse_previous_playlist_text(text, source_label)
        for match_id, items in parsed.items():
            for item in items:
                key = (match_id, item["url"])
                if key in seen:
                    continue
                seen.add(key)
                merged.setdefault(match_id, []).append(item)
    return merged


def is_direct_stream_url(url: str, content_type: str = "") -> bool:
    if not url:
        return False
    clean = canonicalize_stream_url(url)
    parsed = urlparse(clean)
    lower_url = clean.lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if not stream_kind(clean, content_type):
        return False
    return not any(marker in lower_url for marker in AD_MARKERS)


def extract_stream_urls(raw_url: str, content_type: str = "") -> list[str]:
    """Tách luồng trực tiếp, kể cả streamUrl đã percent-encode trong iframe embed."""
    if not raw_url:
        return []

    pending = [raw_url]
    seen_values: set[str] = set()
    found: list[str] = []
    nested_param_names = {
        "streamurl", "stream_url", "stream", "url", "src", "file",
        "source", "video", "hls", "flv", "playurl", "play_url",
    }

    while pending and len(seen_values) < 60:
        value = decode_url_repeatedly(pending.pop(0))
        if not value or value in seen_values:
            continue
        seen_values.add(value)

        direct_type = content_type if value == decode_url_repeatedly(raw_url) else ""
        canonical = canonicalize_stream_url(value)
        if is_direct_stream_url(canonical, direct_type):
            if canonical not in found:
                found.append(canonical)
            continue

        try:
            query = parse_qs(urlparse(value).query, keep_blank_values=False)
        except Exception:
            query = {}

        for key, values in query.items():
            if key.lower() not in nested_param_names:
                continue
            for nested in values:
                decoded = decode_url_repeatedly(nested)
                if decoded.startswith(("http://", "https://")):
                    pending.append(decoded)

        for match in re.findall(
            r"https?://[^\s\"'<>]+?(?:\.m3u8|\.flv)(?:\?[^\s\"'<>]*)?",
            decode_url_repeatedly(value),
            flags=re.IGNORECASE,
        ):
            pending.append(match.rstrip("),];"))

    return found


def stream_referer_hint(raw_candidate: str, frame_url: str = "") -> str:
    """Ưu tiên origin của iframe embed chứa streamUrl, không dùng nhầm trang trận."""
    decoded = decode_url_repeatedly(raw_candidate)
    if extract_stream_urls(decoded) and not is_direct_stream_url(decoded):
        embedded_origin = origin_from_url(decoded)
        if embedded_origin:
            return embedded_origin + "/"
    if frame_url:
        frame_origin = origin_from_url(frame_url)
        if frame_origin:
            return frame_origin + "/"
    return ""


def normalize_playback_referer(value: str) -> str:
    """Giữ đúng Referer Hygenie mà request HLS thật đã gửi."""
    candidate = decode_url_repeatedly(value)
    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        allowed_hosts = {urlparse(value).netloc.lower() for value in HOME_URLS}
        if any(host == item or host.endswith("." + item) for item in allowed_hosts if item):
            return f"{parsed.scheme}://{parsed.netloc}/"
        return candidate
    return PLAYER_ORIGIN_FALLBACK.rstrip("/") + "/"


def parse_hygenie_match_name(value: str, fallback_url: str) -> str:
    text = clean_text(value)
    title_match = re.search(
        r"(?i)link\s+trực\s+tiếp\s+trận\s+(.+?)\s+ngày\s+\d{1,2}[-/]\d{1,2}[-/]\d{4}",
        text,
    )
    if title_match:
        text = clean_text(title_match.group(1))
    text = re.sub(r"(?i)^link\s+trực\s+tiếp\s+trận\s+", "", text)
    text = re.sub(r"(?i)\s+ngày\s+\d{1,2}[-/]\d{1,2}[-/]\d{4}.*$", "", text)
    text = re.sub(r"\s+[–—]\s+", " vs ", text, count=1)
    text = re.sub(r"\s+-\s+", " vs ", text, count=1)
    if re.search(r"\bvs\b", text, re.I):
        return clean_text(text).strip(" -|•")

    slug = unquote(urlparse(fallback_url).path.rstrip("/").split("/")[-1])
    slug = re.sub(r"-vao-luc-\d{4}-\d{2}-\d{2}-\d{4}.*$", "", slug, flags=re.I)
    slug = re.sub(r"-vs-", " vs ", slug, count=1, flags=re.I)
    return clean_text(slug.replace("-", " ")) or fallback_url


def clean_match_name(value: str, fallback_url: str) -> str:
    return parse_hygenie_match_name(value, fallback_url)


def derive_match_info(
    url: str,
    raw_title: str = "",
    raw_time: str = "",
) -> tuple[str, str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    match_name = clean_match_name(raw_title, url)
    _url_date, url_time = extract_hygenie_datetime_from_url(url)
    time_str = extract_time(raw_time) or extract_time(raw_title) or url_time

    blv_name = ""
    for key in ("blvName", "blv_name", "commentator", "commentatorName"):
        values = query.get(key) or query.get(key.lower())
        if values:
            blv_name = normalize_blv_name(values[0])
            if blv_name:
                break
    return match_name, time_str, blv_name


def is_good_logo_url(value: str) -> bool:
    lower = (value or "").lower()
    if not value or value.startswith(("data:", "blob:")):
        return False
    bad = (
        "avatar", "banner", "advert", "doubleclick", "googleads", "emoji",
        "flag", "favicon", "placeholder", "default-avatar", "no-image",
        "logo-white", "logo-dark", "site-logo", "loading.gif",
    )
    return not any(marker in lower for marker in bad)


def _team_parts(match_name: str) -> tuple[str, str]:
    parts = re.split(r"(?i)\s+vs\s+", clean_text(match_name), maxsplit=1)
    home = parts[0] if parts else ""
    away = parts[1].split(" - ", 1)[0] if len(parts) > 1 else ""
    return home, away


def _candidate_dict(value: Any, base: str) -> dict[str, Any]:
    if isinstance(value, dict):
        raw_url = str(value.get("url") or value.get("value") or "")
        context = clean_text(str(value.get("context") or ""))
        source = clean_text(str(value.get("source") or ""))
        try:
            score = float(value.get("score") or 0)
        except Exception:
            score = 0.0
    else:
        raw_url = str(value or "")
        context = ""
        source = ""
        score = 0.0
    return {
        "url": absolute_url(raw_url, base),
        "context": context,
        "source": source,
        "score": score,
    }


def _logo_context_and_hits(
    candidate: dict[str, Any],
    match_name: str,
) -> tuple[str, int, int]:
    url = candidate.get("url", "")
    context = normalize_search_text(
        f"{candidate.get('context', '')} {urlparse(url).path}"
    )
    home, away = _team_parts(match_name)
    home_tokens = [
        token for token in normalize_search_text(home).split()
        if len(token) >= 4
    ]
    away_tokens = [
        token for token in normalize_search_text(away).split()
        if len(token) >= 4
    ]
    home_hits = sum(1 for token in home_tokens if f" {token} " in f" {context} ")
    away_hits = sum(1 for token in away_tokens if f" {token} " in f" {context} ")
    return context, home_hits, away_hits


def score_logo_candidate(candidate: dict[str, Any], match_name: str) -> float:
    url = candidate.get("url", "")
    if not is_good_logo_url(url):
        return -10000

    score = float(candidate.get("score") or 0)
    context, home_hits, away_hits = _logo_context_and_hits(candidate, match_name)
    source = clean_text(str(candidate.get("source") or "")).lower()

    if home_hits:
        score += 55 + min(home_hits, 3) * 8
    elif away_hits:
        score += 35 + min(away_hits, 3) * 6

    if any(marker in f" {context} " for marker in (
        " team ", " club ", " home ", " away ", " doi ", " đội "
    )):
        score += 10

    if any(marker in f" {context} " for marker in (
        " avatar ", " blv ", " commentator ", " banner ", " league ",
        " sponsor ", " advert ", " quảng cáo "
    )):
        score -= 45

    # Ảnh lấy từ card/trang trận nhưng không hề có dấu hiệu thuộc hai đội rất dễ là
    # logo của một trận liên quan nằm cùng section. Thà bỏ trống còn hơn gán sai.
    if not home_hits and not away_hits:
        if source in {"home-card", "detail-match", "detail-team"}:
            score -= 18
        else:
            score -= 40

    if source == "detail-team":
        score += 10
    elif source == "detail-match":
        score += 5
    elif source == "meta":
        score -= 25

    return score


def ranked_logo_candidates(
    candidates: list[Any],
    base: str,
    match_name: str = "",
) -> list[dict[str, Any]]:
    # Cùng một URL có thể xuất hiện từ card trang chủ, DOM trang trận và metadata.
    # Giữ bản có context/điểm tốt nhất thay vì giữ lần xuất hiện đầu tiên.
    best_by_url: dict[str, dict[str, Any]] = {}
    for value in candidates:
        item = _candidate_dict(value, base)
        if not item["url"]:
            continue
        _context, home_hits, away_hits = _logo_context_and_hits(item, match_name)
        item["home_hits"] = home_hits
        item["away_hits"] = away_hits
        item["final_score"] = score_logo_candidate(item, match_name)
        if item["final_score"] <= -1000:
            continue
        previous = best_by_url.get(item["url"])
        if previous is None or (
            item["final_score"], item.get("home_hits", 0), item.get("away_hits", 0)
        ) > (
            previous["final_score"], previous.get("home_hits", 0), previous.get("away_hits", 0)
        ):
            best_by_url[item["url"]] = item

    ranked = list(best_by_url.values())
    ranked.sort(
        key=lambda item: (
            item["final_score"],
            item.get("home_hits", 0),
            item.get("away_hits", 0),
        ),
        reverse=True,
    )
    return ranked


def choose_logo(candidates: list[Any], base: str, match_name: str = "") -> str:
    ranked = ranked_logo_candidates(candidates, base, match_name)
    if not ranked:
        return ""
    best = ranked[0]
    # Chỉ dùng khi có dấu hiệu rõ ràng ảnh thuộc đội/trận hiện tại.
    if best["final_score"] < 28:
        return ""
    if not best.get("home_hits") and not best.get("away_hits") and best["final_score"] < 45:
        return ""
    return best["url"]


def resolve_duplicate_logos(results: list[dict[str, Any]]) -> None:
    """Giữ logo lặp cho đúng trận nhất, loại khỏi các trận bị gán nhầm."""
    ranked_by_result: dict[int, list[dict[str, Any]]] = {}
    for result in results:
        candidates = list(result.get("logo_candidates") or [])
        candidates.extend(result.get("team_logos") or [])
        home_logo = result.get("home_logo") or ""
        home_name = _team_parts(result.get("match_name", ""))[0]
        if home_logo:
            candidates.insert(0, {
                "url": home_logo,
                "score": 180,
                "context": f"Logo {home_name}",
                "source": "hygenie-exact-home",
            })
        if result.get("logo"):
            candidates.append(result["logo"])
        ranked = ranked_logo_candidates(
            candidates,
            result.get("url") or TARGET_URL,
            result.get("match_name") or "",
        )
        ranked_by_result[id(result)] = ranked
        result["logo"] = choose_logo(
            candidates,
            result.get("url") or TARGET_URL,
            result.get("match_name") or "",
        ) or home_logo

    usage: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if result.get("logo"):
            usage.setdefault(result["logo"], []).append(result)

    reserved: set[str] = set()
    for logo_url, owners in usage.items():
        home_teams = {
            normalize_search_text(_team_parts(owner.get("match_name", ""))[0]).strip()
            for owner in owners
        }
        if len(owners) < 2 or len(home_teams) <= 1:
            reserved.add(logo_url)
            continue

        scored_owners: list[tuple[float, int, int, dict[str, Any]]] = []
        for owner in owners:
            candidate = next(
                (item for item in ranked_by_result[id(owner)] if item["url"] == logo_url),
                None,
            )
            if candidate:
                scored_owners.append((
                    float(candidate.get("final_score") or -9999),
                    int(candidate.get("home_hits") or 0),
                    int(candidate.get("away_hits") or 0),
                    owner,
                ))
            else:
                scored_owners.append((-9999.0, 0, 0, owner))

        scored_owners.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        top = scored_owners[0]
        second_score = scored_owners[1][0] if len(scored_owners) > 1 else -9999
        winner: dict[str, Any] | None = None
        if (
            top[0] >= 45
            and (top[1] > 0 or top[2] > 0)
            and (top[0] - second_score >= 12 or second_score < 28)
        ):
            winner = top[3]
            reserved.add(logo_url)

        print(
            f"   ⚠️ Phát hiện một logo bị gán cho {len(owners)} trận khác nhau; "
            f"giữ cho trận khớp nhất và chọn lại các trận còn lại: {logo_url}",
            flush=True,
        )

        for owner in owners:
            if owner is winner:
                continue
            alternatives = [
                item for item in ranked_by_result[id(owner)]
                if item["url"] != logo_url
                and item["url"] not in reserved
                and item["final_score"] >= 28
                and (item.get("home_hits") or item.get("away_hits"))
            ]
            owner["logo"] = alternatives[0]["url"] if alternatives else ""
            if owner["logo"]:
                reserved.add(owner["logo"])



async def install_route_filter(page: Page, homepage: bool = False) -> None:
    """Cho ảnh tải để lazy-load logo hoạt động; chỉ chặn font và media ở trang chủ."""
    blocked_types = {"font"}
    if homepage:
        blocked_types.add("media")

    async def route_handler(route: Route) -> None:
        if route.request.resource_type in blocked_types:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)


async def collect_dom_stream_candidates(page: Page) -> list[dict[str, str]]:
    """Chỉ lấy nguồn đang được player sử dụng; không quét regex toàn bộ HTML/script."""
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for frame in page.frames:
        try:
            frame_candidates = await frame.evaluate(
                r"""() => {
                    const out = [];
                    const seen = new Set();
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const qualityOf = (v) => {
                        const text = clean(v);
                        const match = text.match(/\b(4K|UHD|2160p?|Full\s*HD|FHD|1080p?|HD|720p?|SD|480p?|Auto)\b/i);
                        return match ? match[1] : "";
                    };
                    const add = (value, source, quality = "", context = "") => {
                        if (!value) return;
                        const raw = String(value).trim();
                        if (!raw || raw.length > 12000) return;
                        const key = `${raw}\n${source}\n${quality}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        out.push({
                            url: raw,
                            source,
                            quality: qualityOf(quality || context),
                            context: clean(context),
                        });
                    };

                    // Resource Timing chỉ chứa request đã thực sự được trình duyệt phát ra.
                    try {
                        for (const entry of performance.getEntriesByType("resource")) {
                            if (entry && entry.name && /\.m3u8|\.flv/i.test(entry.name)) {
                                add(entry.name, "performance");
                            }
                        }
                    } catch (_) {}

                    // Nguồn đang gắn trực tiếp vào player.
                    document.querySelectorAll("video, source").forEach((el) => {
                        const context = clean([
                            el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"),
                            el.getAttribute("aria-label"),
                            el.title,
                            el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(context);
                        [
                            el.currentSrc,
                            el.src,
                            el.getAttribute("src"),
                            el.getAttribute("data-src"),
                            el.getAttribute("data-url"),
                            el.getAttribute("data-stream"),
                            el.getAttribute("data-stream-url"),
                            el.getAttribute("data-file"),
                        ].forEach((value) => add(value, "media-element", quality, context));
                    });

                    // Iframe active thường chứa streamUrl đã percent-encode.
                    document.querySelectorAll("iframe[src]").forEach((el) => {
                        const context = clean([
                            el.getAttribute("title"),
                            el.getAttribute("aria-label"),
                            el.className,
                        ].filter(Boolean).join(" "));
                        add(el.src || el.getAttribute("src"), "iframe", qualityOf(context), context);
                    });

                    // Chỉ đọc data-* của phần tử đang active/selected/visible trong player.
                    document.querySelectorAll(
                        "[data-stream], [data-stream-url], [data-hls], [data-flv], [data-file], [data-url]"
                    ).forEach((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        const active = el.matches(
                            ".active, .selected, [aria-selected='true'], [aria-current='true'], :checked"
                        );
                        const visible = rect.width > 0 && rect.height > 0 &&
                            style.display !== "none" && style.visibility !== "hidden";
                        if (!active && !visible) return;
                        const context = clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(context);
                        [
                            el.getAttribute("data-stream"),
                            el.getAttribute("data-stream-url"),
                            el.getAttribute("data-hls"),
                            el.getAttribute("data-flv"),
                            el.getAttribute("data-file"),
                            el.getAttribute("data-url"),
                        ].forEach((value) => add(value, "active-data", quality, context));
                    });
                    return out.slice(0, 120);
                }"""
            )
            for item in frame_candidates:
                raw_url = str(item.get("url", "")) if isinstance(item, dict) else str(item)
                quality = str(item.get("quality", "")) if isinstance(item, dict) else ""
                source = str(item.get("source", "dom")) if isinstance(item, dict) else "dom"
                key = (raw_url, frame.url or "", source)
                if raw_url and key not in seen:
                    seen.add(key)
                    candidates.append({
                        "url": raw_url,
                        "frame_url": frame.url or "",
                        "quality": normalize_quality_hint(quality),
                        "source": source,
                    })
        except Exception:
            continue

    return candidates


async def stimulate_player(page: Page) -> None:
    for selector in PLAY_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count():
                await locator.first.click(timeout=700, force=True)
        except Exception:
            pass

    for frame in page.frames:
        try:
            await frame.evaluate(
                """() => {
                    document.querySelectorAll("video").forEach((video) => {
                        try {
                            video.muted = true;
                            video.volume = 0;
                            const result = video.play();
                            if (result && typeof result.catch === "function") {
                                result.catch(() => {});
                            }
                        } catch (_) {}
                    });
                }"""
            )
        except Exception:
            pass


async def stimulate_quality_variants(page: Page) -> int:
    """Mở menu chất lượng và lần lượt kích hoạt HD/FHD/1080 để lộ mọi URL."""
    clicked = 0
    for frame in page.frames:
        try:
            count = await frame.evaluate(
                r"""async () => {
                    const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const nodes = Array.from(document.querySelectorAll(
                        "button, a, [role='button'], [role='option'], li, label, [data-quality], [data-resolution]"
                    )).filter((el) => {
                        if (el.tagName !== "A") return true;
                        const href = String(el.getAttribute("href") || "").trim();
                        return !href || href === "#" || href.startsWith("javascript:");
                    });
                    const textOf = (el) => clean([
                        el.innerText, el.textContent, el.getAttribute("aria-label"),
                        el.getAttribute("title"), el.getAttribute("data-quality"),
                        el.getAttribute("data-resolution"), el.className
                    ].filter(Boolean).join(" "));

                    let clicks = 0;
                    const menu = nodes.find((el) => visible(el) && /quality|chất lượng|độ phân giải/i.test(textOf(el)));
                    if (menu) {
                        try { menu.click(); clicks += 1; await delay(300); } catch (_) {}
                    }

                    const options = nodes.filter((el) =>
                        visible(el) && /\b(4K|UHD|2160p?|Full\s*HD|FHD|1080p?|HD|720p?|SD|480p?)\b/i.test(textOf(el))
                    ).slice(0, 10);
                    for (const option of options) {
                        try { option.click(); clicks += 1; await delay(450); } catch (_) {}
                    }
                    return clicks;
                }"""
            )
            clicked += int(count or 0)
        except Exception:
            continue
    return clicked


async def scan_quality_variants(page: Page, capture_callback: Any) -> list[str]:
    """Bấm từng chất lượng và chỉ ghi URL mới xuất hiện sau lần bấm đó."""
    discovered: list[str] = []

    for frame in list(page.frames):
        try:
            labels = await frame.evaluate(
                r"""() => {
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const qualityOf = (value) => {
                        const text = clean(value);
                        if (/\b(4K|UHD|2160p?)\b/i.test(text)) return "4K";
                        if (/\b(Full\s*HD|FHD|1080p?)\b/i.test(text)) return "FHD";
                        if (/\b(HD|720p?)\b/i.test(text)) return "HD";
                        if (/\b(SD|480p?)\b/i.test(text)) return "SD";
                        return "";
                    };
                    const values = [];
                    document.querySelectorAll(
                        "button, a, [role='button'], [role='option'], li, label, " +
                        "[data-quality], [data-resolution]"
                    ).forEach((el) => {
                        const blob = clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(blob);
                        if (quality && !values.includes(quality)) values.push(quality);
                    });
                    return values;
                }"""
            )
            for label in labels or []:
                normalized = normalize_quality_hint(str(label))
                if normalized and normalized not in discovered:
                    discovered.append(normalized)
        except Exception:
            continue

    order = {"4K": 0, "FHD": 1, "HD": 2, "SD": 3}
    discovered.sort(key=lambda value: order.get(value, 99))
    activated: list[str] = []

    for target in discovered[:8]:
        before_items = await collect_dom_stream_candidates(page)
        before_urls = {
            canonicalize_stream_url(url)
            for item in before_items
            for url in extract_stream_urls(item.get("url", ""))
            if canonicalize_stream_url(url)
        }
        clicked = False
        for frame in list(page.frames):
            try:
                clicked = bool(await frame.evaluate(
                    r"""async (target) => {
                        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                        const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 &&
                                style.display !== "none" && style.visibility !== "hidden";
                        };
                        const qualityOf = (value) => {
                            const text = clean(value);
                            if (/\b(4K|UHD|2160p?)\b/i.test(text)) return "4K";
                            if (/\b(Full\s*HD|FHD|1080p?)\b/i.test(text)) return "FHD";
                            if (/\b(HD|720p?)\b/i.test(text)) return "HD";
                            if (/\b(SD|480p?)\b/i.test(text)) return "SD";
                            return "";
                        };
                        const selector = "button, a, [role='button'], [role='option'], li, label, " +
                            "[data-quality], [data-resolution]";
                        const textOf = (el) => clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));

                        let nodes = Array.from(document.querySelectorAll(selector));
                        const menu = nodes.find((el) => visible(el) &&
                            /quality|chất lượng|độ phân giải/i.test(textOf(el)));
                        if (menu) {
                            try { menu.click(); await delay(350); } catch (_) {}
                        }
                        nodes = Array.from(document.querySelectorAll(selector));
                        const option = nodes.find((el) => {
                            if (!visible(el) || qualityOf(textOf(el)) !== target) return false;
                            if (el.tagName !== "A") return true;
                            const href = String(el.getAttribute("href") || "").trim();
                            return !href || href === "#" || href.startsWith("javascript:");
                        });
                        if (!option) return false;
                        try {
                            option.scrollIntoView({block: "center", inline: "center"});
                            option.click();
                            option.dispatchEvent(new MouseEvent("click", {
                                bubbles: true, cancelable: true, view: window,
                            }));
                            await delay(550);
                            return true;
                        } catch (_) {
                            return false;
                        }
                    }""",
                    target,
                ))
            except Exception:
                clicked = False

            if clicked:
                await page.wait_for_timeout(1100)
                await stimulate_player(page)
                after_items = await collect_dom_stream_candidates(page)
                new_count = 0
                for candidate in after_items:
                    extracted = extract_stream_urls(candidate.get("url", ""))
                    for raw_stream in extracted:
                        canonical = canonicalize_stream_url(raw_stream)
                        if not canonical or canonical in before_urls:
                            continue
                        capture_callback(
                            canonical,
                            f"quality/{target}",
                            frame_url=candidate.get("frame_url", ""),
                            quality=target or candidate.get("quality", ""),
                        )
                        new_count += 1
                # Network event handlers vẫn bắt nguồn nếu player tái sử dụng URL cũ.
                if new_count == 0:
                    print(
                        f"   ℹ️ Đã bấm {target} nhưng DOM chưa xuất hiện URL mới; "
                        "chờ request/response của player.",
                        flush=True,
                    )
                activated.append(target)
                break

    return activated


async def read_match_metadata(
    page: Page,
    match_url: str,
    match_name: str = "",
    blv_slug: str = "",
) -> dict[str, Any]:
    """Đọc metadata Hygenie từ vùng hiển thị của trận, không lấy giờ SEO/UTC ở cuối trang."""
    try:
        data = await page.evaluate(
            r"""({matchName, blvId}) => {
                const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                const absolute = (v) => { try { return new URL(v, location.href).href; } catch (_) { return ""; } };
                const h1 = clean(document.querySelector("h1")?.innerText || "");
                const headingMatch = h1.match(/link\s+trực\s+tiếp\s+trận\s+(.+?)\s+ngày\s+(\d{1,2}-\d{1,2}-\d{4})\s*[-–—]\s*(\d{1,2}:\d{2})/i);
                let title = headingMatch ? clean(headingMatch[1]) : clean(matchName);
                let date = headingMatch ? headingMatch[2].replace(/-/g, "/") : "";
                let time = headingMatch ? headingMatch[3] : "";

                const splitTeams = (value) => {
                    const text = clean(value)
                        .replace(/^link\s+trực\s+tiếp\s+trận\s+/i, "")
                        .replace(/\s+ngày\s+\d{1,2}-\d{1,2}-\d{4}.*$/i, "");
                    let parts = text.split(/\s+vs\s+/i);
                    if (parts.length !== 2) parts = text.split(/\s+[–—]\s+/);
                    if (parts.length !== 2) parts = text.split(/\s+-\s+/);
                    return parts.length === 2 ? parts.map(clean) : ["", ""];
                };
                let [homeName, awayName] = splitTeams(title);

                const logoItems = [];
                document.querySelectorAll("img").forEach((img) => {
                    const alt = clean(img.alt || img.title || "");
                    const m = alt.match(/^logo\s+(.+)$/i);
                    if (!m) return;
                    const url = absolute(img.currentSrc || img.src || img.getAttribute("data-src") || "");
                    if (url) logoItems.push({name: clean(m[1]), url, alt});
                });
                const norm = (v) => clean(v).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
                const bestLogo = (team, index) => {
                    const n = norm(team);
                    return (logoItems.find((item) => norm(item.name) === n) ||
                            logoItems.find((item) => n && (norm(item.name).includes(n) || n.includes(norm(item.name)))) ||
                            logoItems[index] || {}).url || "";
                };
                const homeLogo = bestLogo(homeName, 0);
                const awayLogo = bestLogo(awayName, 1);

                // Fallback giờ/ngày chỉ tìm quanh đúng cặp logo hoặc vùng đầu trang; không quét toàn body.
                if (!time || !date) {
                    const anchorImg = Array.from(document.querySelectorAll("img")).find((img) =>
                        /^logo\s+/i.test(clean(img.alt || img.title || ""))
                    );
                    let node = anchorImg;
                    let localText = "";
                    for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                        const txt = clean(node.innerText || node.textContent || "");
                        if (/\d{1,2}:\d{2}/.test(txt) && txt.length < 1200) { localText = txt; break; }
                    }
                    const source = localText || clean(document.querySelector("main")?.innerText || "").slice(0, 2500);
                    if (!time) time = (source.match(/(?<!\d)([01]?\d|2[0-3]):[0-5]\d(?!\d)/) || [""])[0];
                    if (!date) {
                        const dm = source.match(/(?<!\d)(\d{1,2})[\/-](\d{1,2})(?:[\/-](\d{4}))?(?!\d)/);
                        if (dm) date = `${dm[1].padStart(2,"0")}/${dm[2].padStart(2,"0")}${dm[3] ? "/"+dm[3] : ""}`;
                    }
                }

                let blv = "";
                const selectors = [
                    `[data-blv="${CSS.escape(blvId || "")}"]`,
                    `a[href*="blv=${encodeURIComponent(blvId || "")}"]`,
                    "[class*='blv'].active", "[class*='commentator'].active",
                    "[aria-selected='true'][class*='blv']"
                ];
                for (const selector of selectors) {
                    if (!blvId && selector.includes("CSS.escape")) continue;
                    let el = null;
                    try { el = document.querySelector(selector); } catch (_) {}
                    const value = clean(el?.innerText || el?.textContent || el?.getAttribute?.("data-name") || "");
                    if (value && value.length <= 80) { blv = value; break; }
                }
                if (!blv) {
                    const topText = clean(document.body?.innerText || "").slice(0, 4500);
                    const bm = topText.match(/(?:BLV|Bình\s*luận\s*viên)\s*[:\-–—]?\s*([^|•\n]{2,40})/i);
                    if (bm) blv = clean(bm[1]);
                }

                const iframeUrls = Array.from(document.querySelectorAll("iframe[src]"))
                    .map((el) => absolute(el.getAttribute("src"))).filter(Boolean);
                const qualitySources = [];
                const seen = new Set();
                const add = (value, context="") => {
                    const url = absolute(value);
                    if (!url || seen.has(url)) return;
                    if (!/\.m3u8(?:[?#]|$)|\.flv(?:[?#]|$)/i.test(url)) return;
                    seen.add(url); qualitySources.push({url, quality: "", context: clean(context)});
                };
                document.querySelectorAll("source[src],video[src],a[href],[data-url],[data-src],[data-stream],[data-file]").forEach((el) => {
                    ["src","href","data-url","data-src","data-stream","data-file"].forEach((attr) => add(el.getAttribute(attr), el.innerText || el.className));
                });
                const league = clean(Array.from(document.querySelectorAll("h2,h3,[class*='league'],[class*='tournament']"))
                    .map((el) => el.innerText || el.textContent).find((v) => /world\s*cup|cup|league|giải/i.test(clean(v))) || "");
                return {
                    title, home_name: homeName, away_name: awayName,
                    home_logo: homeLogo, away_logo: awayLogo,
                    logos: [homeLogo, awayLogo].filter(Boolean),
                    logo_candidates: [
                        homeLogo ? {url: homeLogo, score: 130, context: `Logo ${homeName}`, source: "hygenie-home"} : null,
                        awayLogo ? {url: awayLogo, score: 120, context: `Logo ${awayName}`, source: "hygenie-away"} : null,
                    ].filter(Boolean),
                    date, time, time_text: `${date} ${time}`.trim(), blv, blv_id: blvId,
                    iframe_urls: iframeUrls, quality_sources: qualitySources,
                    sport_text: league || title
                };
            }""",
            {"matchName": match_name, "blvId": blv_slug},
        )
        for key in ("home_logo", "away_logo"):
            data[key] = absolute_url(str(data.get(key, "")), match_url) if data.get(key) else ""
        data["logos"] = [absolute_url(str(v), match_url) for v in data.get("logos", []) if v]
        for item in data.get("logo_candidates", []) or []:
            if isinstance(item, dict):
                item["url"] = absolute_url(str(item.get("url", "")), match_url)
        data["iframe_urls"] = [absolute_url(str(v), match_url) for v in data.get("iframe_urls", []) if v]
        return data
    except Exception as exc:
        return {
            "title": "", "home_name": "", "away_name": "", "home_logo": "", "away_logo": "",
            "date": "", "time": "", "time_text": "", "logos": [], "logo_candidates": [],
            "iframe_urls": [], "quality_sources": [], "sport_text": "", "blv": "", "blv_id": blv_slug,
            "metadata_error": f"{type(exc).__name__}: {exc}",
        }

async def _http_fetch_text(
    context: BrowserContext,
    url: str,
    referer: str,
) -> tuple[int, dict[str, str], str, str]:
    try:
        response = await context.request.get(
            url,
            headers={
                "User-Agent": UA,
                "Referer": referer or TARGET_URL,
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=HTTP_DISCOVERY_TIMEOUT_SECONDS * 1000,
            fail_on_status_code=False,
        )
        body = await response.body()
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        content_type = headers.get("content-type", "")
        if len(body) > 3_000_000:
            body = body[:3_000_000]
        encoding = "utf-8"
        match = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type)
        if match:
            encoding = match.group(1)
        return response.status, headers, body.decode(encoding, errors="ignore"), response.url
    except Exception as exc:
        return 0, {}, f"__HTTP_ERROR__:{type(exc).__name__}:{exc}", url


async def discover_http_candidates(
    context: BrowserContext,
    match: dict[str, Any],
    capture_callback: Any,
) -> int:
    if not HYBRID_HTTP_FIRST:
        return 0
    queue: list[tuple[str, str, int]] = [(match.get("url", ""), match.get("url", ""), 0)]
    visited: set[str] = set()
    discovered_before = 0
    followed = 0
    while queue and followed <= HTTP_DISCOVERY_MAX_FOLLOWS:
        url, referer, depth = queue.pop(0)
        if not url or url in visited:
            continue
        visited.add(url)
        followed += 1
        status, headers, text, final_url = await _http_fetch_text(context, url, referer)
        if status == 0:
            match.setdefault("errors", []).append(text.removeprefix("__HTTP_ERROR__:"))
            continue
        if depth == 0 and not match.get("blv"):
            blv_match = re.search(
                r"(?i)(?:\bBLV\b|bình\s*luận\s*viên)\s*[:\-]?\s*([A-Za-zÀ-ỹ0-9 _.-]{2,32})",
                clean_text(re.sub(r"<[^>]+>", " ", text)),
            )
            if blv_match:
                match["blv"] = normalize_blv_name(blv_match.group(1))
        base_url = final_url or url
        refs = extract_explicit_references(text, base_url)
        for ref in refs:
            raw = ref.get("url", "")
            kind = ref.get("kind", "reference")
            if not raw:
                continue
            capture_callback(
                raw,
                f"http/{kind}",
                headers={
                    "referer": base_url if depth else match.get("url", ""),
                    "user-agent": UA,
                    "origin": headers.get("origin", ""),
                },
                frame_url=base_url,
                status=None,
                content_type=headers.get("content-type", "") if kind == "stream" else "",
            )
            discovered_before += 1
            lower = raw.lower()
            if (
                depth < 1
                and kind in {"iframe", "reference"}
                and raw.startswith(("http://", "https://"))
                and any(marker in lower for marker in ("embed", "player", "live", "stream"))
                and ".m3u8" not in lower
                and ".flv" not in lower
            ):
                queue.append((raw, base_url, depth + 1))
    return discovered_before


async def fetch_stream(
    context: BrowserContext,
    match: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        match_name, time_str, blv_from_link = derive_match_info(
            match["url"], match.get("raw_title", ""), match.get("raw_time", "")
        )
        url_date, url_time = extract_hygenie_datetime_from_url(match["url"])
        match["match_name"] = match_name
        match["date"] = match.get("date") or url_date
        match["time"] = match.get("time") or time_str or url_time
        match["blv"] = blv_from_link
        match["blv_id"] = (parse_qs(urlparse(match["url"]).query).get("blv") or [""])[0]
        match["streams"] = []
        match["stream_urls"] = []
        match["errors"] = []
        match["sport_group"] = classify_sport(
            match.get("sport_hint", ""),
            match.get("card_text", ""),
            match.get("raw_title", ""),
            match.get("url", ""),
            default=match.get("sport_group", "Bóng đá"),
        )

        scan_index = int(match.get("_scan_index", 0))
        scan_total = int(match.get("_scan_total", 0))
        prefix = f"[{scan_index}/{scan_total}] " if scan_index and scan_total else ""
        print(
            f"-> {prefix}Đang quét [{match['sport_group']}]: {match_name[:90]}",
            flush=True,
        )
        stream_map: dict[str, dict[str, Any]] = {}
        first_stream_at: float | None = None
        rate_limit_urls: set[str] = set()
        response_body_tasks: set[asyncio.Task[Any]] = set()

        def capture_url(
            raw_url: str,
            source: str,
            headers: dict[str, str] | None = None,
            frame_url: str = "",
            status: int | None = None,
            content_type: str = "",
            quality: str = "",
            parent_url: str = "",
        ) -> None:
            nonlocal first_stream_at
            normalized_headers = {
                str(k).lower(): str(v) for k, v in (headers or {}).items()
            }
            hint = stream_referer_hint(raw_url, frame_url)

            for stream_url in extract_stream_urls(raw_url, content_type):
                normalized = canonicalize_stream_url(stream_url)
                entry = stream_map.setdefault(
                    normalized,
                    {
                        "url": normalized,
                        "referer": "",
                        "origin": "",
                        "user_agent": "",
                        "status": None,
                        "statuses": [],
                        "content_type": "",
                        "sources": [],
                        "quality": "",
                        "parent_url": "",
                    },
                )

                referer = normalize_playback_referer(
                    normalized_headers.get("referer", "") or hint
                )
                origin = normalized_headers.get("origin", "")
                if not origin and referer:
                    parsed_ref = urlparse(referer)
                    allowed_hosts = {urlparse(value).netloc.lower() for value in HOME_URLS}
                    if any(parsed_ref.netloc.lower() == item or parsed_ref.netloc.lower().endswith("." + item) for item in allowed_hosts if item):
                        origin = f"{parsed_ref.scheme}://{parsed_ref.netloc}"
                user_agent = normalized_headers.get("user-agent", "") or UA

                if referer:
                    entry["referer"] = referer
                if origin:
                    entry["origin"] = origin
                if user_agent:
                    entry["user_agent"] = user_agent
                if status is not None:
                    entry["status"] = status
                    if status not in entry["statuses"]:
                        entry["statuses"].append(status)
                if content_type:
                    entry["content_type"] = content_type
                normalized_quality = normalize_quality_hint(quality or raw_url)
                if normalized_quality:
                    entry["quality"] = normalized_quality
                if parent_url:
                    entry["parent_url"] = parent_url
                if source not in entry["sources"]:
                    entry["sources"].append(source)

                if first_stream_at is None:
                    first_stream_at = time.monotonic()
                if len(entry["sources"]) == 1:
                    print(f"   🎯 [{source}] {normalized}")

        http_reference_count = await discover_http_candidates(context, match, capture_url)
        if http_reference_count:
            print(
                f"   ⚡ HTTP-first phát hiện {len(stream_map)} URL media từ "
                f"{http_reference_count} tham chiếu player; chưa cần mở tab Chromium.",
                flush=True,
            )

        if stream_map:
            early_streams, early_rejected = await finalize_stream_map(
                context, stream_map, match, log_prefix="HTTP-first: "
            )
            verified_count = sum(
                1 for entry in early_streams if entry.get("playability") == "verified"
            )
            delta = match.get("minutes_to_kickoff")
            enough = verified_count >= MAX_OUTPUT_STREAMS_PER_MATCH
            far_with_result = isinstance(delta, int) and delta > DELTA_NEAR_MINUTES and bool(early_streams)
            if enough or far_with_result:
                match["scan_decision"] = "http-first-complete"
                match["rejected_streams"] = early_rejected
                match["streams"] = early_streams
                match["stream_urls"] = [item["url"] for item in early_streams]
                print(
                    f"   🚀 Dừng sớm HTTP-first: verified={verified_count}, "
                    f"đầu ra={len(early_streams)}; không mở Chromium.",
                    flush=True,
                )
                return match

        delta = match.get("minutes_to_kickoff")
        if isinstance(delta, int) and delta > DELTA_NEAR_MINUTES and not stream_map:
            match["scan_decision"] = "http-only-far-upcoming"
            print(
                f"   ⏭️ Trận còn {delta} phút và HTTP chưa lộ stream; "
                "bỏ qua Chromium, sẽ kiểm tra lại theo lịch delta.",
                flush=True,
            )
            return match

        match["scan_decision"] = "browser-fallback"
        page = await context.new_page()
        await install_route_filter(page, homepage=False)

        async def inspect_response_body(response: Any) -> None:
            try:
                content_type = (response.headers.get("content-type", "") or "").lower()
                content_length = response.headers.get("content-length", "")
                if content_length and int(content_length) > 2_500_000:
                    return
                kind = stream_kind(response.url, content_type)
                textual = any(marker in content_type for marker in (
                    "json", "javascript", "text/", "mpegurl", "xml"
                )) or kind == "m3u8"
                if not textual:
                    return
                body = await response.body()
                if not body or len(body) > 2_500_000:
                    return
                text = body.decode("utf-8", errors="ignore")
                request = response.request
                try:
                    frame_url = request.frame.url if request.frame else ""
                except Exception:
                    frame_url = ""
                # Không quét URL bằng regex trong mọi JSON/JS/HTML nữa. Trang player
                # chứa danh sách cấu hình chung của nhiều BLV/kênh, khiến mỗi trận bị gán
                # hàng chục link không liên quan. Chỉ tách variant khi response chính là HLS.
                if kind == "m3u8" or "mpegurl" in content_type:
                    for variant in parse_hls_variants(text, response.url):
                        capture_url(
                            variant["url"], "hls/variant", headers=request.headers,
                            frame_url=frame_url, content_type="application/vnd.apple.mpegurl",
                            quality=variant.get("quality", ""),
                            parent_url=variant.get("parent_url", ""),
                        )
            except Exception:
                return

        def track_response_body(response: Any) -> None:
            task = asyncio.create_task(inspect_response_body(response))
            response_body_tasks.add(task)
            task.add_done_callback(response_body_tasks.discard)

        def handle_request(request: Any) -> None:
            try:
                frame_url = request.frame.url if request.frame else ""
            except Exception:
                frame_url = ""
            capture_url(
                request.url,
                f"request/{request.resource_type}",
                headers=request.headers,
                frame_url=frame_url,
            )

        def handle_response(response: Any) -> None:
            try:
                if response.status == 429 and response.url not in rate_limit_urls:
                    rate_limit_urls.add(response.url)
                    match["errors"].append(
                        f"HTTP 429 (tiếp tục quét, không restart): {response.url}"
                    )
                    print(f"   ⚠️ HTTP 429 nhưng vẫn tiếp tục quét full: {response.url}")

                req = response.request
                frame_url = req.frame.url if req.frame else ""
                content_type = response.headers.get("content-type", "")
                if stream_kind(response.url, content_type) == "m3u8" or "mpegurl" in content_type.lower():
                    track_response_body(response)
                capture_url(
                    response.url,
                    "response",
                    headers=req.headers,
                    frame_url=frame_url,
                    status=response.status,
                    content_type=content_type,
                )
            except Exception:
                capture_url(response.url, "response", status=response.status)

        def handle_page_error(error: Any) -> None:
            match["errors"].append(f"JS: {error}")

        def handle_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                text = str(message.text)
                if len(text) <= 500:
                    match["errors"].append(f"console/{message.type}: {text}")

        page.on("request", handle_request)
        page.on("response", handle_response)
        page.on("pageerror", handle_page_error)
        page.on("console", handle_console)

        try:
            await page.goto(match["url"], wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1200)

            blv_slug = (parse_qs(urlparse(match["url"]).query).get("blv") or [""])[0]
            metadata = await read_match_metadata(
                page, match["url"], match.get("match_name", ""), blv_slug
            )
            if metadata.get("home_name") and metadata.get("away_name"):
                match["match_name"] = f"{clean_text(metadata['home_name'])} vs {clean_text(metadata['away_name'])}"
            elif metadata.get("title"):
                better_name = clean_match_name(metadata["title"], match["url"])
                if re.search(r"\bvs\b", better_name, re.I):
                    match["match_name"] = better_name
            match["date"] = metadata.get("date") or match.get("date", "")
            match["time"] = metadata.get("time") or match.get("time") or extract_time(metadata.get("time_text", ""))
            match["blv_id"] = metadata.get("blv_id") or match.get("blv_id", "")
            if metadata.get("blv"):
                match["blv"] = normalize_blv_name(metadata.get("blv", "")) or match.get("blv", "")
            if not match.get("blv"):
                match["blv"] = BLV_ID_ALIASES.get(str(match.get("blv_id", "")), "")
            match["home_logo"] = metadata.get("home_logo") or match.get("home_logo", "")
            match["away_logo"] = metadata.get("away_logo") or match.get("away_logo", "")

            match["sport_group"] = classify_sport(
                match.get("sport_hint", ""),
                metadata.get("sport_text", ""),
                match.get("card_text", ""),
                match.get("match_name", ""),
                match.get("url", ""),
                default=match.get("sport_group", "Bóng đá"),
            )

            logo_candidates: list[Any] = list(match.get("logo_candidates") or [])
            logo_candidates.extend(match.get("team_logos") or [])
            if match.get("logo"):
                logo_candidates.append(match["logo"])
            logo_candidates.extend(metadata.get("logo_candidates") or [])
            logo_candidates.extend(metadata.get("logos") or [])
            match["logo_candidates"] = logo_candidates
            match["team_logos"] = [
                item.get("url", "") if isinstance(item, dict) else absolute_url(str(item), match["url"])
                for item in logo_candidates if item
            ]
            match["logo"] = match.get("home_logo") or choose_logo(
                logo_candidates, match["url"], match.get("match_name", "")
            )

            for hinted_url in match.get("stream_hints") or []:
                capture_url(
                    str(hinted_url),
                    "home-card/stream-hint",
                    frame_url=match.get("url", ""),
                )

            for iframe_url in metadata.get("iframe_urls") or []:
                iframe_blv = extract_blv_from_url(iframe_url)
                if iframe_blv and not match.get("blv"):
                    match["blv"] = iframe_blv
                capture_url(iframe_url, "iframe/src", frame_url=iframe_url)

            for source_info in metadata.get("quality_sources") or []:
                if not isinstance(source_info, dict):
                    continue
                capture_url(
                    str(source_info.get("url") or ""),
                    "metadata/quality-source",
                    frame_url=match["url"],
                    quality=str(source_info.get("quality") or ""),
                )

            for previous in match.get("_previous_streams") or []:
                if not isinstance(previous, dict):
                    continue
                capture_url(
                    str(previous.get("url") or ""),
                    "previous-playlist",
                    headers={
                        "referer": str(previous.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"),
                        "origin": str(previous.get("origin") or PLAYER_ORIGIN_FALLBACK),
                        "user-agent": str(previous.get("user_agent") or UA),
                    },
                    frame_url=match["url"],
                )

            match_wait_seconds = effective_stream_wait_seconds(match)
            allow_quality_scan = should_probe_quality_buttons(match, bool(stream_map))
            if match_wait_seconds < STREAM_WAIT_SECONDS:
                print(
                    f"   ⚡ Trận còn xa giờ đá; quét nhanh {match_wait_seconds}s "
                    f"thay vì {STREAM_WAIT_SECONDS}s",
                    flush=True,
                )

            activated_qualities: list[str] = []
            if allow_quality_scan:
                activated_qualities = await scan_quality_variants(page, capture_url)
                if activated_qualities:
                    print(
                        "   🎛️ Đã lần lượt thử các mức chất lượng: "
                        + ", ".join(activated_qualities),
                        flush=True,
                    )
                else:
                    quality_clicks = await stimulate_quality_variants(page)
                    if quality_clicks:
                        print(
                            f"   🎛️ Đã thử fallback {quality_clicks} nút/tuỳ chọn chất lượng",
                            flush=True,
                        )
            else:
                print(
                    "   ⏭️ Bỏ qua thao tác đổi FHD/HD vì trận còn xa và player chưa lộ stream",
                    flush=True,
                )

            deadline = time.monotonic() + match_wait_seconds
            quality_retry_done = False
            while time.monotonic() < deadline:
                await stimulate_player(page)
                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(
                        candidate["url"],
                        f"dom/{candidate.get('source', 'candidate')}",
                        frame_url=candidate.get("frame_url", ""),
                        quality=candidate.get("quality", ""),
                    )

                if (
                    not FULL_SCAN
                    and first_stream_at is not None
                    and time.monotonic() - first_stream_at >= EXTRA_WAIT_AFTER_FIRST_STREAM
                ):
                    break
                elapsed = match_wait_seconds - max(0.0, deadline - time.monotonic())
                if allow_quality_scan and not quality_retry_done and elapsed >= max(5, match_wait_seconds * 0.55):
                    quality_retry_done = True
                    retry_qualities = await scan_quality_variants(page, capture_url)
                    if retry_qualities:
                        print(
                            "   🔁 Quét lại nguồn sau khi đổi chất lượng: "
                            + ", ".join(retry_qualities),
                            flush=True,
                        )
                await page.wait_for_timeout(1000)

        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            match["errors"].append(error_text)
            print(f"   ❌ {match_name[:70]} | {error_text}")
        finally:
            if response_body_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*list(response_body_tasks), return_exceptions=True),
                        timeout=6,
                    )
                except Exception:
                    pass
            try:
                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(
                        candidate["url"],
                        f"dom/{candidate.get('source', 'final')}",
                        frame_url=candidate.get("frame_url", ""),
                        quality=candidate.get("quality", ""),
                    )
            except Exception:
                pass
            await page.close()

        match["streams"], match["rejected_streams"] = await finalize_stream_map(
            context, stream_map, match
        )
        match["stream_urls"] = [item["url"] for item in match["streams"]]

        if match["streams"]:
            verified_count = sum(
                1 for entry in match["streams"]
                if entry.get("playability") == "verified"
            )
            fallback_count = len(match["streams"]) - verified_count
            print(
                f"   📌 Kết quả cuối: verified={verified_count} | "
                f"fallback={fallback_count} | rejected={len(match['rejected_streams'])}",
                flush=True,
            )
            for entry in match["streams"]:
                probe = entry.get("probe") or {}
                print(
                    f"   ✅ Stream {entry.get('playability', 'unknown')} | "
                    f"HTTP={probe.get('status') or entry.get('status') or 'N/A'} | "
                    f"referer={entry.get('referer', '')} | "
                    f"logo={'có' if match.get('logo') else 'không'} | "
                    f"BLV={match.get('blv') or 'không rõ'} | "
                    f"chất lượng={entry.get('quality') or 'không rõ'} | "
                    f"lịch={match.get('date') or 'không rõ'} {match.get('time') or 'không rõ'}"
                )
        else:
            print(f"   ⚠️ Không có stream đủ tin cậy: {match_name[:85]}")

        return match


async def collect_home_links(context: BrowserContext, home_url: str = TARGET_URL) -> list[dict[str, Any]]:
    page = await context.new_page()
    await install_route_filter(page, homepage=True)
    print(f"👉 Đang mở trang chủ Lương Sơn: {home_url}")
    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(HOME_WAIT_MS)

        # Ưu tiên tab Tất cả và bấm nút tải thêm nếu có; không để tab cuối làm mất trận.
        try:
            await page.evaluate(r"""async () => {
                const clean = (v) => String(v || "").replace(/\s+/g," ").trim();
                const visible = (el) => { const r=el.getBoundingClientRect(); return r.width>0&&r.height>0; };
                const all = Array.from(document.querySelectorAll("button,a,[role='tab']"))
                    .find((el) => visible(el) && /^tất\s*cả$/i.test(clean(el.innerText || el.textContent)));
                if (all) { all.click(); await new Promise((r)=>setTimeout(r,700)); }
                for (let i=0;i<8;i++) {
                    const more = Array.from(document.querySelectorAll("button,a"))
                        .find((el) => visible(el) && /xem\s*thêm\s*trận|tải\s*thêm/i.test(clean(el.innerText || el.textContent)));
                    if (!more) break;
                    more.click(); await new Promise((r)=>setTimeout(r,550));
                }
            }""")
        except Exception:
            pass
        for _ in range(7):
            await page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight));")
            await page.wait_for_timeout(550)

        result = await page.evaluate(r"""({allowedHosts}) => {
            const clean = (v) => String(v || "").replace(/\s+/g," ").trim();
            const abs = (v) => { try { return new URL(v, location.href).href; } catch (_) { return ""; } };
            const hosts = new Set([location.hostname, ...allowedHosts].map((v)=>String(v||"").toLowerCase()).filter(Boolean));
            const hostAllowed = (host) => [...hosts].some((item)=>host===item || host.endsWith("."+item));
            const isMatch = (v) => { try {
                const u = new URL(v, location.href);
                return hostAllowed(u.hostname.toLowerCase()) && u.pathname.includes("/truc-tiep/");
            } catch (_) { return false; } };
            const seen = new Set(); const items=[];
            const getContainer = (a) => {
                let node=a;
                for(let i=0;node&&i<9;i++,node=node.parentElement){
                    const links=Array.from(node.querySelectorAll?.("a[href]")||[]).map(x=>abs(x.href)).filter(isMatch);
                    const unique=[...new Set(links)];
                    if(unique.length===1 && clean(node.innerText||"").length<2500) return node;
                }
                return a.closest("article,li,[class*='match'],[class*='event'],[class*='card']")||a.parentElement||a;
            };
            const add=(href,scope,title="")=>{
                href=abs(href); if(!isMatch(href)||seen.has(href))return; seen.add(href);
                const text=clean(scope?.innerText||scope?.textContent||title);
                const mediaText=String(scope?.innerHTML||"").replace(/\\\//g,"/");
                const streamHints=[...new Set(mediaText.match(/https?:\/\/[^"' <>\n\r]+?(?:\.m3u8|\.flv)(?:\?[^"' <>\n\r]*)?/gi)||[])].slice(0,12);
                const imgs=Array.from(scope?.querySelectorAll?.("img")||[]).map((img)=>({
                    url:abs(img.currentSrc||img.src||img.getAttribute("data-src")||""),
                    alt:clean(img.alt||img.title||"")
                })).filter(x=>x.url&&/^logo\s+/i.test(x.alt));
                const names=imgs.map(x=>x.alt.replace(/^logo\s+/i,"")).filter(Boolean);
                const path=decodeURIComponent(new URL(href).pathname);
                const tm=path.match(/-vao-luc-(\d{2})(\d{2})-(\d{2})-(\d{2})-(\d{4})/i);
                const time=tm?`${tm[1]}:${tm[2]}`:((text.match(/(?<!\d)([01]?\d|2[0-3]):[0-5]\d(?!\d)/)||[""])[0]);
                const date=tm?`${tm[3]}/${tm[4]}/${tm[5]}`:"";
                let rawTitle=names.length>=2?`${names[0]} vs ${names[1]}`:clean(title||text);
                items.push({
                    url:href, raw_title:rawTitle, card_text:text, raw_time:time,
                    date, time, home_name:names[0]||"", away_name:names[1]||"",
                    home_logo:imgs[0]?.url||"", away_logo:imgs[1]?.url||"",
                    logo:imgs[0]?.url||"", team_logos:imgs.map(x=>x.url),
                    logo_candidates:imgs.map((x,i)=>({url:x.url,score:120-i*10,context:x.alt,source:"hygenie-home-card"})),
                    sport_hint:text, stream_hints:streamHints
                });
            };
            document.querySelectorAll("a[href]").forEach((a)=>{
                if(!isMatch(a.href||a.getAttribute("href")))return;
                const scope=getContainer(a);
                add(a.href,scope,clean(a.innerText||a.title||a.getAttribute("aria-label")||""));
            });
            const html=(document.documentElement?.innerHTML||"").replace(/\\\//g,"/").replace(/&amp;/g,"&");
            (html.match(/(?:https?:\/\/[^"'<>\s]+)?\/truc-tiep\/[^"'<>\s?]+(?:\?[^"'<>\s]*)?/gi)||[]).forEach((href)=>add(href,null,""));
            return {items,diagnostics:{final_url:location.href,title:document.title||"",anchors:document.querySelectorAll("a[href]").length,html_length:html.length}};
        }""", {"allowedHosts": sorted({urlparse(value).hostname or "" for value in HOME_URLS if value})})

        links = list(result.get("items") or [])
        for item in links:
            item["source_home_url"] = home_url
        for item in links:
            item["sport_group"] = classify_sport(
                item.get("sport_hint", ""), item.get("card_text", ""),
                item.get("raw_title", ""), item.get("url", "")
            )
        diagnostics = result.get("diagnostics") or {}
        print(
            f"ℹ️ Lương Sơn: nguồn={home_url} | final={diagnostics.get('final_url','')} | title={diagnostics.get('title','')!r} | "
            f"anchors={diagnostics.get('anchors',0)} | html={diagnostics.get('html_length',0)} | trận={len(links)}"
        )
        if not links:
            Path(OUTPUT_HOME_DEBUG_HTML).write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=OUTPUT_HOME_DEBUG_PNG, full_page=True)
            print(f"⚠️ Đã lưu debug: {OUTPUT_HOME_DEBUG_HTML}, {OUTPUT_HOME_DEBUG_PNG}")
        return links
    except Exception as exc:
        print(f"❌ Không lấy được danh sách Hygenie: {type(exc).__name__}: {exc}")
        return []
    finally:
        await page.close()

async def collect_home_links_with_failover(context: BrowserContext) -> list[dict[str, Any]]:
    """Thử catbee.io rồi hygenie.io; bỏ qua trang redirect không có card /truc-tiep/."""
    attempts: list[tuple[str, int]] = []
    for home_url in HOME_URLS:
        links = await collect_home_links(context, home_url)
        attempts.append((home_url, len(links)))
        if links:
            print(f"✅ Chọn miền Lương Sơn: {home_url} | trận={len(links)}", flush=True)
            return links
        print(f"⚠️ Miền Lương Sơn không có card trận, thử miền tiếp theo: {home_url}", flush=True)
    print("❌ Không miền Lương Sơn nào trả được card trận: " + ", ".join(f"{url}={count}" for url, count in attempts), flush=True)
    return []


def escape_m3u_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value or "").replace('"', "'").strip()


def header_json(user_agent: str, referer: str, origin: str = "") -> str:
    values = {"User-Agent": user_agent}
    if referer:
        values["Referer"] = referer
    if origin:
        values["Origin"] = origin
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def escape_pipe_header(value: str) -> str:
    """Mã hóa giá trị protocol-option để URL không vỡ bởi khoảng trắng, &, | hoặc %."""
    return quote(clean_text(value), safe=":/().,;=-_")


def android_stream_url(stream_url: str, user_agent: str, referer: str, origin: str = "") -> str:
    """
    Header syntax understood by many Android IPTV players:
      URL|User-Agent=...&Referer=...

    Keep EXTVLCOPT too because TiviMate versions differ in which syntax they honor.
    """
    headers = [f"User-Agent={escape_pipe_header(user_agent)}"]
    if referer:
        headers.append(f"Referer={escape_pipe_header(referer)}")
    if origin:
        headers.append(f"Origin={escape_pipe_header(origin)}")
    return stream_url + "|" + "&".join(headers)


def write_outputs(results: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Tạo 3 playlist:
      - hygenie_live.m3u: playlist phổ thông, URL nguyên bản + EXTHTTP/EXTVLCOPT.
      - hygenie_live_pipe.m3u: biến thể Kodi-style URL|Header=Value.
      - hygenie_live_vlc.m3u: URL nguyên bản + EXTVLCOPT dành riêng VLC.

    Không gắn pipe headers vào playlist mặc định vì nhiều IPTV player Android
    coi phần sau dấu | là một phần URL và báo lỗi phát kênh.
    """
    resolve_duplicate_logos(results)

    universal_lines = ["#EXTM3U"]
    pipe_lines = ["#EXTM3U"]
    vlc_lines = ["#EXTM3U"]

    written_streams: set[str] = set()
    match_keys_with_streams: set[str] = set()
    count_links = 0

    sorted_results = sorted(
        results,
        key=lambda item: (
            SPORT_GROUP_RANK.get(item.get("sport_group", "Khác"), 999),
            item.get("date") or "99/99/9999",
            item.get("time") or "99:99",
            clean_text(item.get("match_name") or item.get("raw_title") or "").lower(),
        ),
    )

    group_stream_counts: Counter[str] = Counter()

    for result in sorted_results:
        streams = result.get("streams") or [
            {"url": value} for value in (result.get("stream_urls") or [])
        ]
        if not streams:
            continue

        match_name = result.get("match_name") or result.get("raw_title") or "Hygenie"
        date_str = result.get("date") or ""
        time_str = result.get("time") or ""
        blv = result.get("blv") or ""
        sport_group = result.get("sport_group") or classify_sport(
            result.get("sport_hint", ""),
            result.get("card_text", ""),
            match_name,
            result.get("url", ""),
        )
        if sport_group not in SPORT_GROUP_RANK:
            sport_group = "Khác"
        # resolve_duplicate_logos() đã chọn logo cuối cùng và loại logo dùng nhầm
        # cho nhiều trận. Không chấm lại ở đây vì có thể vô tình chọn lại logo lỗi.
        logo = result.get("logo", "")

        schedule = " ".join(part for part in (date_str, time_str) if part)
        display_base = f"[{schedule}] {match_name}" if schedule else match_name
        if blv and blv.lower() not in display_base.lower():
            display_base += f" [BLV {blv}]"
        display_base = escape_m3u_text(display_base)
        logo = escape_m3u_text(logo)

        unique_streams = [item for item in streams if item.get("url") not in written_streams]
        if not unique_streams:
            continue

        match_keys_with_streams.add(f"{match_name}|{blv}|{date_str}|{time_str}")
        for index, stream_info in enumerate(unique_streams, start=1):
            stream_url = decode_url_repeatedly(stream_info.get("url", ""))
            if not stream_url:
                continue
            written_streams.add(stream_url)

            display_name = display_base
            quality = normalize_quality_hint(stream_info.get("quality", ""))
            if len(unique_streams) > 1 and not quality:
                display_name += f" (Luồng {index})"

            referer = normalize_playback_referer(
                stream_info.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"
            )
            user_agent = clean_text(stream_info.get("user_agent") or UA)
            origin = clean_text(stream_info.get("origin") or PLAYER_ORIGIN_FALLBACK)
            kind = stream_kind(stream_url, stream_info.get("content_type", ""))
            if kind:
                suffix = f"{quality} {kind.upper()}" if quality else kind.upper()
                display_name += f" [{suffix}]"

            channel_id = channel_id_for(result, stream_url, index)
            attributes = (
                f'tvg-id="{escape_m3u_text(channel_id)}" '
                f'tvg-name="{escape_m3u_text(display_base)}" '
                f'group-title="{escape_m3u_text(sport_group)}"'
            )
            if logo:
                attributes += f' tvg-logo="{logo}"'
            extinf = f"#EXTINF:-1 {attributes},{display_name}"

            universal_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                f"#EXTHTTP:{header_json(user_agent, referer, origin)}",
                stream_url,
            ])

            pipe_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                f"#EXTHTTP:{header_json(user_agent, referer, origin)}",
                android_stream_url(stream_url, user_agent, referer, origin),
            ])

            vlc_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                stream_url,
            ])

            group_stream_counts[sport_group] += 1
            count_links += 1

    Path(OUTPUT_DEBUG).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output_sets = (
        (Path(OUTPUT_M3U), universal_lines, "phổ thông"),
        (Path(OUTPUT_PIPE_M3U), pipe_lines, "pipe/Kodi"),
        (Path(OUTPUT_VLC_M3U), vlc_lines, "VLC"),
    )

    if count_links:
        for path, lines, _label in output_sets:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        for path, _lines, label in output_sets:
            if path.exists():
                print(f"⚠️ Không có link mới; giữ nguyên playlist {label}: {path.resolve()}")
            else:
                path.write_text("#EXTM3U\n", encoding="utf-8")
                print(f"⚠️ Đã tạo playlist {label} rỗng: {path.resolve()}")

    if count_links:
        m3u8_count = sum(
            1 for line in vlc_lines if line.startswith("http") and stream_kind(line) == "m3u8"
        )
        flv_count = sum(
            1 for line in vlc_lines if line.startswith("http") and stream_kind(line) == "flv"
        )
        print(f"📊 Playlist: M3U8={m3u8_count} | FLV={flv_count}")
        group_summary = " | ".join(
            f"{group}={group_stream_counts[group]}"
            for group in SPORT_GROUP_ORDER if group_stream_counts[group]
        )
        if group_summary:
            print(f"📂 Thư mục playlist: {group_summary}")
        print(f"📺 Mặc định Android/IPTV: {Path(OUTPUT_M3U).resolve()}")
        print(f"📺 Pipe/Kodi tùy chọn: {Path(OUTPUT_PIPE_M3U).resolve()}")
        print(f"📺 VLC: {Path(OUTPUT_VLC_M3U).resolve()}")

    return len(match_keys_with_streams), count_links


async def progress_heartbeat(tasks: list[asyncio.Task[Any]], total: int) -> None:
    """In tiến trình đều đặn để GitHub Actions không đứng im trong lúc các tab đang chờ."""
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(5)
            completed = sum(task.done() for task in tasks)
            if completed >= total:
                return
            active = min(CONCURRENCY_LIMIT, total - completed)
            waiting = max(0, total - completed - active)
            elapsed = int(time.monotonic() - started)
            print(
                f"⏳ Tiến trình realtime: xong {completed}/{total} | "
                f"đang/chờ tối đa {active}/{waiting} | đã chạy {elapsed}s",
                flush=True,
            )
    except asyncio.CancelledError:
        return


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass

    print(f"🧞 KHỞI ĐỘNG LƯƠNG SƠN STREAM SCANNER - {SCANNER_VERSION}", flush=True)
    print(
        "ℹ️ Lệnh test riêng một trận (chỉ là hướng dẫn, không tự chạy):\n"
        '   python sources/luongson.py "URL_TRẬN_LƯƠNG_SƠN"'
    )
    print(
        f"ℹ️ Chế độ quét: {'FULL toàn bộ thời gian' if FULL_SCAN else 'dừng sớm'} | "
        f"định dạng={','.join(STREAM_EXTENSIONS)} | chờ mỗi trận={STREAM_WAIT_SECONDS}s | "
        f"lọc trận -{SCAN_PAST_MINUTES}/+{SCAN_FUTURE_MINUTES} phút | "
        f"xác minh phát thật={'BẬT' if VERIFY_STREAMS else 'TẮT'} | "
        f"tối đa {MAX_VERIFY_CANDIDATES} ứng viên/{MAX_OUTPUT_STREAMS_PER_MATCH} link đầu ra | "
        f"HTTP-first={'BẬT' if HYBRID_HTTP_FIRST else 'TẮT'} | delta={'BẬT' if DELTA_SCAN_ENABLED else 'TẮT'} | "
        f"miền dự phòng={','.join(HOME_URLS)}"
    )

    direct_urls = [
        arg.strip() for arg in sys.argv[1:]
        if arg.strip().startswith(("http://", "https://"))
    ]
    delta_state = load_delta_state(STATE_PATH) if DELTA_SCAN_ENABLED and not direct_urls else {}
    if delta_state:
        print(f"ℹ️ Delta state: đã nạp {len(delta_state)} trận; chỉ quét lại khi đến next_scan_at.", flush=True)
    previous_streams_by_match = load_previous_playlist_streams()
    if previous_streams_by_match:
        print(
            f"ℹ️ Đã nạp playlist cũ của {len(previous_streams_by_match)} trận để chống mất link đang chạy.",
            flush=True,
        )

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
            ],
        }
        configured_executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
        if configured_executable:
            executable_path = Path(configured_executable)
            if executable_path.is_file():
                launch_options["executable_path"] = str(executable_path)
                print(f"ℹ️ Dùng Chromium hệ thống: {executable_path}", flush=True)
            else:
                print(
                    f"⚠️ PLAYWRIGHT_CHROMIUM_EXECUTABLE không tồn tại: {executable_path}; "
                    "quay về Chromium do Playwright quản lý.",
                    flush=True,
                )
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=UA,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            ignore_https_errors=True,
            service_workers="block",
            extra_http_headers={
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        )

        if direct_urls:
            links = []
            for url in direct_urls:
                match_name, _, _ = derive_match_info(url)
                date_str, time_str = extract_hygenie_datetime_from_url(url)
                links.append({
                    "url": url,
                    "raw_title": match_name,
                    "raw_time": time_str,
                    "date": date_str,
                    "time": time_str,
                    "logo": "",
                    "team_logos": [],
                    "logo_candidates": [],
                    "sport_hint": "",
                    "sport_group": classify_sport(match_name, url),
                })
            print(f"✅ Chế độ test trực tiếp: {len(links)} URL.")
        else:
            links = await collect_home_links_with_failover(context)
            links, window_stats = filter_links_by_scan_window(links)
            print_scan_window_summary(window_stats)
            if DELTA_SCAN_ENABLED:
                due_links: list[dict[str, Any]] = []
                skipped_delta = 0
                for item in links:
                    key = match_key_from_url(item.get("url", ""))
                    due, reason = should_scan_now(
                        item, delta_state.get(key), near_minutes=DELTA_NEAR_MINUTES
                    )
                    item["delta_reason"] = reason
                    if due:
                        due_links.append(item)
                    else:
                        skipped_delta += 1
                links = due_links
                print(
                    f"🧠 Delta scan: đến lượt={len(links)} | hoãn={skipped_delta} | "
                    f"ngưỡng gần giờ={DELTA_NEAR_MINUTES} phút",
                    flush=True,
                )

        if not links:
            print("❌ Không tìm thấy link trận/phòng nào.")
            write_outputs([])
            await context.close()
            await browser.close()
            return

        for match in links:
            match_id = match_key_from_url(match.get("url", ""))
            match["_previous_streams"] = list(previous_streams_by_match.get(match_id, []))

        print(
            f"✅ Tìm thấy {len(links)} link trận/phòng. "
            f"Bắt đầu quét tối đa {CONCURRENCY_LIMIT} trang cùng lúc..."
        )
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        total_links = len(links)
        tasks: list[asyncio.Task[dict[str, Any]]] = []
        for index, match in enumerate(links, start=1):
            match["_scan_index"] = index
            match["_scan_total"] = total_links
            tasks.append(asyncio.create_task(fetch_stream(context, match, semaphore)))

        heartbeat = asyncio.create_task(progress_heartbeat(tasks, total_links))
        results: list[dict[str, Any]] = []
        completed = 0
        try:
            for future in asyncio.as_completed(tasks):
                result = await future
                results.append(result)
                completed += 1
                found = len(result.get("streams") or [])
                print(
                    f"📈 Hoàn thành {completed}/{total_links}: "
                    f"[{result.get('sport_group', 'Khác')}] "
                    f"{result.get('match_name', '')[:70]} | stream={found}",
                    flush=True,
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        pending_without_media = [
            row for row in results
            if isinstance(row.get("minutes_to_kickoff"), int)
            and 0 <= int(row.get("minutes_to_kickoff")) <= SCAN_FUTURE_MINUTES
            and not (row.get("streams") or [])
        ]
        if pending_without_media:
            print(
                f"ℹ️ Có {len(pending_without_media)} trận sắp đá trong cửa sổ nhưng trang chưa lộ "
                "URL M3U8/FLV; không đưa URL trang web vào M3U vì ứng dụng IPTV không phát được.",
                flush=True,
            )

        if DELTA_SCAN_ENABLED:
            update_state_from_results(delta_state, results, match_key_from_url)
            save_delta_state(STATE_PATH, delta_state, "luongson")
            print(f"💾 Đã cập nhật delta state: {STATE_PATH.resolve()}", flush=True)

        count_matches, count_links = write_outputs(results)

        if count_links:
            print(f"\n🎉 HOÀN TẤT: lấy được {count_links} link từ {count_matches} trận/phòng.")
            print(f"📺 Playlist mặc định: {Path(OUTPUT_M3U).resolve()}")
            print(f"📺 Playlist pipe/Kodi: {Path(OUTPUT_PIPE_M3U).resolve()}")
            print(f"📺 Playlist VLC: {Path(OUTPUT_VLC_M3U).resolve()}")
        else:
            print("\n❌ Không bắt được m3u8/flv nào.")
        print(f"🧾 Nhật ký chi tiết: {Path(OUTPUT_DEBUG).resolve()}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
