"""
fetch_harbors.py
================
Fetches Swedish guest harbors, nature harbors, marinas, moorings and
anchorages from OpenStreetMap (Overpass API) and Naturvårdsverket (WFS).
Deduplicates across sources and classifies each harbor.

Output:
  api/harbors.json       Complete harbor dataset for all of Sweden

Run weekly — harbor data changes infrequently.
"""

import json
import math
import os
import sys
import time

import requests

try:
    from pyproj import Transformer
    _SWEREF_TO_WGS84 = Transformer.from_crs("EPSG:3006", "EPSG:4326",
                                              always_xy=True)
    PYPROJ_AVAILABLE = True
except ImportError:
    PYPROJ_AVAILABLE = False
    print("WARNING: pyproj not installed — Naturvårdsverket coordinates "
          "will not be reprojected. Run: pip install pyproj")

OUT_PATH       = os.path.join("api", "harbors.json")
OVERPASS_URL   = "https://overpass-api.de/api/interpreter"
NV_WFS_URL     = "https://geodata.naturvardsverket.se/anordningar_friluftsliv/wfs"
DEDUP_M        = 50      # metres — closer than this → same harbor
MAX_RETRIES    = 3
RETRY_DELAY    = 30


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url, params=None, timeout=120):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "BoatNavApp/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"    Retry {attempt}: {e}")
            time.sleep(RETRY_DELAY)


def http_post(url, data, timeout=180):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, data=data, timeout=timeout,
                              headers={"User-Agent": "BoatNavApp/1.0"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            print(f"    Retry {attempt}: {e}")
            time.sleep(RETRY_DELAY)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(name, tags, source, nv_typ=None):
    n = (name or "").lower()
    if "gästhamn" in n or "gasthamn" in n:
        return "gästhamn"
    if "naturhamn" in n:
        return "naturhamn"

    if nv_typ:
        m = {"hamn": "hamn", "brygga": "brygga",
             "ankringsplats": "ankringsplats", "pir": "pir",
             "fyr": "fyr", "kajakramp": "kajakramp"}
        return m.get(nv_typ.lower(), "hamn")

    cat = tags.get("seamark:harbour:category", "")
    if cat == "marina_no_facilities":
        return "naturhamn"
    if cat == "marina":
        return "marina"
    if tags.get("mooring") == "guest":
        return "gästhamn"
    if tags.get("mooring") == "buoy":
        return "förtöjningsboj"
    if tags.get("seamark:type") == "anchorage":
        return "ankringsplats"
    if tags.get("seamark:type") == "mooring":
        return "förtöjningsboj"

    if tags.get("leisure") == "marina":
        has_services = any(tags.get(k) for k in
                           ("electricity", "shower", "toilets", "fuel"))
        if tags.get("fee") == "yes" or has_services:
            return "gästhamn"
        if tags.get("fee") == "no":
            return "naturhamn"
        return "marina"

    if tags.get("leisure") == "harbour" or tags.get("harbour") == "yes":
        return "hamn"

    return "unknown"


# ---------------------------------------------------------------------------
# OSM source
# ---------------------------------------------------------------------------

OSM_QUERY = """
[out:json][timeout:120];
area["ISO3166-1"="SE"]->.sweden;
(
  nwr["leisure"="marina"](area.sweden);
  nwr["leisure"="harbour"](area.sweden);
  nwr["seamark:type"="harbour"](area.sweden);
  nwr["seamark:type"="anchorage"](area.sweden);
  nwr["seamark:type"="mooring"](area.sweden);
  nwr["harbour"="yes"](area.sweden);
  nwr["mooring"~"."](area.sweden);
  nwr["name"~"[Gg]ästhamn"](area.sweden);
  nwr["name"~"[Nn]aturhamn"](area.sweden);
);
out center;
"""

def _coalesce(tags, *keys):
    for k in keys:
        if tags.get(k):
            return tags[k]
    return None

def fetch_osm():
    print("  Querying OSM Overpass API…")
    raw = http_post(OVERPASS_URL, {"data": OSM_QUERY})
    elements = raw.get("elements", [])
    print(f"  {len(elements):,} raw OSM elements")

    records = []
    for el in elements:
        tags = el.get("tags", {})
        if not tags:
            continue

        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            lat, lon = c.get("lat"), c.get("lon")

        if lat is None or lon is None:
            continue

        name = _coalesce(tags, "name", "name:sv", "name:en")

        mooring_parts = []
        for mk in ("mooring", "seamark:mooring:category"):
            if tags.get(mk):
                mooring_parts.append(f"{mk}={tags[mk]}")

        description = _coalesce(tags, "description", "description:sv",
                                "description:en")

        rec = {
            "source":     "osm",
            "source_id":  f"{el['type']}/{el['id']}",
            "lat":        round(lat, 7),
            "lon":        round(lon, 7),
            "name":       name,
            "type":       classify(name, tags, "osm"),
        }
        if description:
            rec["description"] = description

        # Services
        svc = {}
        for k in ("electricity", "water", "fuel", "toilets", "shower",
                  "pump_out", "wifi", "slipway", "crane", "boat_repair",
                  "waste_disposal", "laundry", "restaurant"):
            if tags.get(k):
                svc[k] = tags[k]
        if svc:
            rec["services"] = svc

        # Navigation
        nav = {}
        for k, sk in (("depth", "depth"), ("maxdepth", "max_depth"),
                      ("maxlength", "max_length"), ("maxdraught", "max_draught"),
                      ("maxwidth", "max_width"), ("max_stay", "max_stay")):
            if tags.get(k):
                nav[sk] = tags[k]
        cap = tags.get("capacity:boats") or tags.get("capacity")
        if cap:
            nav["capacity"] = cap
        if mooring_parts:
            nav["mooring_type"] = "; ".join(mooring_parts)
        if nav:
            rec["navigation"] = nav

        # Access
        acc = {}
        for k in ("fee", "charge", "opening_hours", "access"):
            if tags.get(k):
                acc[k] = tags[k]
        if acc:
            rec["access"] = acc

        # Contact
        con = {}
        for k, sk in (("website", "website"), ("contact:website", "website"),
                      ("phone", "phone"), ("contact:phone", "phone"),
                      ("operator", "operator"), ("wikipedia", "wikipedia"),
                      ("wikidata", "wikidata"),
                      ("addr:city", "city"), ("addr:postcode", "postcode")):
            if tags.get(k) and sk not in con:
                con[sk] = tags[k]
        if con:
            rec["contact"] = con

        records.append(rec)

    print(f"  {len(records):,} OSM harbor records")
    return records


# ---------------------------------------------------------------------------
# Naturvårdsverket source
# ---------------------------------------------------------------------------

NV_BOAT_TYPES = "Typ IN ('Hamn','Brygga','Ankringsplats','Pir','Fyr','Livboj')"

NV_PARAMS = {
    "service":      "WFS",
    "version":      "2.0.0",
    "request":      "GetFeature",
    "typeNames":    "ANORDNINGAR",
    "CQL_FILTER":   NV_BOAT_TYPES,
    "srsName":      "EPSG:3006",
    "outputFormat": "application/json",
    "count":        100_000,
}

def fetch_naturvardsverket():
    print("  Querying Naturvårdsverket WFS…")
    try:
        data = http_get(NV_WFS_URL, params=NV_PARAMS, timeout=120)
    except Exception as e:
        print(f"  WARNING: NV WFS failed ({e}). Skipping.")
        return []

    features = data.get("features", [])
    print(f"  {len(features):,} NV features")

    if not PYPROJ_AVAILABLE:
        print("  WARNING: pyproj missing — skipping NV coordinates")
        return []

    records = []
    for feat in features:
        props = feat.get("properties", {})
        geom  = feat.get("geometry", {})
        if not geom or geom.get("type") != "Point":
            continue

        x, y = geom["coordinates"][0], geom["coordinates"][1]
        lon, lat = _SWEREF_TO_WGS84.transform(x, y)

        name     = props.get("Anordningsnamn") or props.get("ANORD_NAMN")
        nv_typ   = props.get("Typ") or props.get("TYP", "")
        nv_under = props.get("Undertyp") or props.get("UNDERTYP", "")
        desc     = props.get("Beskrivning") or props.get("BESKRIVN")
        area_nm  = props.get("Skyddat_område") or props.get("SKOMRDE")
        area_id  = props.get("Skyddat_område_ID") or props.get("SKOMRDE_ID")
        geoqual  = props.get("Geometrikvalitet") or props.get("GEOKVALITE")
        anord_id = props.get("Anordning_ID") or props.get("ANORD_ID", "")

        full_desc = desc or ""
        if nv_under and nv_under != nv_typ:
            full_desc = f"[{nv_under}] {full_desc}".strip()

        rec = {
            "source":     "naturvardsverket",
            "source_id":  anord_id,
            "lat":        round(lat, 7),
            "lon":        round(lon, 7),
            "name":       name,
            "type":       classify(name, {}, "naturvardsverket", nv_typ),
            "meta": {
                k: v for k, v in {
                    "protected_area":     area_nm,
                    "protected_area_id":  area_id,
                    "description":        full_desc or None,
                    "position_accuracy":  geoqual,
                }.items() if v
            } or None,
        }
        records.append(rec)

    print(f"  {len(records):,} Naturvårdsverket records")
    return records


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def deduplicate(records, threshold_m=DEDUP_M):
    PRIORITY = {"naturvardsverket": 0, "osm": 1}
    records = sorted(records, key=lambda r: PRIORITY.get(r["source"], 9))
    kept = []
    for rec in records:
        lat1, lon1 = rec.get("lat"), rec.get("lon")
        if lat1 is None:
            kept.append(rec)
            continue
        dup = False
        for ex in kept:
            lat2, lon2 = ex.get("lat"), ex.get("lon")
            if lat2 is None:
                continue
            if haversine(lat1, lon1, lat2, lon2) <= threshold_m:
                # Merge missing fields
                for section in ("services", "navigation", "access",
                                "contact", "meta"):
                    if section in rec and section not in ex:
                        ex[section] = rec[section]
                ex.setdefault("also_in", []).append(
                    f"{rec['source']}:{rec['source_id']}"
                )
                dup = True
                break
        if not dup:
            kept.append(rec)
    return kept


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Sweden Harbor Data Fetcher")
    print("Sources: OSM (ODbL) + Naturvårdsverket (CC0)")
    print("=" * 60)

    os.makedirs(os.path.dirname(OUT_PATH) or ".", exist_ok=True)

    print("\n[1/3] Fetching OpenStreetMap data…")
    osm = fetch_osm()

    print("\n[2/3] Fetching Naturvårdsverket data…")
    nv = fetch_naturvardsverket()

    all_rec = osm + nv
    print(f"\n[3/3] Deduplicating {len(all_rec):,} records "
          f"(threshold {DEDUP_M} m)…")
    harbors = deduplicate(all_rec)
    removed = len(all_rec) - len(harbors)
    print(f"  Removed {removed:,} duplicates → {len(harbors):,} unique")

    # Type summary
    types = {}
    for h in harbors:
        t = h.get("type", "unknown")
        types[t] = types.get(t, 0) + 1
    for t, c in sorted(types.items(), key=lambda x: -x[1]):
        print(f"  {t:<22} {c:>5}")

    output = {
        "description": ("Sweden harbor dataset — guest harbors, nature harbors, "
                        "marinas, anchorages, moorings, docks and piers."),
        "sources": [
            {"id": "osm",  "name": "OpenStreetMap",
             "license": "ODbL", "count": len(osm),
             "attribution": "© OpenStreetMap contributors",
             "url": "https://www.openstreetmap.org/copyright"},
            {"id": "naturvardsverket",
             "name": "Naturvårdsverket — Leder och friluftsanordningar",
             "license": "CC0", "count": len(nv),
             "url": "https://geodata.naturvardsverket.se/nedladdning/friluftsliv/"},
        ],
        "generated_at":       time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dedup_threshold_m":  DEDUP_M,
        "total":              len(harbors),
        "by_type":            types,
        "harbors":            harbors,
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"\nSaved {len(harbors):,} harbors to {OUT_PATH} ({size_kb:.0f} KB)")
    print("Attribution required: © OpenStreetMap contributors (ODbL)")


if __name__ == "__main__":
    main()
