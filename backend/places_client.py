"""
Google Places API client — fetches nearby dentists for competitor analysis
and quarterly brand-negative database sync.

Uses the Places Nearby Search API (legacy) which supports type=dentist and
pagination via next_page_token, returning up to 60 results (3 pages × 20).

Center point: Grafton Dental Care (42.5584, -71.6864)
Place ID:     ChIJ77R6FMYO5IkRPfd2zsowDms

Two entry points:
  fetch_nearby_dentists(api_key, radius_meters, max_pages)
      One-shot fetch used by the competitor analysis wizard step (live call).

  sync_nearby_practices(api_key)
      Quarterly DB sync: fetches all 4 radius bands (5/10/15/20 mi),
      computes actual haversine distance, derives brand_stems, upserts into
      the nearby_practices table via database helpers.
"""
import logging
import math
import re
import time
import uuid
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Grafton Dental Care lat/lng (used as search center)
_GDC_LAT = 42.5584
_GDC_LNG = -71.6864
_GDC_PLACE_ID = "ChIJ77R6FMYO5IkRPfd2zsowDms"
_GDC_NAME_NORMALIZED = "grafton dental care"

# Places Nearby Search endpoint
_NEARBY_URL = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
# Places Details endpoint (used to get lat/lng for each result)
_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"

# Radius bands for quarterly sync (miles → meters)
_SYNC_BANDS_MI = [5, 10, 15, 20]
_MI_TO_M = 1609.344


def fetch_nearby_dentists(
    api_key: str,
    radius_meters: int = 24_000,   # ~15 miles
    max_pages: int = 3,
) -> list[dict]:
    """
    Fetch all dental practices within radius_meters of Grafton Dental Care.

    Returns a list of dicts with keys:
        name, place_id, vicinity (address), rating, user_ratings_total,
        business_status, types

    Excludes Grafton Dental Care itself.
    Paginates automatically (up to max_pages × 20 = 60 results).
    Returns [] on API error (non-fatal — caller falls back to Claude-only mode).
    """
    if not api_key:
        logger.warning("[places] No API key configured — skipping Places fetch")
        return []

    results = []
    params = {
        "location": f"{_GDC_LAT},{_GDC_LNG}",
        "radius": radius_meters,
        "type": "dentist",
        "key": api_key,
    }

    for page in range(max_pages):
        try:
            resp = requests.get(_NEARBY_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[places] API request failed on page {page + 1}: {e}")
            break

        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            logger.warning(f"[places] API status={status} on page {page + 1}: {data.get('error_message', '')}")
            break

        for place in data.get("results", []):
            # Skip ourselves
            if place.get("place_id") == _GDC_PLACE_ID:
                continue
            name = place.get("name", "")
            if name.lower().strip() == _GDC_NAME_NORMALIZED:
                continue

            results.append({
                "name":               name,
                "place_id":           place.get("place_id", ""),
                "vicinity":           place.get("vicinity", ""),
                "rating":             place.get("rating"),
                "user_ratings_total": place.get("user_ratings_total", 0),
                "business_status":    place.get("business_status", ""),
                "types":              place.get("types", []),
            })

        # Paginate — next_page_token takes ~2s to activate
        next_token = data.get("next_page_token")
        if not next_token or page + 1 >= max_pages:
            break
        time.sleep(2)
        params = {"pagetoken": next_token, "key": api_key}

    logger.info(f"[places] Fetched {len(results)} nearby dentists (radius={radius_meters}m)")
    return results


def format_for_claude(places: list[dict]) -> str:
    """
    Format the Places results into a compact text block to inject into the
    Claude competitor analysis prompt. Sorted by rating (highest first) so
    Claude sees the most prominent practices at the top.
    """
    if not places:
        return ""

    # Sort: open + highly rated first
    sorted_places = sorted(
        places,
        key=lambda p: (
            0 if p.get("business_status") == "OPERATIONAL" else 1,
            -(p.get("rating") or 0),
            -(p.get("user_ratings_total") or 0),
        )
    )

    lines = []
    for p in sorted_places:
        rating_str = f"★{p['rating']}" if p.get("rating") else "no rating"
        reviews_str = f"({p['user_ratings_total']} reviews)" if p.get("user_ratings_total") else ""
        status_str = "" if p.get("business_status") == "OPERATIONAL" else f" [{p.get('business_status', '')}]"
        lines.append(f"  • {p['name']} — {p['vicinity']} — {rating_str} {reviews_str}{status_str}")

    return "\n".join(lines)


# ── Quarterly DB sync ─────────────────────────────────────────────────────────

def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _derive_brand_stems(name: str) -> list[str]:
    """
    Derive brand stems from a practice name for use as Google Ads negative keywords.
    Returns 1–3 lowercase strings covering likely search variations.

    Examples:
      "Aspen Dental - Leominster, MA" → ["aspen dental leominster", "aspen dental"]
      "Emerson Dental"                → ["emerson dental"]
      "David J. Gianino, DDS"        → ["gianino dds", "david gianino"]
    """
    # Strip location suffix after dash or comma
    clean = re.sub(r"\s*[-–—,|]\s*(ma|massachusetts|.*?\bma\b.*)?$", "", name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"\s*[-–—,|]\s*.*$", "", clean).strip()

    # Normalize: lowercase, remove punctuation, collapse spaces
    norm = re.sub(r"[^a-z0-9 ]+", " ", clean.lower()).strip()
    norm = re.sub(r" {2,}", " ", norm)

    stems: list[str] = []
    if norm:
        stems.append(norm)

    # Also add the full normalized original name (catches "aspen dental leominster ma")
    full_norm = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()
    full_norm = re.sub(r" {2,}", " ", full_norm)
    if full_norm and full_norm != norm:
        stems.append(full_norm)

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for s in stems:
        if s not in seen and len(s) >= 3:
            seen.add(s)
            result.append(s)

    return result[:3]


def _fetch_place_latlng(place_id: str, api_key: str) -> tuple[float, float]:
    """
    Fetch lat/lng for a place_id via the Places Details API.
    Returns (0.0, 0.0) on failure (non-fatal).
    """
    try:
        resp = requests.get(
            _DETAILS_URL,
            params={"place_id": place_id, "fields": "geometry", "key": api_key},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()
        loc = data.get("result", {}).get("geometry", {}).get("location", {})
        return float(loc.get("lat", 0)), float(loc.get("lng", 0))
    except Exception as e:
        logger.warning(f"[places] Details fetch failed for {place_id}: {e}")
        return 0.0, 0.0


def sync_nearby_practices(api_key: str) -> dict:
    """
    Quarterly sync: fetch all dental practices at 4 radius bands (5/10/15/20 mi),
    compute actual haversine distance, derive brand_stems, upsert into nearby_practices DB.

    Returns a summary dict: {synced, skipped, errors, run_id, bands: {5:n, 10:n, 15:n, 20:n}}
    """
    if not api_key:
        logger.warning("[places_sync] No API key — skipping sync")
        return {"ok": False, "error": "No API key configured"}

    from database import upsert_nearby_practice

    run_id = str(uuid.uuid4())[:8]
    seen_place_ids: set[str] = set()   # deduplicate across bands
    synced = 0
    errors = 0
    band_counts: dict[int, int] = {b: 0 for b in _SYNC_BANDS_MI}

    for band_mi in _SYNC_BANDS_MI:
        radius_m = int(band_mi * _MI_TO_M)
        logger.info(f"[places_sync] Fetching band {band_mi}mi (radius={radius_m}m)")

        params = {
            "location": f"{_GDC_LAT},{_GDC_LNG}",
            "radius": radius_m,
            "type": "dentist",
            "key": api_key,
        }

        for page in range(3):  # up to 60 results per band
            try:
                resp = requests.get(_NEARBY_URL, params=params, timeout=12)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(f"[places_sync] API request failed band={band_mi}mi page={page+1}: {e}")
                errors += 1
                break

            status = data.get("status")
            if status not in ("OK", "ZERO_RESULTS"):
                logger.warning(f"[places_sync] API status={status} band={band_mi}mi")
                break

            for place in data.get("results", []):
                pid = place.get("place_id", "")

                # Skip ourselves
                if pid == _GDC_PLACE_ID:
                    continue
                name = place.get("name", "")
                if name.lower().strip() == _GDC_NAME_NORMALIZED:
                    continue

                # Skip already-seen in a smaller band (keep smallest band assignment)
                if pid in seen_place_ids:
                    continue
                seen_place_ids.add(pid)

                # Get lat/lng — nearbysearch returns geometry on results
                geo = place.get("geometry", {}).get("location", {})
                lat = float(geo.get("lat", 0))
                lng = float(geo.get("lng", 0))

                # If geometry missing from nearbysearch result, fetch via Details API
                if not lat and not lng:
                    lat, lng = _fetch_place_latlng(pid, api_key)
                    time.sleep(0.1)  # be polite

                distance = (
                    _haversine_miles(_GDC_LAT, _GDC_LNG, lat, lng)
                    if lat and lng else 0.0
                )

                stems = _derive_brand_stems(name)

                try:
                    upsert_nearby_practice(
                        place_id=pid,
                        name=name,
                        vicinity=place.get("vicinity", ""),
                        lat=lat,
                        lng=lng,
                        distance_miles=round(distance, 2),
                        radius_band=band_mi,
                        rating=float(place.get("rating") or 0),
                        review_count=int(place.get("user_ratings_total") or 0),
                        business_status=place.get("business_status", "OPERATIONAL"),
                        brand_stems=stems,
                        sync_run_id=run_id,
                    )
                    synced += 1
                    band_counts[band_mi] += 1
                except Exception as e:
                    logger.warning(f"[places_sync] upsert failed for '{name}': {e}")
                    errors += 1

            # Paginate
            next_token = data.get("next_page_token")
            if not next_token or page + 1 >= 3:
                break
            time.sleep(2)  # next_page_token needs ~2s to activate
            params = {"pagetoken": next_token, "key": api_key}

    logger.info(
        f"[places_sync] Done run_id={run_id}: synced={synced} errors={errors} "
        f"bands={band_counts}"
    )
    return {
        "ok": True,
        "run_id": run_id,
        "synced": synced,
        "errors": errors,
        "bands": band_counts,
    }
