from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

VERSION = "4.3.0-HYBRID-DELTA-SINGLE-M3U"
TZ_VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
ALLOWED_GROUPS = {"Bóng đá", "Bóng rổ", "Bóng chuyền", "Tennis", "Esports", "Khác"}
GROUP_ORDER = {name: index for index, name in enumerate(("Bóng đá", "Bóng rổ", "Bóng chuyền", "Tennis", "Esports", "Khác"))}
PLAYABILITY_RANK = {
    "verified": 4,
    "browser-observed": 3,
    "upcoming-pending": 2,
}
QUALITY_RANK = {"4K": 5, "FHD": 4, "HD": 3, "SD": 2, "UNKNOWN": 1}


@dataclass(slots=True)
class SourceFiles:
    key: str
    label: str
    universal: Path
    pipe: Path
    vlc: Path
    debug: Path
    fresh: bool = True
    returncode: int = 0


@dataclass(slots=True)
class M3UBlock:
    source_key: str
    source_label: str
    extinf: str
    lines: list[str]
    url_line: str
    canonical_url: str
    attributes: dict[str, str]
    display_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: int = 0
    match_key: str = ""
    quality: str = "UNKNOWN"
    kind: str = ""
    playability: str = ""
    kickoff: datetime | None = None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_ascii(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def canonical_stream_url(value: str) -> str:
    raw = clean_text(value)
    if "|" in raw:
        raw = raw.split("|", 1)[0]
    raw = unquote(raw).replace("\\/", "/")
    # Các tham số của iframe từng bị nối nhầm sau đuôi stream.
    raw = re.sub(r"(?i)(\.m3u8|\.flv)&(?:autoplay|isHome)=.*$", r"\1", raw)
    return raw


def stream_kind(url: str) -> str:
    path = urlparse(canonical_stream_url(url)).path.lower()
    if ".m3u8" in path:
        return "m3u8"
    if ".flv" in path:
        return "flv"
    return ""


def parse_attributes(extinf: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def parse_m3u(path: Path, source_key: str, source_label: str) -> list[M3UBlock]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[M3UBlock] = []
    current: list[str] = []
    extinf = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            current = [line]
            extinf = line
            continue
        if not current:
            continue
        current.append(line)
        if stripped.startswith(("http://", "https://")):
            display = extinf.split(",", 1)[1] if "," in extinf else ""
            blocks.append(
                M3UBlock(
                    source_key=source_key,
                    source_label=source_label,
                    extinf=extinf,
                    lines=list(current),
                    url_line=line,
                    canonical_url=canonical_stream_url(line),
                    attributes=parse_attributes(extinf),
                    display_name=clean_text(display),
                )
            )
            current = []
            extinf = ""
    return blocks


def _parse_datetime_value(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ_VIETNAM)
        return parsed.astimezone(TZ_VIETNAM)
    except ValueError:
        return None


def resolve_kickoff(row: dict[str, Any], now: datetime) -> datetime | None:
    direct = _parse_datetime_value(row.get("kickoff_iso"))
    if direct:
        return direct
    date_text = clean_text(row.get("date"))
    time_text = clean_text(row.get("time"))
    time_match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", time_text)
    if not time_match:
        return None
    hour, minute = map(int, time_match.groups())
    date_candidates: list[datetime] = []
    if date_text:
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m", "%d-%m"):
            try:
                parsed = datetime.strptime(date_text, fmt)
                year = parsed.year if "%Y" in fmt else now.year
                date_candidates.append(datetime(year, parsed.month, parsed.day, hour, minute, tzinfo=TZ_VIETNAM))
                break
            except ValueError:
                continue
    if not date_candidates:
        for day_offset in (-1, 0, 1):
            day = (now + timedelta(days=day_offset)).date()
            date_candidates.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ_VIETNAM))
    return min(date_candidates, key=lambda dt: abs((dt - now).total_seconds()))


def normalize_quality(value: Any, display_name: str, url: str) -> str:
    text = " ".join((clean_text(value), display_name, url)).lower()
    if re.search(r"\b(4k|uhd|2160)\b", text):
        return "4K"
    if re.search(r"\b(fhd|full\s*hd|1080)\b", text) or re.search(r"(?i)(?:hd)(?:/playlist\.m3u8|\.flv)(?:$|\?)", url):
        return "FHD"
    if re.search(r"\b(hd|720)\b", text):
        return "HD"
    if re.search(r"\b(sd|480|360)\b", text):
        return "SD"
    return "UNKNOWN"


def normalize_match_name(value: str) -> str:
    text = clean_text(value)
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"\s*\[(?:CHỜ PHÁT\s+)?(?:4K|FHD|HD|SD)?\s*(?:M3U8|FLV)\]\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\[BLV\s+[^\]]+\]", "", text, flags=re.I)
    match = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+-\s+|$)", text, flags=re.I)
    if match:
        return f"{normalize_ascii(match.group(1))} vs {normalize_ascii(match.group(2))}"
    return normalize_ascii(text)


def extract_blv(row: dict[str, Any], display_name: str) -> str:
    value = clean_text(row.get("blv"))
    if value:
        return normalize_ascii(value)
    match = re.search(r"\[BLV\s+([^\]]+)\]", display_name, flags=re.I)
    return normalize_ascii(match.group(1)) if match else ""


def build_debug_index(debug_path: Path, now: datetime) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not debug_path.exists():
        return {}, []
    try:
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, []
    rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kickoff = resolve_kickoff(row, now)
        for stream in row.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            url = canonical_stream_url(stream.get("url", ""))
            if not url:
                continue
            merged = dict(row)
            merged.update(stream)
            merged["_kickoff"] = kickoff
            current = index.get(url)
            if current is None or PLAYABILITY_RANK.get(clean_text(merged.get("playability")), 0) > PLAYABILITY_RANK.get(clean_text(current.get("playability")), 0):
                index[url] = merged
    return index, rows


def enrich_blocks(source: SourceFiles, blocks: list[M3UBlock], now: datetime) -> tuple[list[M3UBlock], int]:
    debug_index, rows = build_debug_index(source.debug, now)
    for block in blocks:
        meta = dict(debug_index.get(block.canonical_url, {}))
        block.metadata = meta
        block.playability = clean_text(meta.get("playability"))
        block.kickoff = meta.get("_kickoff") if isinstance(meta.get("_kickoff"), datetime) else None
        block.quality = normalize_quality(meta.get("quality"), block.display_name, block.canonical_url)
        block.kind = stream_kind(block.canonical_url)
        match_name = clean_text(meta.get("match_name") or meta.get("raw_title") or block.display_name)
        blv = extract_blv(meta, block.display_name)
        block.match_key = f"{normalize_match_name(match_name)}|{blv}"
        observed = bool(meta.get("observed_active"))
        status = meta.get("http_status") or meta.get("status")
        status_bonus = 10 if status in {200, 206, "200", "206"} else 0
        block.score = (
            PLAYABILITY_RANK.get(block.playability, 0) * 100
            + QUALITY_RANK.get(block.quality, 1) * 10
            + (6 if block.kind == "m3u8" else 3 if block.kind == "flv" else 0)
            + (8 if observed else 0)
            + status_bonus
            + (5 if source.fresh else 0)
        )
    return blocks, len(rows)


def is_candidate_allowed(block: M3UBlock, now: datetime, upcoming_hours: int) -> bool:
    if block.playability == "verified":
        return True
    if block.playability == "browser-observed" and block.metadata.get("observed_active"):
        return True
    if block.playability in {"browser-observed", "upcoming-pending"} and block.kickoff:
        minutes = (block.kickoff - now).total_seconds() / 60
        return 0 <= minutes <= upcoming_hours * 60
    return False


def choose_candidates(blocks: Iterable[M3UBlock], now: datetime, max_per_match: int, upcoming_hours: int) -> tuple[list[M3UBlock], list[dict[str, Any]]]:
    best_by_url: dict[str, M3UBlock] = {}
    dropped: list[dict[str, Any]] = []
    for block in blocks:
        if not block.canonical_url or block.kind not in {"m3u8", "flv"}:
            dropped.append({"url": block.canonical_url, "reason": "not-stream", "source": block.source_key})
            continue
        if not is_candidate_allowed(block, now, upcoming_hours):
            dropped.append({"url": block.canonical_url, "reason": "not-verified-or-not-upcoming", "source": block.source_key})
            continue
        previous = best_by_url.get(block.canonical_url)
        if previous is None or block.score > previous.score:
            best_by_url[block.canonical_url] = block

    grouped: dict[str, list[M3UBlock]] = {}
    for block in best_by_url.values():
        grouped.setdefault(block.match_key or normalize_match_name(block.display_name), []).append(block)

    selected: list[M3UBlock] = []
    for match_key, items in grouped.items():
        items.sort(key=lambda item: (-item.score, item.source_key, item.canonical_url))
        qualities: set[str] = set()
        chosen: list[M3UBlock] = []
        for item in items:
            qkey = item.quality
            if qkey in qualities:
                dropped.append({"url": item.canonical_url, "reason": f"duplicate-quality-{qkey}", "source": item.source_key, "match_key": match_key})
                continue
            chosen.append(item)
            qualities.add(qkey)
            if len(chosen) >= max_per_match:
                break
        for item in items:
            if item not in chosen and not any(row.get("url") == item.canonical_url for row in dropped):
                dropped.append({"url": item.canonical_url, "reason": "per-match-cap", "source": item.source_key, "match_key": match_key})
        selected.extend(chosen)

    selected.sort(
        key=lambda item: (
            GROUP_ORDER.get(item.attributes.get("group-title", "Khác"), 999),
            item.kickoff or datetime.max.replace(tzinfo=TZ_VIETNAM),
            normalize_match_name(item.display_name),
            -item.score,
        )
    )
    return selected, dropped


def _block_map(path: Path, source: SourceFiles) -> dict[str, M3UBlock]:
    return {block.canonical_url: block for block in parse_m3u(path, source.key, source.label)}


def _write_variant(path: Path, selected: list[M3UBlock], maps: dict[str, dict[str, M3UBlock]], fallback_maps: dict[str, dict[str, M3UBlock]]) -> None:
    lines = ["#EXTM3U"]
    for item in selected:
        block = maps.get(item.source_key, {}).get(item.canonical_url) or fallback_maps.get(item.source_key, {}).get(item.canonical_url) or item
        lines.extend(block.lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_intermediate_playlists(root: Path) -> list[str]:
    """Xóa mọi playlist sinh tạm, chỉ giữ all_live.m3u làm đầu ra công khai."""
    keep = "all_live.m3u"
    removed: list[str] = []
    for path in root.glob("*.m3u"):
        if path.name == keep:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
    return sorted(removed)


def merge_sources(
    root: Path,
    sources: list[SourceFiles],
    *,
    now: datetime | None = None,
    max_per_match: int | None = None,
    upcoming_hours: int | None = None,
    preserve_on_empty: bool = True,
) -> dict[str, Any]:
    now = (now or datetime.now(TZ_VIETNAM)).astimezone(TZ_VIETNAM)
    max_per_match = max_per_match or max(1, min(int(os.getenv("MULTI_MAX_STREAMS_PER_MATCH", "2")), 6))
    upcoming_hours = upcoming_hours or max(1, min(int(os.getenv("MULTI_UPCOMING_KEEP_HOURS", "4")), 24))

    all_blocks: list[M3UBlock] = []
    universal_maps: dict[str, dict[str, M3UBlock]] = {}
    source_stats: list[dict[str, Any]] = []

    for source in sources:
        blocks = parse_m3u(source.universal, source.key, source.label)
        blocks, debug_rows = enrich_blocks(source, blocks, now)
        universal_maps[source.key] = {item.canonical_url: item for item in blocks}
        if source.returncode == 0 and debug_rows > 0:
            all_blocks.extend(blocks)
        source_stats.append({
            "key": source.key,
            "label": source.label,
            "returncode": source.returncode,
            "fresh": source.fresh,
            "debug_rows": debug_rows,
            "playlist_blocks": len(blocks),
            "included": source.returncode == 0 and debug_rows > 0,
        })

    selected, dropped = choose_candidates(all_blocks, now, max_per_match, upcoming_hours)
    outputs = {
        "playlist": root / "all_live.m3u",
        "debug": root / "all_live_debug.json",
    }

    if selected:
        _write_variant(outputs["playlist"], selected, universal_maps, universal_maps)
    elif not preserve_on_empty:
        outputs["playlist"].write_text("#EXTM3U\n", encoding="utf-8")

    channels = []
    for item in selected:
        channels.append({
            "source": item.source_key,
            "source_label": item.source_label,
            "url": item.canonical_url,
            "match_key": item.match_key,
            "display_name": item.display_name,
            "group": item.attributes.get("group-title", "Khác"),
            "quality": item.quality,
            "kind": item.kind,
            "playability": item.playability,
            "score": item.score,
            "kickoff_iso": item.kickoff.isoformat() if item.kickoff else None,
        })

    report = {
        "version": VERSION,
        "generated_at": now.isoformat(),
        "policy": {
            "max_streams_per_match_blv": max_per_match,
            "upcoming_keep_hours": upcoming_hours,
            "requires_verified_or_observed": True,
        },
        "sources": source_stats,
        "input_candidates": len(all_blocks),
        "selected_count": len(selected),
        "dropped_count": len(dropped),
        "channels": channels,
        "dropped": dropped,
        "outputs_written": bool(selected),
    }
    outputs["debug"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
