"""
generate_dem.py
===============
Downloads free AWS Terrain Tiles (Terrarium format) covering Sweden and
produces a set of small 1°×1° JSON elevation tiles at ~200 m resolution
suitable for client-side wind-sheltering calculations in a boat navigation app.

Source:  AWS Terrain Tiles (Mapzen / Linux Foundation)
URL:     https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
License: Public domain (derived from SRTM, EU-DEM, NED etc.)
Encoding: elevation_m = (R × 256 + G + B / 256) - 32768

Output files:
  api/dem/index.json          — list of available tiles + grid metadata
  api/dem/tiles/N55E010.json  — 1°×1° elevation grid (one per land tile)

Each output tile is a JSON object:
  {
    "lat":   55,        // SW corner latitude (integer degrees)
    "lon":   10,        // SW corner longitude
    "rows":  334,       // number of latitude steps  (≈ 0.003° each)
    "cols":  334,       // number of longitude steps
    "dlat":  0.003,     // degrees per row  (~200 m N-S)
    "dlon":  0.003,     // degrees per col  (~150 m E-W at 60°N)
    "data":  [...]      // flat int16 array, row-major (N→S, W→E)
                        // negative values clamped to 0 (sea = 0)
  }

Usage:
  pip install requests Pillow numpy
  python generate_dem.py
"""

import json
import math
import os
import sys
import time

import numpy as np
import requests
from PIL import Image

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ZOOM          = 9          # z=9 → ~300 m native pixel resolution at 60°N
TILE_URL      = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
OUTPUT_DIR    = os.path.join("api", "dem", "tiles")
INDEX_PATH    = os.path.join("api", "dem", "index.json")
CACHE_DIR     = os.path.join(".cache", "terrarium")

# Sweden bounding box with a 0.5° buffer
LAT_MIN, LAT_MAX = 54.5, 70.5
LON_MIN, LON_MAX = 9.5,  25.5

# Output tile resolution
DLAT = 0.003   # ≈ 334 m at any latitude
DLON = 0.003   # ≈ 150–200 m depending on latitude

# Only save tiles where max land elevation exceeds this (filters pure-sea tiles)
MIN_LAND_ELEVATION_M = 1.0

RETRY_DELAY   = 5
MAX_RETRIES   = 4
REQUEST_PAUSE = 0.05   # 50 ms between tile downloads (be polite to S3)


# ---------------------------------------------------------------------------
# Tile coordinate maths (Web Mercator / Slippy Map)
# ---------------------------------------------------------------------------

def deg_to_tile(lat_deg: float, lon_deg: float, zoom: int) -> tuple[int, int]:
    """Convert (lat, lon) in WGS84 to (x, y) tile coords at the given zoom."""
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    lat_r = math.radians(lat_deg)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi)
             / 2.0 * n)
    return x, y


def tile_to_deg_nw(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Return the NW corner (lat, lon) of a tile."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_r = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat = math.degrees(lat_r)
    return lat, lon


def tile_bbox(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    """Return (lat_n, lat_s, lon_w, lon_e) for a tile."""
    lat_n, lon_w = tile_to_deg_nw(x,     y,     zoom)
    lat_s, lon_e = tile_to_deg_nw(x + 1, y + 1, zoom)
    return lat_n, lat_s, lon_w, lon_e


# ---------------------------------------------------------------------------
# Terrarium tile download + decode
# ---------------------------------------------------------------------------

def download_tile(x: int, y: int, zoom: int) -> np.ndarray | None:
    """
    Download a single Terrarium PNG tile, decode to a float32 elevation array
    of shape (256, 256) in metres. Returns None on failure.
    """
    cache_path = os.path.join(CACHE_DIR, str(zoom), str(x), f"{y}.png")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    # Use cache if available
    if os.path.exists(cache_path):
        img = Image.open(cache_path).convert("RGB")
    else:
        url = TILE_URL.format(z=zoom, x=x, y=y)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                with open(cache_path, "wb") as f:
                    f.write(r.content)
                img = Image.open(cache_path).convert("RGB")
                break
            except Exception as e:
                if attempt == MAX_RETRIES:
                    print(f"    FAILED {url}: {e}")
                    return None
                time.sleep(RETRY_DELAY)
        else:
            return None

        time.sleep(REQUEST_PAUSE)

    arr = np.array(img, dtype=np.float32)   # shape (256, 256, 3)
    R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    # Terrarium decoding
    elevation = R * 256.0 + G + B / 256.0 - 32768.0
    return elevation   # shape (256, 256), float32, metres


# ---------------------------------------------------------------------------
# Build Sweden elevation mosaic
# ---------------------------------------------------------------------------

def build_mosaic(zoom: int) -> dict:
    """
    Download all Terrarium tiles covering the Sweden bbox and return a dict
    mapping (x, y) → numpy elevation array (256×256 float32).
    """
    x_min, y_min = deg_to_tile(LAT_MAX, LON_MIN, zoom)   # NW corner
    x_max, y_max = deg_to_tile(LAT_MIN, LON_MAX, zoom)   # SE corner

    total = (x_max - x_min + 1) * (y_max - y_min + 1)
    print(f"  Fetching {total} tiles at zoom {zoom} "
          f"(x {x_min}–{x_max}, y {y_min}–{y_max})")

    tiles = {}
    count = 0
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            count += 1
            if count % 50 == 0:
                print(f"    {count}/{total} tiles downloaded…")
            data = download_tile(x, y, zoom)
            if data is not None:
                tiles[(x, y)] = data

    print(f"  Downloaded {len(tiles)}/{total} tiles successfully")
    return tiles


def sample_elevation(tiles: dict, zoom: int,
                     lat: float, lon: float) -> float:
    """
    Bilinear interpolation of elevation at (lat, lon) from the tile mosaic.
    Returns 0.0 if outside coverage.
    """
    n = 2 ** zoom
    # Fractional tile position
    fx = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    fy = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n

    tx, ty = int(fx), int(fy)
    px = (fx - tx) * 256
    py = (fy - ty) * 256

    arr = tiles.get((tx, ty))
    if arr is None:
        return 0.0

    # Pixel indices (clamp to tile boundary)
    i0 = min(int(py), 255)
    j0 = min(int(px), 255)
    i1 = min(i0 + 1, 255)
    j1 = min(j0 + 1, 255)

    fi = py - int(py)
    fj = px - int(px)

    e = (arr[i0, j0] * (1 - fi) * (1 - fj)
         + arr[i1, j0] * fi      * (1 - fj)
         + arr[i0, j1] * (1 - fi) * fj
         + arr[i1, j1] * fi      * fj)
    return float(e)


# ---------------------------------------------------------------------------
# Generate 1°×1° output tiles
# ---------------------------------------------------------------------------

def generate_output_tiles(tiles: dict, zoom: int) -> list[dict]:
    """
    For each 1°×1° cell covering Sweden, sample the mosaic onto a
    regular DLAT×DLON grid and save a JSON tile.
    Returns list of tile metadata dicts for the index.
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    index = []

    lat_start = int(math.floor(LAT_MIN))
    lat_end   = int(math.ceil(LAT_MAX))
    lon_start = int(math.floor(LON_MIN))
    lon_end   = int(math.ceil(LON_MAX))

    total_tiles = (lat_end - lat_start) * (lon_end - lon_start)
    processed   = 0
    saved       = 0

    for lat0 in range(lat_start, lat_end):
        for lon0 in range(lon_start, lon_end):
            processed += 1
            lat1 = lat0 + 1
            lon1 = lon0 + 1

            # Build sample grid (N→S, W→E)
            lats = np.arange(lat1, lat0, -DLAT)    # descending (N to S)
            lons = np.arange(lon0, lon1,  DLON)    # ascending  (W to E)
            rows, cols = len(lats), len(lons)

            data = []
            max_elev = 0.0
            for lat in lats:
                for lon in lons:
                    e = sample_elevation(tiles, zoom, float(lat), float(lon))
                    clamped = max(0, int(round(e)))
                    data.append(clamped)
                    if clamped > max_elev:
                        max_elev = clamped

            if max_elev < MIN_LAND_ELEVATION_M:
                continue   # pure ocean tile — skip

            # Tile name: N58E017 (SW corner)
            lat_hem = "N" if lat0 >= 0 else "S"
            lon_hem = "E" if lon0 >= 0 else "W"
            tile_name = f"{lat_hem}{abs(lat0):02d}{lon_hem}{abs(lon0):03d}"
            out_path = os.path.join(OUTPUT_DIR, f"{tile_name}.json")

            tile_obj = {
                "lat":   lat0,
                "lon":   lon0,
                "rows":  rows,
                "cols":  cols,
                "dlat":  DLAT,
                "dlon":  DLON,
                "max_elevation_m": int(max_elev),
                "data":  data,
            }

            with open(out_path, "w") as f:
                # Compact JSON — no spaces between items
                json.dump(tile_obj, f, separators=(",", ":"))

            size_kb = os.path.getsize(out_path) / 1024
            print(f"  [{processed}/{total_tiles}] {tile_name}: "
                  f"{rows}×{cols} grid, max {int(max_elev)} m, {size_kb:.0f} KB")

            index.append({
                "name":           tile_name,
                "lat":            lat0,
                "lon":            lon0,
                "max_elevation_m": int(max_elev),
                "path":           f"tiles/{tile_name}.json",
            })
            saved += 1

    return index


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Sweden DEM Generator")
    print("Source: AWS Terrain Tiles (Terrarium, free/public)")
    print("=" * 60)

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)

    # Step 1: Download tiles
    print(f"\n[1/3] Downloading Terrarium tiles (zoom {ZOOM})…")
    tiles = build_mosaic(ZOOM)

    if not tiles:
        print("ERROR: No tiles downloaded. Check network connectivity.")
        sys.exit(1)

    # Step 2: Generate output tiles
    print(f"\n[2/3] Sampling elevation grid at {DLAT}° resolution…")
    index = generate_output_tiles(tiles, ZOOM)

    # Step 3: Write index
    print(f"\n[3/3] Writing index ({len(index)} land tiles)…")
    index_obj = {
        "description":  "Sweden elevation tiles for wind-sheltering calculation",
        "source":       "AWS Terrain Tiles (Mapzen/Linux Foundation)",
        "license":      "Public domain",
        "zoom_source":  ZOOM,
        "dlat":         DLAT,
        "dlon":         DLON,
        "bbox": {
            "lat_min": LAT_MIN, "lat_max": LAT_MAX,
            "lon_min": LON_MIN, "lon_max": LON_MAX,
        },
        "usage": (
            "Fetch tile by lat/lon floor, e.g. lat=58.7 lon=17.4 → N58E017.json. "
            "data[] is a flat row-major array (N→S, W→E). "
            "Index with: row = floor((lat_nw - lat) / dlat), col = floor((lon - lon0) / dlon)"
        ),
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     __import__("time").gmtime()),
        "tiles": index,
    }
    with open(INDEX_PATH, "w") as f:
        json.dump(index_obj, f, indent=2)

    # Summary
    total_size_mb = sum(
        os.path.getsize(os.path.join(OUTPUT_DIR, t["name"] + ".json"))
        for t in index
    ) / (1024 * 1024)

    print(f"\nDone! {len(index)} tiles, {total_size_mb:.1f} MB total")
    print(f"Index: {INDEX_PATH}")
    print(f"Tiles: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
