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


def _next_round_hour_utc(now: datetime) -> datetime:
    """Return the current UTC hour if already on the hour, else advance to the next."""
    if now.minute == 0 and now.second == 0 and now.microsecond == 0:
        return now.replace(second=0, microsecond=0)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


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

        # 3. HOS simulation — correct phase ordering: leg1 → pickup → leg2 → dropoff
        if d.get("start_datetime"):
            # Parse the raw ISO string preserving its timezone offset.
            # DRF's DateTimeField would strip the offset (converting to UTC), so we
            # accept a CharField and parse here instead.
            raw = str(d["start_datetime"]).strip().replace("Z", "+00:00")
            try:
                start_dt = datetime.fromisoformat(raw)
            except (ValueError, AttributeError):
                return Response(
                    {"start_datetime": ["Enter a valid ISO 8601 date/time string."]},
                    status=http_status.HTTP_400_BAD_REQUEST,
                )
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            trip_start_assumed = False
        else:
            start_dt = _next_round_hour_utc(datetime.now(timezone.utc))
            trip_start_assumed = True

        # Derive the UTC-offset string (e.g. "+05:00") from the trip start so
        # that day-boundary splits and log times are expressed in the user's
        # local timezone rather than UTC.
        utcoff = start_dt.utcoffset()
        if utcoff is None:
            tz_offset_str = "+00:00"
        else:
            total_sec = int(utcoff.total_seconds())
            sign = "+" if total_sec >= 0 else "-"
            abs_sec = abs(total_sec)
            tz_offset_str = f"{sign}{abs_sec // 3600:02d}:{(abs_sec % 3600) // 60:02d}"

        segments = simulate_trip(
            leg1_driving_hours=leg1["duration_hours"],
            leg1_distance_miles=leg1["distance_miles"],
            leg2_driving_hours=leg2["duration_hours"],
            leg2_distance_miles=leg2["distance_miles"],
            cycle_used_hours=d["current_cycle_used"],
            start_dt=start_dt,
            current_location=d["current_location"],
            pickup_location=d["pickup_location"],
            dropoff_location=d["dropoff_location"],
            has_sleeper_berth=bool(d.get("has_sleeper_berth", True)),
            sleeper_strategy=str(d.get("sleeper_strategy", "conservative_10h")),
        )

        # 4. Resolve human-readable city names for each segment.
        # Pass separate leg geometries so post-pickup positions interpolate within
        # leg2 (toward dropoff), not leg1 (toward pickup).
        leg1_geom = leg1["geometry"]
        leg2_geom = leg2["geometry"]
        leg1_h    = leg1["duration_hours"]
        leg2_h    = leg2["duration_hours"]

        resolved = _resolve_segment_locations(
            segments, leg1_geom, leg2_geom, leg1_h, leg2_h,
            d["pickup_location"], d["dropoff_location"],
        )

        segments_out = _serialize_segments(
            segments, leg1_geom, leg2_geom, leg1_h, leg2_h,
            resolved, pickup_coords, dropoff_coords,
        )
        daily_totals = compute_daily_totals(segments)
        day_locs = _compute_day_locs(
            daily_totals, segments, resolved,
            leg1_geom, leg2_geom, leg1_h, leg2_h,
            d["current_location"], d["dropoff_location"],
            tz_offset_str=tz_offset_str,
        )
        days_out = _serialize_days(
            segments_out, daily_totals, total_distance, total_duration,
            day_locs, d["current_cycle_used"],
            tz_offset_str=tz_offset_str,
        )
        stops_out = _extract_stops(segments_out, pickup_coords, dropoff_coords)
        summary = _build_summary(segments_out, days_out, total_distance, total_duration)

        return Response({
            "trip_start":            start_dt.isoformat(),
            "trip_start_assumed":    trip_start_assumed,
            "trip_timezone_offset":  tz_offset_str,
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

def _coords_for_segment(
    seg: "Segment",
    leg1_geometry: list,
    leg2_geometry: list,
    leg1_h: float,
    leg2_h: float,
) -> list[float]:
    """Return [lon, lat] for a segment by interpolating within the correct leg geometry.

    Leg1 segments use leg1_geometry; leg2 segments use leg2_geometry with their
    driving-hours offset from leg1 subtracted before computing the fraction.
    This prevents leg2 rest/fuel stops from being misplaced inside leg1 territory.
    """
    if seg.leg == 1 or leg2_h < 1e-9:
        frac = seg.cumulative_drive_h / leg1_h if leg1_h > 1e-9 else 0.0
        return _point_at_fraction(leg1_geometry, frac)
    else:
        leg2_done = max(0.0, seg.cumulative_drive_h - leg1_h)
        frac = leg2_done / leg2_h if leg2_h > 1e-9 else 0.0
        return _point_at_fraction(leg2_geometry, frac)


def _resolve_segment_locations(
    segments: list["Segment"],
    leg1_geometry: list,
    leg2_geometry: list,
    leg1_h: float,
    leg2_h: float,
    pickup_location: str,
    dropoff_location: str,
) -> list[str]:
    """Return a resolved city-name string for each segment.

    Pickup and dropoff use the user-supplied text.  Non-driving stops are
    reverse-geocoded at their per-leg position.  Driving segments get an
    empty string — the log sheet needs no city for a moving interval.
    """
    result: list[str] = []

    for seg in segments:
        if seg.remark == "pickup":
            result.append(pickup_location)
        elif seg.remark == "dropoff":
            result.append(dropoff_location)
        elif seg.status == "driving":
            result.append("")
        else:
            coords = _coords_for_segment(seg, leg1_geometry, leg2_geometry, leg1_h, leg2_h)
            result.append(reverse_geocode(coords[0], coords[1]))

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
    if remark == "sleeper overnight rest":
        return f"Sleeper berth / 10-hour rest – {loc}" if loc else "Sleeper berth / 10-hour rest"
    if remark == "split sleeper first":
        return f"Split sleeper: paired off-duty period – near {loc}" if loc else "Split sleeper: paired off-duty period"
    if remark == "split sleeper second":
        return f"Sleeper berth: qualifying period (split pair complete) – {loc}" if loc else "Sleeper berth: qualifying period (split pair complete)"
    if remark == "fuel stop":
        return f"Planned fuel window – near {loc} (not a verified station)" if loc else "Planned fuel window (not a verified station)"
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


def _locate_at_dt(
    target_dt: datetime,
    segments: list["Segment"],
    resolved_locations: list[str],
    leg1_geometry: list,
    leg2_geometry: list,
    leg1_h: float,
    leg2_h: float,
) -> str:
    """Return a city/state string for the driver's position at target_dt.

    Walks the segment list to find which segment contains target_dt, then:
    - Non-driving: returns the pre-resolved city name (no extra geocoding call).
    - Driving: interpolates within the correct leg geometry and reverse-geocodes.
    - No segment found: returns "".
    """
    for i, seg in enumerate(segments):
        if seg.start <= target_dt < seg.end:
            if seg.status != "driving":
                return resolved_locations[i]
            elapsed_h = (target_dt - seg.start).total_seconds() / 3600
            drive_done = seg.cumulative_drive_h + elapsed_h
            if seg.leg == 1 or leg2_h < 1e-9:
                frac = min(1.0, drive_done / leg1_h) if leg1_h > 1e-9 else 0.0
                coords = _point_at_fraction(leg1_geometry, frac)
            else:
                leg2_done = max(0.0, drive_done - leg1_h)
                frac = min(1.0, leg2_done / leg2_h) if leg2_h > 1e-9 else 0.0
                coords = _point_at_fraction(leg2_geometry, frac)
            return reverse_geocode(coords[0], coords[1])
    return ""


def _serialize_segments(
    segments: list["Segment"],
    leg1_geometry: list,
    leg2_geometry: list,
    leg1_h: float,
    leg2_h: float,
    resolved_locations: list[str],
    pickup_coords: tuple[float, float],
    dropoff_coords: tuple[float, float],
) -> list[dict]:
    """Convert Segment objects to JSON-serializable dicts with enriched location/remark.

    Pickup and dropoff get exact geocoded coordinates; all other segments are
    interpolated within the correct leg geometry so post-pickup stops correctly
    reflect progress toward the dropoff, not back toward the pickup.
    Location is empty string for driving segments — never "en route".
    """
    out = []
    for i, seg in enumerate(segments):
        loc = resolved_locations[i]

        if seg.remark == "pickup":
            coords = list(pickup_coords)
        elif seg.remark == "dropoff":
            coords = list(dropoff_coords)
        else:
            coords = _coords_for_segment(seg, leg1_geometry, leg2_geometry, leg1_h, leg2_h)

        out.append({
            "status":         seg.status,
            "start":          seg.start.isoformat(),
            "end":            seg.end.isoformat(),
            "location":       loc,          # empty string for driving; real city for stops
            "remark":         _format_remark(seg.remark, loc),
            "duration_hours": round(seg.duration_hours, 4),
            "distance_miles": round(seg.distance_miles, 2),
            "coords":         coords,
        })
    return out


def _cycle_at(
    segments_out: list[dict],
    initial_cycle: float,
    target_dt: datetime,
) -> float:
    """Compute cycle_used at target_dt by walking segments and resetting at 34 h restarts.

    A 34-hour restart resets the cycle to 0 the moment it COMPLETES (seg.end ≤ target_dt).
    If the restart is still in progress at target_dt, the pre-restart value is preserved.
    """
    cycle = initial_cycle
    for s in segments_out:
        try:
            seg_start = datetime.fromisoformat(s["start"])
        except ValueError:
            continue
        if seg_start >= target_dt:
            break

        remark = s.get("remark", "")
        if "34-hour restart" in remark.lower() or remark == "34h restart":
            try:
                seg_end = datetime.fromisoformat(s["end"])
            except ValueError:
                continue
            if seg_end <= target_dt:
                cycle = 0.0  # restart fully completed — cycle resets
            continue  # off-duty; never accumulates regardless

        if s["status"] in ("driving", "on_duty"):
            try:
                seg_end = datetime.fromisoformat(s["end"])
            except ValueError:
                continue
            clipped_hours = (min(seg_end, target_dt) - seg_start).total_seconds() / 3600
            if clipped_hours > 0:
                cycle += clipped_hours

    return max(0.0, cycle)


def _compute_day_locs(
    daily_totals: list[dict],
    segments: list["Segment"],
    resolved_locations: list[str],
    leg1_geometry: list,
    leg2_geometry: list,
    leg1_h: float,
    leg2_h: float,
    current_location: str,
    dropoff_location: str,
    tz_offset_str: str = "+00:00",
) -> list[tuple[str, str]]:
    """Return (from_loc, to_loc) pairs representing the driver's city at the start
    and end of each calendar day.

    from_loc: exact position at 00:00 local time (interpolated from driving geometry
              when the driver crosses midnight while driving, not the prior rest-stop).
    to_loc:   exact position at 23:59 local time (or the last stop location if the
              driver is already stopped before midnight).

    Day 1 from_loc is always current_location.
    Falls back to the last known stop in edge cases (trip ends early, etc.).
    """
    # Fallback: known stop locations with timestamps for days with no driving at boundaries
    known_locs: list[tuple[datetime, str]] = []
    for i, seg in enumerate(segments):
        loc = resolved_locations[i]
        if loc and seg.status != "driving" and loc not in _GENERIC_LOCS:
            known_locs.append((seg.start, loc))

    results: list[tuple[str, str]] = []
    for i, day_data in enumerate(daily_totals):
        day_date = str(day_data["date"])
        day_start = datetime.fromisoformat(day_date + "T00:00:00" + tz_offset_str)
        day_end   = day_start + timedelta(days=1)

        # ── from_loc ──────────────────────────────────────────────────────
        if i == 0:
            from_loc = current_location
        else:
            from_loc = _locate_at_dt(
                day_start, segments, resolved_locations,
                leg1_geometry, leg2_geometry, leg1_h, leg2_h,
            )
            if not from_loc or from_loc in _GENERIC_LOCS:
                before = [loc for ts, loc in known_locs if ts < day_start]
                from_loc = before[-1] if before else current_location

        # ── to_loc ────────────────────────────────────────────────────────
        end_check = day_end - timedelta(seconds=60)  # 23:59:00 — last full minute of the day
        to_loc = _locate_at_dt(
            end_check, segments, resolved_locations,
            leg1_geometry, leg2_geometry, leg1_h, leg2_h,
        )
        if not to_loc or to_loc in _GENERIC_LOCS:
            until = [loc for ts, loc in known_locs if ts < day_end]
            to_loc = until[-1] if until else dropoff_location

        results.append((from_loc, to_loc))
    return results


def _serialize_days(
    segments_out: list[dict],
    daily_totals: list[dict],
    total_distance: float,
    total_duration: float,
    day_locs: list[tuple[str, str]],
    initial_cycle_used: float,
    tz_offset_str: str = "+00:00",
) -> list[dict]:
    """Build enriched per-day records with from/to cities, mileage, and cycle data.

    from/to locations come from day_locs (pre-computed by _compute_day_locs) and
    reflect the driver's actual position at midnight — not just the last rest stop.
    Artifact days (< 30 min total) caused by trips starting just before UTC midnight
    are removed before returning.
    """
    # Use actual simulation driving hours as denominator so daily mileages
    # sum to total_distance (ORS may report slightly different duration than
    # what the engine drives after quarter-hour rounding).
    total_driving_h_sim = sum(d["driving"] for d in daily_totals)
    speed = total_distance / total_driving_h_sim if total_driving_h_sim > 1e-9 else 0.0

    result = []

    for i, day_data in enumerate(daily_totals):
        day_date = str(day_data["date"])
        day_start = datetime.fromisoformat(day_date + "T00:00:00" + tz_offset_str)
        day_end   = day_start + timedelta(days=1)

        from_loc, to_loc = day_locs[i] if i < len(day_locs) else ("", "")

        miles_today = round(day_data["driving"] * speed, 1)

        # cycle_used_start: compute at the moment the driver FIRST goes on-duty that day.
        # This reflects the log-sheet "Recap of Hours" — the cycle count the driver carries
        # INTO the shift, after any 34 h restart that may have completed earlier that day.
        first_active_dt = day_start
        for s in segments_out:
            try:
                seg_start_dt = datetime.fromisoformat(s["start"])
                seg_end_dt   = datetime.fromisoformat(s["end"])
            except ValueError:
                continue
            if s["status"] in ("driving", "on_duty") and seg_end_dt > day_start and seg_start_dt < day_end:
                first_active_dt = max(seg_start_dt, day_start)
                break
        cycle_start = _cycle_at(segments_out, initial_cycle_used, first_active_dt)

        # Detect whether a 34h restart completed during this day and before
        # the driver's first active duty — used to annotate the recap section.
        after_restart = False
        for s in segments_out:
            remark = s.get("remark", "")
            if "34-hour restart" in remark.lower():
                try:
                    seg_end = datetime.fromisoformat(s["end"])
                except ValueError:
                    continue
                if day_start <= seg_end <= first_active_dt:
                    after_restart = True
                    break

        result.append({
            "date":             day_date,
            "off_duty":         round(day_data["off_duty"], 4),
            "sleeper":          round(day_data["sleeper"],  4),
            "driving":          round(day_data["driving"],  4),
            "on_duty":          round(day_data["on_duty"],  4),
            "from_location":    from_loc,
            "to_location":      to_loc,
            "miles_today":      miles_today,
            "cycle_used_start": round(cycle_start, 2),
            "after_restart":    after_restart,
        })

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
    num_overnight      = sum(1 for s in segments_out if "sleeper berth" in s["remark"].lower() or "10-hour rest" in s["remark"].lower() or "split pair complete" in s["remark"].lower())
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
