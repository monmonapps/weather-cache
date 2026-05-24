"""Open-Meteo fetcher.

Reads grid/grid_cells.json, fetches each cell from Open-Meteo (in parallel),
transforms to custom schema, writes one JSON per cell under docs/v1/.

Environment variables:
  URL_SALT          (required) HMAC salt for filename hashing
  OPENMETEO_APIKEY  (optional) If set, uses commercial endpoint
  OPENMETEO_MODE    (optional) "free" or "commercial". Defaults to
                    "commercial" if OPENMETEO_APIKEY is set, else "free".
  FETCH_LIMIT       (optional) If set to a positive integer, only fetches
                    the first N cells. Useful for dev sanity tests on Free
                    tier so we don't burn the daily 10,000-call budget.
  FETCH_CELL_IDS    (optional) Comma-separated list of cell IDs (e.g.
                    "393_1269,394_1269"). If set, only those cells are
                    fetched. Applied before FETCH_LIMIT. Unknown IDs are
                    warned and skipped.
  FETCH_WORKERS     (optional) Parallelism. Default 8 (safe for Free's
                    ~300/min rate limit). Standard plan can go higher.
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from transform import transform
from url_hasher import hmac_filename


ROOT = Path(__file__).resolve().parent.parent
GRID_PATH = ROOT / "grid" / "grid_cells.json"
OUT_DIR = ROOT / "docs" / "v1"

FREE_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
COMMERCIAL_ENDPOINT = "https://customer-api.open-meteo.com/v1/forecast"

HOURLY_PARAMS = ",".join([
    "temperature_2m",
    "apparent_temperature",
    "precipitation_probability",
    "precipitation",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "relative_humidity_2m",
    "uv_index",
])
DAILY_PARAMS = ",".join([
    "weather_code",
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "precipitation_probability_mean",
    "precipitation_sum",
    "sunrise",
    "sunset",
    "uv_index_max",
])
CURRENT_PARAMS = "temperature_2m,weather_code,is_day"

REQUEST_TIMEOUT = 10  # seconds — Open-Meteo normally returns <1s; longer waits
                      # almost always indicate a dead connection that retries faster.
RETRY_DELAYS = [1, 4, 16]  # for 5xx / timeout
RATE_LIMIT_DELAY = 15      # for 429. Per-thread blocking sleep, so keeping this
                           # short matters: 60s × ~3 workers stalls the whole pool.
                           # Jitter (see fetch_one) prevents the pool from
                           # resynchronizing and immediately re-hitting 429.
DEFAULT_WORKERS = 8
USER_AGENT = "weather-cache-fetcher/1 (+https://github.com)"


def main() -> int:
    salt = os.environ.get("URL_SALT")
    if not salt:
        print("ERROR: URL_SALT env var is required", file=sys.stderr)
        return 1

    api_key = os.environ.get("OPENMETEO_APIKEY", "").strip() or None
    mode = os.environ.get("OPENMETEO_MODE", "").strip().lower()
    if not mode:
        mode = "commercial" if api_key else "free"
    if mode == "commercial" and not api_key:
        print("ERROR: OPENMETEO_MODE=commercial requires OPENMETEO_APIKEY", file=sys.stderr)
        return 1
    endpoint = COMMERCIAL_ENDPOINT if mode == "commercial" else FREE_ENDPOINT

    limit = _read_int_env("FETCH_LIMIT", default=0)
    workers = _read_int_env("FETCH_WORKERS", default=DEFAULT_WORKERS)
    if workers < 1:
        workers = 1

    grid = _load_grid()
    cells = grid["cells"]

    cell_ids_raw = os.environ.get("FETCH_CELL_IDS", "").strip()
    if cell_ids_raw:
        wanted = [s.strip() for s in cell_ids_raw.split(",") if s.strip()]
        by_id = {c["id"]: c for c in cells}
        missing = [cid for cid in wanted if cid not in by_id]
        if missing:
            print(f"[warn] FETCH_CELL_IDS: unknown cell id(s) skipped: {missing}", file=sys.stderr)
        cells = [by_id[cid] for cid in wanted if cid in by_id]
        print(f"[info] FETCH_CELL_IDS -> processing {len(cells)} cell(s)")

    if limit > 0:
        cells = cells[:limit]
        print(f"[info] FETCH_LIMIT={limit} -> processing only first {len(cells)} cell(s)")

    print(f"[info] mode={mode}  endpoint={endpoint}")
    print(f"[info] cells={len(cells)}  workers={workers}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    results: list[tuple[str, dict[str, Any] | None]] = []
    completed = 0
    total = len(cells)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_and_write, cell, endpoint, api_key, salt): cell
            for cell in cells
        }
        for fut in as_completed(futures):
            cell = futures[fut]
            completed += 1
            try:
                cell_id, data = fut.result()
                results.append((cell_id, data))
                if data is None:
                    print(f"[warn] ({completed}/{total}) cell {cell['id']}: failed")
                elif completed % 100 == 0 or completed == total:
                    elapsed = time.monotonic() - start
                    rate = completed / elapsed if elapsed > 0 else 0
                    print(f"[ok]   ({completed}/{total}) elapsed={elapsed:.1f}s rate={rate:.1f} cells/s")
            except Exception as e:
                print(f"[err]  cell {cell['id']}: unhandled {type(e).__name__}: {e}")
                results.append((cell["id"], None))

    _write_index(grid, results)

    failed = sum(1 for _, d in results if d is None)
    total = len(results)
    elapsed = time.monotonic() - start
    print(f"[done] {total - failed}/{total} cells in {elapsed:.1f}s")

    if total > 0 and failed / total > 0.10:
        print(f"[fail] {failed}/{total} cells failed (>10%)", file=sys.stderr)
        return 2
    return 0


def _fetch_and_write(
    cell: dict[str, Any],
    endpoint: str,
    api_key: str | None,
    salt: str,
) -> tuple[str, dict[str, Any] | None]:
    cell_id = cell["id"]
    lat = cell["lat"]
    lon = cell["lon"]
    data = fetch_one(cell_id, lat, lon, endpoint, api_key)
    if data is None:
        return (cell_id, None)
    filename = hmac_filename(salt, cell_id)
    out_path = OUT_DIR / f"{filename}.json"
    out_path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return (cell_id, data)


def fetch_one(
    cell_id: str,
    lat: float,
    lon: float,
    endpoint: str,
    api_key: str | None,
) -> dict[str, Any] | None:
    params = {
        "latitude": f"{lat}",
        "longitude": f"{lon}",
        "current": CURRENT_PARAMS,
        "hourly": HOURLY_PARAMS,
        "daily": DAILY_PARAMS,
        "wind_speed_unit": "ms",
        "timezone": "Asia/Tokyo",
        "models": "best_match",
        "forecast_days": "14",
        "past_days": "0",
    }
    if api_key:
        params["apikey"] = api_key
    url = f"{endpoint}?{urlencode(params)}"

    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return transform(cell_id, lat, lon, payload, datetime.now(timezone.utc))
        except HTTPError as e:
            if e.code == 429:
                if attempt < len(RETRY_DELAYS):
                    # Jitter so the worker pool doesn't resynchronize and
                    # immediately re-hit 429 on the next minute boundary.
                    wait = RATE_LIMIT_DELAY + random.uniform(0, RATE_LIMIT_DELAY)
                    print(f"[warn] cell {cell_id}: HTTP 429, waiting {wait:.1f}s")
                    time.sleep(wait)
                    continue
                print(f"[err]  cell {cell_id}: HTTP 429 exhausted retries")
                return None
            if 500 <= e.code < 600 and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            print(f"[err]  cell {cell_id}: HTTP {e.code}: {e.reason}")
            return None
        except (URLError, TimeoutError) as e:
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            print(f"[err]  cell {cell_id}: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            print(f"[err]  cell {cell_id}: unexpected {type(e).__name__}: {e}")
            return None
    return None


def _load_grid() -> dict[str, Any]:
    if not GRID_PATH.exists():
        raise FileNotFoundError(f"grid file not found: {GRID_PATH}")
    return json.loads(GRID_PATH.read_text(encoding="utf-8"))


def _write_index(grid: dict[str, Any], results: list[tuple[str, dict[str, Any] | None]]) -> None:
    succeeded = sum(1 for _, d in results if d is not None)
    index = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url_salt_version": 1,
        "cell_count": succeeded,
        "grid_lat_deg": grid.get("grid_lat_deg"),
        "grid_lon_deg": grid.get("grid_lon_deg"),
    }
    (OUT_DIR / "index.json").write_text(json.dumps(index, separators=(",", ":")))


def _read_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[warn] env {name}={raw!r} not an int, using default {default}")
        return default


if __name__ == "__main__":
    sys.exit(main())
