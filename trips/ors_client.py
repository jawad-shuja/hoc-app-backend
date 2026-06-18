"""OpenRouteService API client — geocoding and HGV routing."""

from __future__ import annotations

import requests
from django.conf import settings

ORS_BASE = "https://api.openrouteservice.org"


class OrsError(Exception):
    pass


def _api_key() -> str:
    key = settings.OPENROUTESERVICE_API_KEY
    if not key:
        raise OrsError("OPENROUTESERVICE_API_KEY is not set in .env")
    return key


def geocode(address: str) -> tuple[float, float]:
    """Return (longitude, latitude) for a free-text address."""
    try:
        resp = requests.get(
            f"{ORS_BASE}/geocode/search",
            params={"api_key": _api_key(), "text": address, "size": 1},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OrsError(f"Geocoding failed for {address!r}: {exc}") from exc

    features = resp.json().get("features", [])
    if not features:
        raise OrsError(f"No results found for location: {address!r}")

    lon, lat = features[0]["geometry"]["coordinates"]
    return float(lon), float(lat)


def reverse_geocode(lon: float, lat: float) -> str:
    """Return 'City, ST' (or best available label) for coordinates, '' on failure."""
    try:
        resp = requests.get(
            f"{ORS_BASE}/geocode/reverse",
            params={
                "api_key": _api_key(),
                "point.lon": round(lon, 6),
                "point.lat": round(lat, 6),
                "size": 1,
            },
            timeout=8,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return ""
    features = resp.json().get("features", [])
    if not features:
        return ""
    props = features[0].get("properties", {})
    locality  = props.get("locality") or props.get("name") or ""
    region_a  = props.get("region_a") or ""
    if locality and region_a:
        return f"{locality}, {region_a}"
    return locality or region_a


def get_route(
    origin: tuple[float, float],
    destination: tuple[float, float],
) -> dict:
    """Route between two (lon, lat) pairs using the HGV profile.

    Returns::

        {
            "distance_miles": float,
            "duration_hours": float,
            "geometry": [[lon, lat], ...],
        }
    """
    try:
        resp = requests.post(
            f"{ORS_BASE}/v2/directions/driving-hgv/geojson",
            headers={
                "Authorization": _api_key(),
                "Content-Type": "application/json",
            },
            json={"coordinates": [list(origin), list(destination)]},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OrsError(f"Routing request failed: {exc}") from exc

    features = resp.json().get("features", [])
    if not features:
        raise OrsError("ORS returned no route features")

    summary = features[0]["properties"].get("summary", {})
    geometry_coords = features[0]["geometry"]["coordinates"]  # [[lon, lat], ...]

    return {
        "distance_miles": summary.get("distance", 0.0) / 1609.344,
        "duration_hours": summary.get("duration", 0.0) / 3600.0,
        "geometry": geometry_coords,
    }
