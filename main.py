from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from merger import SourceFiles, cleanup_intermediate_playlists, merge_sources

ROOT = Path(__file__).resolve().parent
VERSION = "4.3.0-HYBRID-DELTA-HTTP-FIRST-SINGLE-M3U"


@dataclass(slots=True)
class SourceConfig:
    key: str
    label: str
    script: Path
    universal: Path
    pipe: Path
    vlc: Path
    debug: Path
    host_markers: tuple[str, ...]


SOURCES = {
    "chuoichien": SourceConfig(
        key="chuoichien",
        label="Chuối Chiên",
        script=ROOT / "sources" / "chuoichien.py",
        universal=ROOT / "chuoichien_live.m3u",
        pipe=ROOT / "chuoichien_live_pipe.m3u",
        vlc=ROOT / "chuoichien_live_vlc.m3u",
        debug=ROOT / "chuoichien_debug.json",
        host_markers=("chuoichientv.me", "chuoichien.tv"),
    ),
    "luongson": SourceConfig(
        key="luongson",
        label="Lương Sơn",
        script=ROOT / "sources" / "luongson.py",
        universal=ROOT / "hygenie_live.m3u",
        pipe=ROOT / "hygenie_live_pipe.m3u",
        vlc=ROOT / "hygenie_live_vlc.m3u",
        debug=ROOT / "hygenie_debug.json",
        host_markers=("hygenie.io", "catbee.io"),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quét Chuối Chiên + Lương Sơn trong một lần chạy")
    parser.add_argument("urls", nargs="*", help="URL trận để test riêng; tự định tuyến theo tên miền")
    parser.add_argument("--source", choices=("all", "chuoichien", "luongson"), default="all")
    parser.add_argument("--merge-only", action="store_true", help="Không quét web, chỉ gộp các kết quả hiện có")
    return parser.parse_args()


def route_urls(urls: Iterable[str]) -> dict[str, list[str]]:
    routed = {key: [] for key in SOURCES}
    for value in urls:
        host = (urlparse(value).hostname or "").lower()
        matched = False
        for key, config in SOURCES.items():
            if any(marker in host for marker in config.host_markers):
                routed[key].append(value)
                matched = True
                break
        if not matched:
            raise ValueError(f"Không nhận diện được nguồn của URL: {value}")
    return routed


def file_stamp(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def run_source(config: SourceConfig, direct_urls: list[str]) -> tuple[int, bool, float]:
    before = file_stamp(config.debug)
    command = [sys.executable, "-u", str(config.script), *direct_urls]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    started = time.monotonic()
    print(f"\n{'=' * 72}", flush=True)
    print(f"🚀 BẮT ĐẦU NGUỒN {config.label.upper()} | lệnh: {' '.join(command)}", flush=True)
    print(f"{'=' * 72}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(f"[{config.label}] {line}", end="", flush=True)
    returncode = process.wait()
    elapsed = time.monotonic() - started
    after = file_stamp(config.debug)
    fresh = after != before and after != (0, 0)
    state = "THÀNH CÔNG" if returncode == 0 else f"LỖI {returncode}"
    print(f"🏁 {config.label}: {state} | {elapsed:.1f}s | debug_mới={'có' if fresh else 'không'}", flush=True)
    return returncode, fresh, elapsed


def debug_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return len(payload["results"])
    return 0


def source_files(config: SourceConfig, returncode: int, fresh: bool) -> SourceFiles:
    return SourceFiles(
        key=config.key,
        label=config.label,
        universal=config.universal,
        pipe=config.pipe,
        vlc=config.vlc,
        debug=config.debug,
        returncode=returncode,
        fresh=fresh,
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass

    args = parse_args()
    print(f"🥷 KHỞI ĐỘNG MULTI-SOURCE SCANNER {VERSION}", flush=True)
    parallel_enabled = os.getenv("MULTI_RUN_SOURCES_PARALLEL", "1").strip().lower() not in {"0", "false", "no", "off"}
    mode = "song song có giới hạn" if parallel_enabled else "tuần tự"
    print(f"ℹ️ Một lần chạy quét Chuối Chiên + Lương Sơn theo chế độ {mode}, rồi gộp thành all_live.m3u.", flush=True)

    try:
        routed = route_urls(args.urls)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if args.source == "all":
        selected_keys = [key for key in ("chuoichien", "luongson") if not args.urls or routed[key]]
    else:
        selected_keys = [args.source]
        if args.urls and not routed[args.source]:
            print(f"❌ URL test không thuộc nguồn --source={args.source}", file=sys.stderr)
            return 2

    statuses: dict[str, tuple[int, bool, float]] = {}
    if args.merge_only:
        for key in selected_keys:
            statuses[key] = (0, True, 0.0)
    elif parallel_enabled and len(selected_keys) > 1:
        print(f"⚡ Chạy song song {len(selected_keys)} nguồn; mỗi nguồn vẫn tự giới hạn số tab.", flush=True)
        with ThreadPoolExecutor(max_workers=len(selected_keys), thread_name_prefix="source-scan") as executor:
            future_to_key = {
                executor.submit(run_source, SOURCES[key], routed[key]): key
                for key in selected_keys
            }
            for future in as_completed(future_to_key):
                key = future_to_key[future]
                try:
                    statuses[key] = future.result()
                except Exception as exc:
                    print(f"❌ Nguồn {SOURCES[key].label} lỗi điều phối: {type(exc).__name__}: {exc}", flush=True)
                    statuses[key] = (1, False, 0.0)
    else:
        for key in selected_keys:
            config = SOURCES[key]
            statuses[key] = run_source(config, routed[key])


    merge_inputs: list[SourceFiles] = []
    for key in selected_keys:
        config = SOURCES[key]
        returncode, fresh, _elapsed = statuses[key]
        rows = debug_row_count(config.debug)
        if returncode != 0:
            print(f"⚠️ Loại nguồn {config.label} khỏi lần gộp này vì scanner lỗi.", flush=True)
        elif rows == 0:
            print(f"⚠️ Nguồn {config.label} không có kết quả tươi; không dùng playlist cũ để tránh link chết.", flush=True)
        merge_inputs.append(source_files(config, returncode, fresh))

    print(f"\n{'=' * 72}", flush=True)
    print("🔀 GỘP PLAYLIST VÀ LỌC TRÙNG/CHẤT LƯỢNG", flush=True)
    print(f"{'=' * 72}", flush=True)
    report = merge_sources(ROOT, merge_inputs, preserve_on_empty=True)

    if report["selected_count"]:
        print(
            f"✅ Gộp xong: đầu vào={report['input_candidates']} | "
            f"giữ={report['selected_count']} | loại={report['dropped_count']}",
            flush=True,
        )
        print(f"📺 Playlist duy nhất: {(ROOT / 'all_live.m3u').resolve()}", flush=True)
    else:
        print("⚠️ Không có stream đủ tin cậy từ hai nguồn; giữ nguyên all_live.m3u cũ nếu có.", flush=True)

    removed = cleanup_intermediate_playlists(ROOT)
    if removed:
        print(f"🧹 Đã xóa {len(removed)} playlist trung gian; chỉ giữ all_live.m3u.", flush=True)

    success_sources = sum(1 for code, _fresh, _elapsed in statuses.values() if code == 0)
    if success_sources == 0 and not args.merge_only:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
