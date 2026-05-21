"""Generate grid_cells.json from MLIT 位置参照情報 (ISJ) data.

ISJ provides lat/lon at the 大字・町丁目 (small area) level for all
of Japan, distributed as 47 per-prefecture ZIPs (~150 MB total).
We aggregate these centroids to 10km grid cells; any cell that
contains at least one 大字 is considered "populated" and included.

Usage:
    python grid/generate_grid.py              # downloads if missing, then generates
    python grid/generate_grid.py --refresh    # re-downloads even if cached
    python grid/generate_grid.py --version 14.0a  # override ISJ version

Output:
    grid/cache/{NN}000-{version}.zip          (cached downloads)
    grid/grid_cells.json                       (final grid)

Run frequency: roughly once per year (ISJ releases ~1-2 updates/year).

Data source attribution (must appear in Play Store description):
    本アプリは「位置参照情報（国土交通省）」のデータを加工して
    天気予報の対象地域決定に使用しています。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import zipfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "grid" / "cache"
OUTPUT_PATH = ROOT / "grid" / "grid_cells.json"

DEFAULT_VERSION = "13.0a"
URL_TEMPLATE = "https://nlftp.mlit.go.jp/isj/dls/data/{ver}/{pref:02d}000-{ver}.zip"
PREFECTURE_CODES = list(range(1, 48))   # 01 (Hokkaido) .. 47 (Okinawa)

GRID_LAT_DEG = 0.09   # ~10 km
GRID_LON_DEG = 0.11   # ~10 km at 35°N

# Japan bounding box (matches JapanBoundsChecker on the Android side)
LAT_MIN, LAT_MAX = 24.0, 46.0
LON_MIN, LON_MAX = 122.0, 146.0

USER_AGENT = "weather-cache-grid-generator/1 (+https://github.com/monmonapps/weather-cache)"
DOWNLOAD_TIMEOUT = 60


def main() -> int:
    args = _parse_args()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cells: dict[str, tuple[float, float, int]] = {}  # cell_id -> (center_lat, center_lon, count)
    total_rows = 0
    parsed_per_pref: list[tuple[int, int]] = []

    for pref in PREFECTURE_CODES:
        zip_path = _ensure_downloaded(pref, args.version, refresh=args.refresh)
        rows = _parse_pref_zip(zip_path)
        parsed_per_pref.append((pref, len(rows)))
        total_rows += len(rows)
        for lat, lon in rows:
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue
            cell_id = _cell_id_for(lat, lon)
            if cell_id in cells:
                clat, clon, cnt = cells[cell_id]
                cells[cell_id] = (clat, clon, cnt + 1)
            else:
                clat, clon = _cell_center(cell_id)
                cells[cell_id] = (clat, clon, 1)
        print(f"  pref {pref:02d}: {len(rows):>6} rows  ->  {len(cells):>5} unique cells so far")

    print(f"\n[summary] {total_rows} small-area rows -> {len(cells)} unique 10km cells")
    _write_output(cells, args.version)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--version", default=DEFAULT_VERSION,
                   help=f"ISJ release version (default: {DEFAULT_VERSION})")
    p.add_argument("--refresh", action="store_true",
                   help="Re-download even if cached file exists")
    return p.parse_args()


def _ensure_downloaded(pref: int, version: str, refresh: bool) -> Path:
    fname = f"{pref:02d}000-{version}.zip"
    path = CACHE_DIR / fname
    if path.exists() and not refresh:
        return path
    url = URL_TEMPLATE.format(ver=version, pref=pref)
    print(f"[dl] pref={pref:02d}  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
        data = resp.read()
    path.write_bytes(data)
    print(f"     -> {path.name} ({len(data) // 1024} KB)")
    return path


def _parse_pref_zip(zip_path: Path) -> list[tuple[float, float]]:
    """Extract (lat, lon) tuples from the CSV inside the prefecture ZIP."""
    rows: list[tuple[float, float]] = []
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(f"no CSV inside {zip_path}")
        with zf.open(csv_names[0]) as f:
            raw = f.read()
    text = _decode_csv(raw)
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None:
        return rows
    lat_idx, lon_idx = _find_latlon_columns(header)
    for row in reader:
        if len(row) <= max(lat_idx, lon_idx):
            continue
        try:
            lat = float(row[lat_idx])
            lon = float(row[lon_idx])
        except ValueError:
            continue
        rows.append((lat, lon))
    return rows


def _decode_csv(raw: bytes) -> str:
    """ISJ CSVs are Shift-JIS in older releases, UTF-8 (with BOM) in newer."""
    for enc in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    # Last resort: replace undecodable bytes rather than crash
    return raw.decode("cp932", errors="replace")


def _find_latlon_columns(header: list[str]) -> tuple[int, int]:
    """Locate lat/lon columns. Header may be Japanese or romanized."""
    lat_idx = lon_idx = -1
    for i, name in enumerate(header):
        n = name.strip().replace('"', "")
        if n in ("緯度", "lat", "latitude"):
            lat_idx = i
        elif n in ("経度", "lon", "lng", "longitude"):
            lon_idx = i
    if lat_idx < 0 or lon_idx < 0:
        raise RuntimeError(f"could not find lat/lon columns in header: {header!r}")
    return lat_idx, lon_idx


def _cell_id_for(lat: float, lon: float) -> str:
    lat_idx = int(lat // GRID_LAT_DEG)
    lon_idx = int(lon // GRID_LON_DEG)
    return f"{lat_idx}_{lon_idx}"


def _cell_center(cell_id: str) -> tuple[float, float]:
    lat_idx_str, lon_idx_str = cell_id.split("_")
    lat_idx = int(lat_idx_str)
    lon_idx = int(lon_idx_str)
    return (
        (lat_idx + 0.5) * GRID_LAT_DEG,
        (lon_idx + 0.5) * GRID_LON_DEG,
    )


def _write_output(cells: dict[str, tuple[float, float, int]], version: str) -> None:
    sorted_ids = sorted(cells.keys(), key=lambda s: tuple(int(x) for x in s.split("_")))
    out = {
        "version": f"isj-{version}-{date.today().isoformat()}",
        "source": "国土交通省 位置参照情報 (大字・町丁目レベル)",
        "grid_lat_deg": GRID_LAT_DEG,
        "grid_lon_deg": GRID_LON_DEG,
        "cells": [
            {
                "id": cid,
                "lat": round(cells[cid][0], 4),
                "lon": round(cells[cid][1], 4),
            }
            for cid in sorted_ids
        ],
    }
    OUTPUT_PATH.write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[ok] wrote {OUTPUT_PATH.name}: {len(sorted_ids)} cells")


if __name__ == "__main__":
    sys.exit(main())
