"""
geo_distance.py — Haversine distance helpers for GDC geo intelligence.

Practice anchor: Grafton MA  42.2237, -71.6838
Distance bands (miles): 0-5, 5-10, 10-15, 15-25, 25+
"""
from __future__ import annotations
import math

# ── Practice anchor ───────────────────────────────────────────────────────────
PRACTICE_LAT = 42.2237
PRACTICE_LNG = -71.6838

# ── Known city lat/lng table (Worcester-area MA + MetroWest) ──────────────────
# Used to convert city names → coordinates for distance-band bucketing.
# Source: US Census geocoder + manual verification.
CITY_LATLNG: dict[str, tuple[float, float]] = {
    # Core service area
    "grafton":        (42.2237, -71.6838),
    "shrewsbury":     (42.2959, -71.7148),
    "westborough":    (42.2693, -71.6162),
    "northborough":   (42.3195, -71.6440),
    "southborough":   (42.3037, -71.7770),
    "millbury":       (42.1934, -71.7673),
    "upton":          (42.1762, -71.6009),
    "hopedale":       (42.1334, -71.5537),
    "milford":        (42.1398, -71.5187),
    "worcester":      (42.2626, -71.8023),
    "auburn":         (42.1973, -71.8354),
    "oxford":         (42.1182, -71.8640),
    "sutton":         (42.1543, -71.7560),
    "mendon":         (42.0987, -71.5551),
    "uxbridge":       (42.0748, -71.6318),
    "douglas":        (42.0526, -71.7454),
    "holden":         (42.3565, -71.8618),
    # MetroWest / Route 9 corridor
    "framingham":     (42.2793, -71.4162),
    "natick":         (42.2840, -71.3495),
    "marlborough":    (42.3459, -71.5523),
    "hudson":         (42.3918, -71.5662),
    "berlin":         (42.3812, -71.6373),
    "clinton":        (42.4159, -71.6818),
    "sterling":       (42.4337, -71.7682),
    "leominster":     (42.5251, -71.7598),
    "fitchburg":      (42.5834, -71.8023),
    "gardner":        (42.5751, -71.9979),
    "wachusett":      (42.4987, -71.9010),
    # South / SW
    "holliston":      (42.2001, -71.4346),
    "hopkinton":      (42.2287, -71.5224),
    "medway":         (42.1487, -71.3935),
    "millis":         (42.1671, -71.3579),
    "bellingham":     (42.0851, -71.4748),
    "franklin":       (42.0835, -71.3965),
    "medfield":       (42.1848, -71.3054),
    "norwood":        (42.1945, -71.1995),
    # North
    "leicester":      (42.2459, -71.9126),
    "spencer":        (42.2459, -71.9882),
    "paxton":         (42.3109, -71.9326),
    "rutland":        (42.3837, -71.9576),
    # Grafton area ZIPs (used for display)
    "northborough ma": (42.3195, -71.6440),
    "westborough ma":  (42.2693, -71.6162),
    "grafton ma":      (42.2237, -71.6838),
    "shrewsbury ma":   (42.2959, -71.7148),
}

# ── MA ZIP → lat/lng (service-area ZIPs) ─────────────────────────────────────
ZIP_LATLNG: dict[str, tuple[float, float]] = {
    "01519": (42.2237, -71.6838),  # Grafton
    "01536": (42.2237, -71.6838),  # Grafton (alt)
    "01545": (42.2959, -71.7148),  # Shrewsbury
    "01581": (42.2693, -71.6162),  # Westborough
    "01532": (42.3195, -71.6440),  # Northborough
    "01772": (42.3037, -71.7770),  # Southborough
    "01527": (42.1934, -71.7673),  # Millbury
    "01568": (42.1762, -71.6009),  # Upton
    "01747": (42.1334, -71.5537),  # Hopedale
    "01757": (42.1398, -71.5187),  # Milford
    "01605": (42.2626, -71.8023),  # Worcester
    "01606": (42.2959, -71.8023),  # Worcester N
    "01607": (42.2293, -71.8023),  # Worcester S
    "01608": (42.2626, -71.8023),  # Worcester downtown
    "01609": (42.2793, -71.8187),  # Worcester W
    "01610": (42.2459, -71.7765),  # Worcester SE
    "01501": (42.1973, -71.8354),  # Auburn
    "01069": (42.1543, -71.7560),  # Sutton (01590)
    "01590": (42.1543, -71.7560),  # Sutton
    "01749": (42.3918, -71.5662),  # Hudson
    "01752": (42.3459, -71.5523),  # Marlborough
    "01701": (42.2793, -71.4162),  # Framingham
    "01702": (42.2793, -71.4162),  # Framingham
    "01746": (42.2001, -71.4346),  # Holliston
    "01748": (42.2287, -71.5224),  # Hopkinton
    "01756": (42.0987, -71.5551),  # Mendon
    "01527": (42.1762, -71.6009),  # Upton
    "02019": (42.0851, -71.4748),  # Bellingham
    "02038": (42.0835, -71.3965),  # Franklin
    "01757": (42.1398, -71.5187),  # Milford
}

# ── Distance bands ────────────────────────────────────────────────────────────
BANDS = ["0-5", "5-10", "10-15", "15-25", "25+"]


def haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def distance_from_practice(lat: float, lng: float) -> float:
    """Miles from the practice (Grafton MA)."""
    return haversine_miles(PRACTICE_LAT, PRACTICE_LNG, lat, lng)


def distance_band(miles: float) -> str:
    """Bucket a distance in miles into a band string."""
    if miles < 5:
        return "0-5"
    if miles < 10:
        return "5-10"
    if miles < 15:
        return "10-15"
    if miles < 25:
        return "15-25"
    return "25+"


def city_to_distance_band(city_name: str) -> str:
    """
    Convert a city name (as returned by GAds geographic_view) to a distance band.
    Falls back to "unknown" if not in the lookup table.

    GAds returns names like "Worcester, Massachusetts" or "Worcester".
    We normalise to lowercase first-token for lookup.
    """
    if not city_name:
        return "unknown"
    # Normalise: "Worcester, Massachusetts, United States" → "worcester"
    key = city_name.lower().split(",")[0].strip()
    coords = CITY_LATLNG.get(key)
    if coords is None:
        # Try full lowercased string (handles "northborough ma" style)
        key2 = city_name.lower().strip()
        coords = CITY_LATLNG.get(key2)
    if coords is None:
        return "unknown"
    miles = distance_from_practice(*coords)
    return distance_band(miles)


def zip_to_distance_band(zip_code: str) -> str:
    """Convert a 5-digit US ZIP to a distance band. Falls back to 'unknown'."""
    coords = ZIP_LATLNG.get((zip_code or "").strip())
    if coords is None:
        return "unknown"
    return distance_band(distance_from_practice(*coords))


def city_distance_miles(city_name: str) -> float | None:
    """Return miles from practice for a city name, or None if unknown."""
    key = (city_name or "").lower().split(",")[0].strip()
    coords = CITY_LATLNG.get(key)
    if coords is None:
        return None
    return round(distance_from_practice(*coords), 1)
