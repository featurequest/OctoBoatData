# boat-nav-data

Static JSON API for a Swedish boat navigation app, auto-updated via GitHub Actions
and served free from GitHub Pages.

## Repository structure

```
.github/workflows/
  update_weather.yml     Hourly — fetches wind, waves, SMHI observations
  update_harbors.yml     Weekly — fetches OSM + Naturvårdsverket harbors
  generate_dem.yml       Weekly — builds 200 m elevation tiles (for wind sheltering)

scripts/
  requirements.txt
  fetch_weather.py       Open-Meteo + SMHI
  fetch_harbors.py       OSM Overpass + Naturvårdsverket WFS
  generate_dem.py        AWS Terrain Tiles → 1°×1° JSON DEM tiles

api/
  weather/
    wind.json            7-day hourly wind grid (0.25°, Sweden)
    waves.json           7-day hourly wave grid (0.5°, Baltic)
    observations.json    Current SMHI coastal station readings
    meta.json            Last-updated + source info
  harbors.json           ~2000+ harbors, marinas, anchorages
  dem/
    index.json           Tile list + metadata
    tiles/
      N55E010.json       1°×1° elevation grid at ~200 m resolution
      N55E011.json
      ...
```

## Setup

1. Fork / create a new repo from this template
2. Enable GitHub Pages (Settings → Pages → Deploy from branch `main`, root `/`)
3. Enable GitHub Actions (they should be on by default)
4. Trigger the DEM workflow manually first (Actions → Generate DEM → Run workflow)
5. Weather and harbor workflows will run on their own schedule

Your API will be live at:
`https://<username>.github.io/<reponame>/api/`

## Attribution

Data served by this API must be attributed:
- **Wind / waves**: © [Open-Meteo](https://open-meteo.com) (CC BY 4.0)
- **Observations**: © [SMHI](https://smhi.se) (CC BY 4.0)
- **Harbors (OSM)**: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) (ODbL)
- **Harbors (NV)**: Naturvårdsverket (CC0)
- **Elevation**: [AWS Terrain Tiles](https://registry.opendata.aws/terrain-tiles/) (public domain)

---

## Client-side usage example (JavaScript / Mapbox)

```javascript
// ============================================================
// boat-nav-client.js — example usage of the GitHub Pages API
// ============================================================

const BASE = "https://<username>.github.io/<reponame>/api";

// ---- 1. Wind interpolation -----------------------------------------

let _windCache = null;
let _windCacheTime = 0;

async function getWindGrid() {
  // Cache for 1 hour
  if (_windCache && Date.now() - _windCacheTime < 3_600_000) {
    return _windCache;
  }
  const res = await fetch(`${BASE}/weather/wind.json`);
  _windCache = await res.json();
  _windCacheTime = Date.now();
  return _windCache;
}

/**
 * Get interpolated wind at (lat, lon) for the current hour.
 * Returns { speed_ms, direction_deg, gust_ms, temp_c }
 */
async function getWindAt(lat, lon) {
  const grid = await getWindGrid();
  const step = grid.grid_step_deg;

  // Find the 4 surrounding grid points
  const lat0 = Math.floor(lat / step) * step;
  const lon0 = Math.floor(lon / step) * step;
  const corners = [
    { lat: lat0,        lon: lon0 },
    { lat: lat0 + step, lon: lon0 },
    { lat: lat0,        lon: lon0 + step },
    { lat: lat0 + step, lon: lon0 + step },
  ];

  // Current hour index
  const now = new Date();
  const hourStr = now.toISOString().slice(0, 13) + ":00";
  const timeIdx = (grid.points[0]?.times ?? []).indexOf(hourStr);
  if (timeIdx === -1) return null;

  // Bilinear interpolation fractions
  const fi = (lat - lat0) / step;
  const fj = (lon - lon0) / step;
  const weights = [
    (1 - fi) * (1 - fj),   // SW
    fi       * (1 - fj),   // NW
    (1 - fi) * fj,          // SE
    fi       * fj,           // NE
  ];

  let u = 0, v = 0, gust = 0, temp = 0;
  for (let k = 0; k < 4; k++) {
    const pt = grid.points.find(
      p => Math.abs(p.lat - corners[k].lat) < 0.001 &&
           Math.abs(p.lon - corners[k].lon) < 0.001
    );
    if (!pt) continue;
    u    += weights[k] * (pt.u10[timeIdx]   ?? 0);
    v    += weights[k] * (pt.v10[timeIdx]   ?? 0);
    gust += weights[k] * (pt.gusts[timeIdx] ?? 0);
    temp += weights[k] * (pt.temp[timeIdx]  ?? 0);
  }

  // CRITICAL: interpolate U and V separately, then compute speed
  const speed_ms     = Math.sqrt(u * u + v * v);
  const direction_deg = ((Math.atan2(-u, -v) * 180 / Math.PI) + 360) % 360;

  return {
    speed_ms:      Math.round(speed_ms * 10) / 10,
    direction_deg: Math.round(direction_deg),
    gust_ms:       Math.round(gust * 10) / 10,
    temp_c:        Math.round(temp * 10) / 10,
    beaufort:      speedToBeaufort(speed_ms),
  };
}

// ---- 2. DEM-based wind sheltering ----------------------------------

const _demCache = {};

async function getDemTile(lat, lon) {
  const lat0 = Math.floor(lat);
  const lon0 = Math.floor(lon);
  const latH = lat0 >= 0 ? "N" : "S";
  const lonH = lon0 >= 0 ? "E" : "W";
  const name = `${latH}${String(Math.abs(lat0)).padStart(2,"0")}` +
               `${lonH}${String(Math.abs(lon0)).padStart(3,"0")}`;

  if (!_demCache[name]) {
    try {
      const res = await fetch(`${BASE}/dem/tiles/${name}.json`);
      if (!res.ok) { _demCache[name] = null; return null; }
      _demCache[name] = await res.json();
    } catch { _demCache[name] = null; return null; }
  }
  return _demCache[name];
}

/**
 * Get elevation (m) at (lat, lon) from the pre-baked DEM grid.
 */
async function getElevation(lat, lon) {
  const tile = await getDemTile(lat, lon);
  if (!tile) return 0;

  const row = Math.floor((tile.lat + 1 - lat) / tile.dlat);
  const col = Math.floor((lon - tile.lon)      / tile.dlon);
  const r   = Math.max(0, Math.min(row, tile.rows - 1));
  const c   = Math.max(0, Math.min(col, tile.cols - 1));
  return tile.data[r * tile.cols + c] ?? 0;
}

/**
 * Estimate wind sheltering factor [0..1] from upwind terrain.
 * Returns 1.0 = fully exposed, 0.3 = well sheltered.
 */
async function shelteringFactor(lat, lon, windDirectionDeg) {
  const SAMPLES = 5;
  const DIST_M  = 300;   // 300 m between sample points

  const userElev = await getElevation(lat, lon);
  let maxRidgeHeight = 0;

  const upwindBearing = (windDirectionDeg + 180) % 360;   // direction wind comes FROM

  for (let i = 1; i <= SAMPLES; i++) {
    const dist = i * DIST_M;
    const [sLat, sLon] = offsetPoint(lat, lon, upwindBearing, dist);
    const elev = await getElevation(sLat, sLon);
    maxRidgeHeight = Math.max(maxRidgeHeight, elev - userElev);
  }

  if (maxRidgeHeight <= 0) return 1.0;                       // flat/open water
  if (maxRidgeHeight >= 50) return 0.3;                      // heavily sheltered
  return 1.0 - (maxRidgeHeight / 50) * 0.7;
}

/**
 * Offset a (lat, lon) point by `dist` metres in direction `bearing` degrees.
 */
function offsetPoint(lat, lon, bearing, dist) {
  const R   = 6_371_000;
  const d   = dist / R;
  const b   = (bearing * Math.PI) / 180;
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;

  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(d) +
    Math.cos(lat1) * Math.sin(d) * Math.cos(b)
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(b) * Math.sin(d) * Math.cos(lat1),
      Math.cos(d) - Math.sin(lat1) * Math.sin(lat2)
    );

  return [(lat2 * 180) / Math.PI, (lon2 * 180) / Math.PI];
}

// ---- 3. Corrected wind for vessel position --------------------------

async function getLocalWind(lat, lon) {
  const raw = await getWindAt(lat, lon);
  if (!raw) return null;

  const shelter = await shelteringFactor(lat, lon, raw.direction_deg);
  return {
    ...raw,
    speed_ms_local:    Math.round(raw.speed_ms * shelter * 10) / 10,
    sheltering_factor: Math.round(shelter * 100) / 100,
    is_sheltered:      shelter < 0.7,
  };
}

// ---- 4. Utilities ---------------------------------------------------

function speedToBeaufort(mps) {
  const t = [0.3,1.6,3.4,5.5,8.0,10.8,13.9,17.2,20.8,24.5,28.5,32.7];
  return t.findIndex(v => mps < v);
}

// ---- 5. Example usage with Mapbox -----------------------------------
/*
map.on("click", async (e) => {
  const { lat, lng } = e.lngLat;
  const wind = await getLocalWind(lat, lng);
  if (wind) {
    console.log(`Wind: ${wind.speed_ms_local} m/s (Bft ${wind.beaufort})`);
    console.log(`From: ${wind.direction_deg}°`);
    console.log(`Gust: ${wind.gust_ms} m/s`);
    console.log(`Sheltered: ${wind.is_sheltered}`);
  }
});
*/
```
