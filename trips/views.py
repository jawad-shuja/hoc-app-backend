from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rest_framework import status as http_status
from rest_framework.response import Response
from rest_framework.views import APIView

from .hos_engine import Segment, compute_daily_totals, simulate_trip
from .ors_client import OrsError, geocode, get_route, reverse_geocode
from .serializers import TripRequestSerializer

# Location strings that carry no geographic information
_GENERIC_LOCS = {"en route", "rest area", "fuel stop", "pickup location", "dropoff location", ""}


class TripPlanView(APIView):
    """POST /api/trips/

    Geocodes the three locations, fetches HGV routes via OpenRouteService,
    runs the FMCSA HOS simulation, and returns route geometry, stop markers,
    all duty-status segments, and per-day log totals.
    """

    def post(self, request):
        serializer = TripRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        # 1. Geocode
        try:
            current_coords = geocode(d["current_location"])
            pickup_coords = geocode(d["pickup_location"])
            dropoff_coords = geocode(d["dropoff_location"])
        except OrsError as exc:
            return Response({"error": str(exc)}, status=http_status.HTTP_400_BAD_REQUEST)

        # 2. Route: current → pickup, then pickup → dropoff
        try:
            leg1 = get_route(current_coords, pickup_coords)
            leg2 = get_route(pickup_coords, dropoff_coords)
        except OrsError as exc:
            return Response({"error": str(exc)}, status=http_status.HTTP_502_BAD_GATEWAY)

        total_distance = leg1["distance_miles"] + leg2["distance_miles"]
        total_duration = leg1["duration_hours"] + leg2["duration_hours"]
        # Drop the duplicate junction point when joining leg geometries
        geometry = leg1["geometry"] + (leg2["geometry"][1:] if len(leg2["geometry"]) > 1 else [])

        # 3. HOS simulation — trip starts right now
        now = datetime.now(timezone.utc)
        segments = simulate_trip(
            driving_hours=total_duration,
            distance_miles=total_distance,
            cycle_used_hours=d["current_cycle_used"],
            start_dt=now,
        )

        # 4. Resolve human-readable city names for each segment
        resolved = _resolve_segment_locations(
            segments, geometry, total_duration,
            d["pickup_location"], d["dropoff_location"],
        )

        segments_out = _serialize_segments(segments, geometry, total_duration, resolved)
        daily_totals = compute_daily_totals(segments)
        days_out = _serialize_days(
            segments_out, daily_totals, total_distance, total_duration,
            d["pickup_location"], d["dropoff_location"],
            d["current_cycle_used"],
        )
        stops_out = _extract_stops(segments_out, pickup_coords, dropoff_coords)
        summary = _build_summary(segments_out, days_out, total_distance, total_duration)

        return Response({
            "route": {
                "geometry": geometry,
                "distance_miles": round(total_distance, 2),
                "duration_hours": round(total_duration, 2),
                "legs": [
                    {
                        "from": d["current_location"],
                        "to": d["pickup_location"],
                        "distance_miles": round(leg1["distance_miles"], 2),
                        "duration_hours": round(leg1["duration_hours"], 2),
                    },
                    {
                        "from": d["pickup_location"],
                        "to": d["dropoff_location"],
                        "distance_miles": round(leg2["distance_miles"], 2),
                        "duration_hours": round(leg2["duration_hours"], 2),
                    },
                ],
            },
            "segments": segments_out,
            "stops": stops_out,
            "days": days_out,
            "summary": summary,
        })


# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

def _resolve_segment_locations(
    segments: list[Segment],
    geometry: list,
    total_driving_hours: float,
    pickup_location: str,
    dropoff_location: str,
) -> list[str]:
    """Return a resolved city-name string for each segment.

    Pickup and dropoff use the user-supplied text.  Non-driving stops are
    reverse-geocoded.  Driving segments get an empty string (no specific city).
    """
    result: list[str] = []
    cumulative_drive_h = 0.0

    for seg in segments:
        frac = cumulative_drive_h / total_driving_hours if total_driving_hours > 0 else 0.0
        coords = _point_at_fraction(geometry, frac)

        if seg.remark == "pickup":
            name = pickup_location
        elif seg.remark == "dropoff":
            name = dropoff_location
        elif seg.status == "driving":
            name = ""
        else:
            # Non-driving stop — reverse geocode the interpolated position
            name = reverse_geocode(coords[0], coords[1])

        result.append(name)

        if seg.status == "driving":
            cumulative_drive_h += seg.duration_hours

    return result


def _format_remark(remark: str, location: str) -> str:
    """Build a display-ready remark string embedding the resolved city name."""
    loc = location or ""
    if remark == "pickup":
        return f"Pickup – {loc}" if loc else "Pickup"
    if remark == "dropoff":
        return f"Dropoff – {loc}" if loc else "Dropoff"
    if remark == "driving":
        return "Driving"
    if remark == "30-min break":
        return f"30-minute break – near {loc}" if loc else "30-minute break"
    if remark == "overnight rest":
        return f"10-hour rest break – {loc}" if loc else "10-hour rest break"
    if remark == "fuel stop":
        return f"Planned fuel stop – near {loc}" if loc else "Planned fuel stop"
    if remark == "34h restart":
        return f"34-hour restart – {loc}" if loc else "34-hour restart"
    return f"{remark} – {loc}" if loc else remark


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _point_at_fraction(geometry: list, fraction: float) -> list[float]:
    """Interpolate [lon, lat] at a fraction (0..1) along a polyline."""
    if not geometry:
        return [0.0, 0.0]
    fraction = max(0.0, min(1.0, fraction))
    idx = fraction * (len(geometry) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(geometry) - 1)
    t = idx - lo
    return [
        round(geometry[lo][0] + t * (geometry[hi][0] - geometry[lo][0]), 6),
        round(geometry[lo][1] + t * (geometry[hi][1] - geometry[lo][1]), 6),
    ]


def _serialize_segments(
    segments: list[Segment],
    geometry: list,
    total_driving_hours: float,
    resolved_locations: list[str],
) -> list[dict]:
    """Convert Segment objects to JSON-serializable dicts with enriched location/remark."""
    out = []
    cumulative_drive_h = 0.0
    for i, seg in enumerate(segments):
        frac = cumulative_drive_h / total_driving_hours if total_driving_hours > 0 else 0.0
        loc = resolved_locations[i]
        out.append({
            "status":         seg.status,
            "start":          seg.start.isoformat(),
            "end":            seg.end.isoformat(),
            "location":       loc or seg.location,
            "remark":         _format_remark(seg.remark, loc),
            "duration_hours": round(seg.duration_hours, 4),
            "distance_miles": round(seg.distance_miles, 2),
            "coords":         _point_at_fraction(geometry, frac),
        })
        if seg.status == "driving":
            cumulative_drive_h += seg.duration_hours
    return out


def _serialize_days(
    segments_out: list[dict],
    daily_totals: list[dict],
    total_distance: float,
    total_duration: float,
    pickup_location: str,
    dropoff_location: str,
    initial_cycle_used: float,
) -> list[dict]:
    """Build enriched per-day records with from/to cities, mileage, and cycle data.

    Artifact days (< 30 min total) caused by trips starting just before UTC midnight
    are removed before returning.
    """
    speed = total_distance / total_duration if total_duration > 0 else 0.0

    # Build a timeline of known (geocoded) locations with their segment start times.
    # Driving segments and generic placeholder strings are excluded.
    known_locs: list[tuple[datetime, str]] = []
    for s in segments_out:
        loc = s["location"]
        if loc and s["status"] != "driving" and loc not in _GENERIC_LOCS:
            try:
                known_locs.append((datetime.fromisoformat(s["start"]), loc))
            except ValueError:
                pass

    result = []
    cumulative_cycle = initial_cycle_used

    for i, day_data in enumerate(daily_totals):
        day_date = str(day_data["date"])
        day_start = datetime.fromisoformat(day_date + "T00:00:00+00:00")
        day_end   = day_start + timedelta(days=1)

        # from_location: day 1 always starts at the pickup city.
        # Subsequent days pick up from wherever the driver rested overnight.
        if i == 0:
            from_loc = pickup_location
        else:
            before = [loc for ts, loc in known_locs if ts < day_start]
            from_loc = before[-1] if before else pickup_location

        # to_location: last known city whose segment started before this day ended.
        until = [loc for ts, loc in known_locs if ts < day_end]
        to_loc = until[-1] if until else dropoff_location

        miles_today = round(day_data["driving"] * speed, 1)
        on_duty_today = day_data["driving"] + day_data["on_duty"]

        result.append({
            "date":             day_date,
            "off_duty":         round(day_data["off_duty"], 4),
            "sleeper":          round(day_data["sleeper"],  4),
            "driving":          round(day_data["driving"],  4),
            "on_duty":          round(day_data["on_duty"],  4),
            "from_location":    from_loc,
            "to_location":      to_loc,
            "miles_today":      miles_today,
            "cycle_used_start": round(cumulative_cycle, 2),
        })

        cumulative_cycle += on_duty_today

    # Drop artifact days where almost nothing happened (trip started near UTC midnight).
    return [
        d for d in result
        if d["off_duty"] + d["sleeper"] + d["driving"] + d["on_duty"] >= 0.5
    ]


def _extract_stops(
    serialized_segments: list[dict],
    pickup_coords: tuple[float, float],
    dropoff_coords: tuple[float, float],
) -> list[dict]:
    """Pull map-marker stops out of the serialized segment list."""
    stops = []
    for seg in serialized_segments:
        remark_lc = seg["remark"].lower()
        status = seg["status"]

        if remark_lc.startswith("pickup"):
            stop_type = "pickup"
            coords = list(pickup_coords)
        elif remark_lc.startswith("dropoff"):
            stop_type = "dropoff"
            coords = list(dropoff_coords)
        elif "fuel" in remark_lc:
            stop_type = "fuel"
            coords = seg["coords"]
        elif status in ("off_duty", "sleeper") and seg["duration_hours"] >= 1.0:
            stop_type = "rest"
            coords = seg["coords"]
        else:
            continue

        stops.append({
            "type":           stop_type,
            "location":       seg["location"],
            "coords":         coords,
            "arrival":        seg["start"],
            "departure":      seg["end"],
            "remark":         seg["remark"],
            "duration_hours": seg["duration_hours"],
        })

    return stops


def _build_summary(
    segments_out: list[dict],
    days_out: list[dict],
    total_distance: float,
    total_duration: float,
) -> dict:
    total_on_duty = sum(d["driving"] + d["on_duty"] for d in days_out)
    num_fuel_stops     = sum(1 for s in segments_out if "fuel" in s["remark"].lower() and s["status"] == "on_duty")
    num_rest_breaks    = sum(1 for s in segments_out if "30-minute break" in s["remark"].lower())
    num_overnight      = sum(1 for s in segments_out if "10-hour rest" in s["remark"].lower())
    restart_required   = any("34-hour restart" in s["remark"] for s in segments_out)
    return {
        "total_miles":          round(total_distance, 2),
        "total_driving_hours":  round(total_duration,  2),
        "total_on_duty_hours":  round(total_on_duty,   2),
        "num_fuel_stops":       num_fuel_stops,
        "num_rest_breaks":      num_rest_breaks,
        "num_overnight_rests":  num_overnight,
        "restart_required":     restart_required,
        "num_days":             len(days_out),
    }
