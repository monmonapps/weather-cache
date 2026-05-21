"""Open-Meteo fetcher.

Reads grid/grid_cells.json, fetches each cell from Open-Meteo,
transforms to custom schema, writes one JSON per cell under docs/v1/.

For Phase 1, runs sequentially (small grid). Phase 2+ may add
ThreadPoolExecutor for the full 1,000-cell grid.

Environment variables:
  URL_SALT          (required) HMAC salt for filename hashing
  OPENMETEO_APIKEY  (optional) If set, uses commercial endpoint
  OPENMETEO_MODE    (optional) "free" or "commercial". Defaults to
                    "commercial" if OPENMETEO_APIKEY is set, else "free".
"""
from __future__ import annotations

import json
import os
import sys
import time
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
    "sunrise",
    "sunset",
    "uv_index_max",
])
CURRENT_PARAMS = "temperature_2m,weather_code,is_day"

REQUEST_TIMEOUT = 30  # seconds
RETRY_DELAYS = [1, 4, 16]  # for 5xx / timeout
RATE_LIMIT_DELAY = 60      # for 429
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
    print(f"[info] Using {mode} endpoint: {endpoint}")

    grid = _load_grid()
    cells = grid["cells"]
    print(f"[info] Loaded {len(cells)} cell(s) from grid")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, dict[str, Any] | None]] = []
    for i, cell in enumerate(cells, start=1):
        cell_id = cell["id"]
        lat = cell["lat"]
        lon = cell["lon"]
        print(f"[fetch] ({i}/{len(cells)}) cell={cell_id} lat={lat} lon={lon}")
        data = fetch_one(cell_id, lat, lon, endpoint, api_key)
        if data is None:
            print(f"[warn] cell {cell_id}: fetch failed, skipping (previous JSON retained)")
            results.append((cell_id, None))
            continue

        filename = hmac_filename(salt, cell_id)
        out_path = OUT_DIR / f"{filename}.json"
        out_path.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False))
        results.append((cell_id, data))
        print(f"[ok]   cell {cell_id} -> docs/v1/{filename}.json")

    _write_index(grid, results)

    failed = sum(1 for _, d in results if d is None)
    total = len(results)
    if total > 0 and failed / total > 0.10:
        print(f"[fail] {failed}/{total} cells failed (>10%)", file=sys.stderr)
        return 2
    print(f"[done] {total - failed}/{total} cells fetched successfully")
    return 0


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
        "timezone": "Asia/Tokyo",
        "models": "best_match",
        "forecast_days": "7",
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
                print(f"[warn] cell {cell_id}: HTTP 429, waiting {RATE_LIMIT_DELAY}s")
                time.sleep(RATE_LIMIT_DELAY)
                continue
            if 500 <= e.code < 600 and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                print(f"[warn] cell {cell_id}: HTTP {e.code}, retrying in {delay}s")
                time.sleep(delay)
                continue
            print(f"[err]  cell {cell_id}: HTTP {e.code}: {e.reason}")
            return None
        except (URLError, TimeoutError) as e:
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                print(f"[warn] cell {cell_id}: {type(e).__name__}: {e}, retrying in {delay}s")
                time.sleep(delay)
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


if __name__ == "__main__":
    sys.exit(main())
