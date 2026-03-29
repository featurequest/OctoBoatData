"""
fetch_weather.py
================
Fetches current wind, wave and temperature data for Sweden and the
surrounding seas. Data is written as JSON files intended to be served
statically from GitHub Pages and consumed by a Mapbox boat-navigation app.

Sources:
  - Open-Meteo Forecast API  (wind, temperature, gusts) — free, no key
  - Open-Meteo Marine API    (waves, swell, SST)        — free, no key
  - SMHI Open Data API       (coastal station obs)      — free, open

Output:
  api/weather/wind.json        7-day hourly wind grid over Sweden
  api/weather/waves.json       7-day hourly wave grid over Swedish waters
  api/weather/observations.json  Current SMHI coastal station readings
  api/weather/meta.json        Last-updated timestamp + source info

Grid design:
  Wind/temp: 0.25° grid, lat 55–70, lon 10–25  (~61×61 = 3721 points)
             Batched as 50 per Open-Meteo request to stay within limits
  Waves:     0.5°  grid, lat 55–70, lon 10–25  (~31×31 = 961 points)
             Marine API only responds for ocean/sea points; land = null

Client-side usage:
  1. Load wind.json once per hour (cache in app)
  2. For a vessel position, find the 4 surrounding grid points
  3. Bilinearly interpolate U/V components, then compute speed+direction
  4. Apply DEM-based sheltering correction (see generate_dem.py)
"""

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
MARINE_API   = "https://marine-api.open-meteo.com/v1/marine"
SMHI_OBS_API = "https://opendata-download-metobs.smhi.se/api/version/latest"

OUT_DIR      = os.path.join("api", "weather")

# Sweden bbox
LAT_MIN, LAT_MAX = 55.0, 70.0
LON_MIN, LON_MAX = 10.0, 25.0

# Grid resolutions
WIND_STEP  = 0.25   # degrees
WAVE_STEP  = 0.5    # degrees

BATCH_SIZE   = 50   # Open-Meteo max locations per request
RETRY_DELAY  = 10
MAX_RETRIES  = 5
MAX_WORKERS  = 3    # concurrent HTTP requests (keep below Open-Meteo rate limit)

# SMHI parameter codes for coastal/marine stations
# 4  = wind speed (m/s, 10-min mean)
# 3  = wind direction (degrees)
# 1  = air temperature (°C)
# 39 = wave height (m) — buoys only
SMHI_PARAMS = {
    "wind_speed":      4,
    "wind_direction":  3,
    "temperature":     1,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_grid(lat_min: float, lat_max: float,
               lon_min: float, lon_max: float,
               step: float) -> list[tuple[float, float]]:
    """Return a list of (lat, lon) grid points."""
    lats = [round(lat_min + i * step, 6)
            for i in range(int((lat_max - lat_min) / step) + 1)]
    lons = [round(lon_min + j * step, 6)
            for j in range(int((lon_max - lon_min) / step) + 1)]
    return [(lat, lon) for lat in lats for lon in lons]


def get_json(url: str, params: dict, timeout: int = 60) -> dict | None:
    """GET with retry and 429-aware backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "BoatNavApp/1.0"})
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", RETRY_DELAY * attempt))
                if attempt == MAX_RETRIES:
                    print(f"    FAILED {url.split('?')[0]}: 429 after {MAX_RETRIES} attempts")
                    return None
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"    FAILED {url.split('?')[0]}: {e}")
                return None
            time.sleep(RETRY_DELAY * attempt)  # exponential-ish backoff
    return None


def chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


# ---------------------------------------------------------------------------
# Open-Meteo forecast (wind + temperature)
# ---------------------------------------------------------------------------

WIND_VARS = [
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
    "temperature_2m",
]

def _fetch_wind_batch(batch: list[tuple[float, float]]) -> list[dict]:
    lats = [p[0] for p in batch]
    lons = [p[1] for p in batch]
    params = {
        "latitude":        ",".join(map(str, lats)),
        "longitude":       ",".join(map(str, lons)),
        "hourly":          ",".join(WIND_VARS),
        "wind_speed_unit": "ms",
        "forecast_days":   7,
        "timezone":        "UTC",
    }
    data = get_json(FORECAST_API, params)
    if data is None:
        return []
    results = []
    for entry, (lat, lon) in zip(data if isinstance(data, list) else [data], batch):
        hourly = entry.get("hourly", {})
        spd  = hourly.get("wind_speed_10m", [])
        wdir = hourly.get("wind_direction_10m", [])
        u_list, v_list = [], []
        for s, d in zip(spd, wdir):
            if s is None or d is None:
                u_list.append(None)
                v_list.append(None)
            else:
                rad = math.radians(d)
                u_list.append(round(-s * math.sin(rad), 3))
                v_list.append(round(-s * math.cos(rad), 3))
        results.append({
            "lat":   lat,
            "lon":   lon,
            "times": hourly.get("time", []),
            "u10":   u_list,
            "v10":   v_list,
            "gusts": [round(g, 1) if g is not None else None
                      for g in hourly.get("wind_gusts_10m", [])],
            "temp":  [round(t, 1) if t is not None else None
                      for t in hourly.get("temperature_2m", [])],
        })
    return results


def fetch_wind_grid(grid: list[tuple[float, float]]) -> dict[str, Any]:
    """
    Fetch 7-day hourly wind + temperature for every grid point.
    Returns a dict suitable for JSON serialisation.
    """
    batches = list(chunks(grid, BATCH_SIZE))
    total = len(batches)
    print(f"  Fetching wind forecast for {len(grid)} grid points "
          f"({total} requests, {MAX_WORKERS} workers)…")

    all_results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_wind_batch, b): i for i, b in enumerate(batches)}
        for future in as_completed(futures):
            i = futures[future]
            result = future.result()
            done += 1
            if not result:
                print(f"    [{done}/{total}] Batch {i+1} failed — skipping")
            elif done % 10 == 0 or done == total:
                print(f"    [{done}/{total}] {done * 100 // total}% complete")
            all_results.extend(result)

    return {
        "description":   "7-day hourly wind forecast grid for Sweden",
        "source":        "Open-Meteo (ECMWF IFS 9km + DWD ICON 1km)",
        "license":       "CC BY 4.0",
        "attribution":   "open-meteo.com",
        "grid_step_deg": WIND_STEP,
        "bbox": {
            "lat_min": LAT_MIN, "lat_max": LAT_MAX,
            "lon_min": LON_MIN, "lon_max": LON_MAX,
        },
        "variables": {
            "u10":   "Eastward wind component (m/s) at 10 m",
            "v10":   "Northward wind component (m/s) at 10 m",
            "gusts": "Wind gust speed (m/s) at 10 m",
            "temp":  "Air temperature (°C) at 2 m",
        },
        "interpolation_note": (
            "Interpolate U and V separately, then compute "
            "speed = sqrt(u²+v²), direction = atan2(-u, -v) * 180/π"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "points":        all_results,
    }


# ---------------------------------------------------------------------------
# Open-Meteo marine (waves + SST)
# ---------------------------------------------------------------------------

WAVE_VARS = [
    "wave_height",
    "wave_direction",
    "wave_period",
    "wind_wave_height",
    "swell_wave_height",
    "swell_wave_direction",
    "sea_surface_temperature",
]

def _round1(lst: list) -> list:
    return [round(v, 1) if v is not None else None for v in lst]


def _fetch_wave_batch(batch: list[tuple[float, float]]) -> list[dict]:
    lats = [p[0] for p in batch]
    lons = [p[1] for p in batch]
    params = {
        "latitude":      ",".join(map(str, lats)),
        "longitude":     ",".join(map(str, lons)),
        "hourly":        ",".join(WAVE_VARS),
        "forecast_days": 7,
        "timezone":      "UTC",
    }
    data = get_json(MARINE_API, params)
    if data is None:
        return []
    results = []
    for entry, (lat, lon) in zip(data if isinstance(data, list) else [data], batch):
        hourly = entry.get("hourly", {})
        wave_h = hourly.get("wave_height", [])
        if all(v is None for v in wave_h):
            continue
        results.append({
            "lat":         lat,
            "lon":         lon,
            "times":       hourly.get("time", []),
            "wave_height": _round1(wave_h),
            "wave_dir":    _round1(hourly.get("wave_direction", [])),
            "wave_period": _round1(hourly.get("wave_period", [])),
            "wind_wave_h": _round1(hourly.get("wind_wave_height", [])),
            "swell_h":     _round1(hourly.get("swell_wave_height", [])),
            "swell_dir":   _round1(hourly.get("swell_wave_direction", [])),
            "sst":         _round1(hourly.get("sea_surface_temperature", [])),
        })
    return results


def fetch_wave_grid(grid: list[tuple[float, float]]) -> dict[str, Any]:
    """
    Fetch 7-day hourly wave data for sea grid points.
    Land points will return nulls — these are filtered out.
    """
    batches = list(chunks(grid, BATCH_SIZE))
    total = len(batches)
    print(f"  Fetching wave forecast for {len(grid)} grid points "
          f"({total} requests, {MAX_WORKERS} workers)…")

    all_results: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_wave_batch, b): i for i, b in enumerate(batches)}
        for future in as_completed(futures):
            i = futures[future]
            result = future.result()
            done += 1
            if result is None:
                print(f"    [{done}/{total}] Batch {i+1} failed — skipping")
            elif done % 5 == 0 or done == total:
                print(f"    [{done}/{total}] {done * 100 // total}% complete")
            all_results.extend(result or [])

    print(f"  {len(all_results)} sea grid points returned wave data")

    return {
        "description":   "7-day hourly wave forecast for Swedish waters",
        "source":        "Open-Meteo Marine (ERA5 + DWD WAM + ECMWF WAM)",
        "license":       "CC BY 4.0",
        "attribution":   "open-meteo.com",
        "grid_step_deg": WAVE_STEP,
        "bbox": {
            "lat_min": LAT_MIN, "lat_max": LAT_MAX,
            "lon_min": LON_MIN, "lon_max": LON_MAX,
        },
        "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "points":        all_results,
    }


# ---------------------------------------------------------------------------
# SMHI coastal station observations
# ---------------------------------------------------------------------------

def fetch_smhi_observations() -> dict[str, Any]:
    """
    Fetch the latest wind speed, wind direction and temperature observations
    from all SMHI weather stations. Filters to only coastal / marine stations
    with recent data.
    """
    print("  Fetching SMHI station observations…")
    stations_out = []

    for var_name, param_id in SMHI_PARAMS.items():
        url = f"{SMHI_OBS_API}/parameter/{param_id}/station-set/all/period/latest-day/data.json"
        data = get_json(url, {})
        if data is None:
            print(f"    SMHI {var_name} fetch failed")
            continue

        stations = data.get("station", [])
        for st in stations:
            lat = st.get("latitude")
            lon = st.get("longitude")
            # Filter to Sweden bbox
            if lat is None or lon is None:
                continue
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue

            values = st.get("value", [])
            if not values:
                continue

            latest = values[-1]
            try:
                val = float(latest.get("value", ""))
            except (ValueError, TypeError):
                continue

            # Find or create station entry
            st_id = str(st.get("key", ""))
            existing = next((s for s in stations_out if s["id"] == st_id), None)
            if existing is None:
                existing = {
                    "id":   st_id,
                    "name": st.get("name", ""),
                    "lat":  lat,
                    "lon":  lon,
                    "time": latest.get("date", ""),
                }
                stations_out.append(existing)
            existing[var_name] = round(val, 1)

        time.sleep(0.5)

    # Compute wind_speed_beaufort for convenience
    for st in stations_out:
        spd = st.get("wind_speed")
        if spd is not None:
            st["beaufort"] = speed_to_beaufort(spd)

    print(f"  Got observations from {len(stations_out)} SMHI stations")

    return {
        "description":   "Latest SMHI weather station observations",
        "source":        "SMHI Open Data API",
        "license":       "CC BY 4.0 — SMHI",
        "attribution":   "SMHI (Swedish Meteorological and Hydrological Institute)",
        "generated_at":  time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stations":      stations_out,
    }


def speed_to_beaufort(mps: float) -> int:
    """Convert wind speed in m/s to Beaufort scale."""
    thresholds = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2,
                  20.8, 24.5, 28.5, 32.7]
    for i, t in enumerate(thresholds):
        if mps < t:
            return i
    return 12


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Sweden Weather Data Fetcher")
    print("Sources: Open-Meteo (wind/waves) + SMHI (observations)")
    print("=" * 60)

    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- Wind grid ----------------------------------------------------------
    print("\n[1/4] Building wind grid…")
    wind_grid = build_grid(LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, WIND_STEP)
    print(f"  {len(wind_grid)} points at {WIND_STEP}°")
    wind_data = fetch_wind_grid(wind_grid)
    with open(os.path.join(OUT_DIR, "wind.json"), "w") as f:
        json.dump(wind_data, f, separators=(",", ":"))
    print(f"  Saved {len(wind_data['points'])} wind points")

    # ---- Wave grid ----------------------------------------------------------
    print("\n[2/4] Building wave grid…")
    wave_grid = build_grid(LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, WAVE_STEP)
    print(f"  {len(wave_grid)} points at {WAVE_STEP}°")
    wave_data = fetch_wave_grid(wave_grid)
    with open(os.path.join(OUT_DIR, "waves.json"), "w") as f:
        json.dump(wave_data, f, separators=(",", ":"))
    print(f"  Saved {len(wave_data['points'])} sea wave points")

    # ---- SMHI observations --------------------------------------------------
    print("\n[3/4] Fetching SMHI coastal observations…")
    obs_data = fetch_smhi_observations()
    with open(os.path.join(OUT_DIR, "observations.json"), "w") as f:
        json.dump(obs_data, f, separators=(",", ":"))
    print(f"  Saved {len(obs_data['stations'])} station observations")

    # ---- Meta file ----------------------------------------------------------
    print("\n[4/4] Writing metadata…")
    meta = {
        "last_updated":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "update_interval": "hourly",
        "wind_points":     len(wind_data["points"]),
        "wave_points":     len(wave_data["points"]),
        "station_count":   len(obs_data["stations"]),
        "files": {
            "wind":         "wind.json",
            "waves":        "waves.json",
            "observations": "observations.json",
        },
        "attribution": {
            "wind_waves": "Open-Meteo (open-meteo.com) — CC BY 4.0",
            "observations": "SMHI (smhi.se) — CC BY 4.0",
        },
    }
    with open(os.path.join(OUT_DIR, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nDone! All weather data written to {OUT_DIR}/")


if __name__ == "__main__":
    main()
