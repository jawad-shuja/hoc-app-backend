from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Quarter-hour helpers
# ---------------------------------------------------------------------------

def _snap_quarter(dt: datetime) -> datetime:
    """Snap a datetime to the nearest 15-minute boundary."""
    total_min = dt.hour * 60 + dt.minute + dt.second / 60 + dt.microsecond / 60_000_000
    snapped = round(total_min / 15) * 15
    base = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=snapped)


def _floor_quarter(hours: float) -> float:
    """Floor hours to the nearest 15-minute increment.

    The +1e-9 epsilon prevents floating-point values like 7.9999999 from
    flooring down to 7.75 when they should yield 8.0.
    """
    return math.floor(hours * 4 + 1e-9) / 4


# ---------------------------------------------------------------------------
# Segment dataclass
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One duty-status interval in a driver's log."""
    status: str        # "off_duty" | "sleeper" | "driving" | "on_duty"
    start: datetime
    end: datetime
    location: str
    remark: str
    distance_miles: float = 0.0        # populated for driving segments
    leg: int = 0                        # 1 = current→pickup, 2 = pickup→dropoff
    cumulative_drive_h: float = 0.0    # total driving hours completed before this segment

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


# ---------------------------------------------------------------------------
# HOS simulation
# ---------------------------------------------------------------------------

def simulate_trip(
    leg1_driving_hours: float,
    leg1_distance_miles: float,
    leg2_driving_hours: float,
    leg2_distance_miles: float,
    cycle_used_hours: float,
    start_dt: datetime,
    current_location: str = "current location",
    pickup_location: str = "pickup location",
    dropoff_location: str = "dropoff location",
    pickup_duration_hours: float = 1.0,
    dropoff_duration_hours: float = 1.0,
) -> list[Segment]:
    """Simulate an FMCSA-compliant two-leg trip with correct phase ordering.

    Phase order:
      leg1   — drive current_location → pickup_location
      pickup — 1 h on-duty at pickup_location
      leg2   — drive pickup_location → dropoff_location
      dropoff— 1 h on-duty at dropoff_location

    HOS rules enforced (70 h / 8-day, property-carrying, no adverse conditions):
      - 11 h driving cap per duty period
      - 14 h on-duty window per duty period
      - 30-min break after 8 cumulative driving hours (off-duty resets counter)
      - 10 h consecutive off-duty resets duty period
      - Fuel stop (0.5 h on-duty) every 1,000 miles
      - 70 h / 8-day cycle cap: HARD LIMIT — no on-duty once cycle ≥ 70 until 34 h restart
      - 34 h restart resets cycle to 0

    All duty-status change times are snapped to 15-minute (quarter-hour) boundaries.
    """
    segments: list[Segment] = []

    # Snap start to nearest 15 minutes for clean log-sheet times
    now = _snap_quarter(start_dt)

    cycle_used = float(cycle_used_hours)
    speed1 = leg1_distance_miles / leg1_driving_hours if leg1_driving_hours > 1e-9 else 60.0
    speed2 = leg2_distance_miles / leg2_driving_hours if leg2_driving_hours > 1e-9 else 60.0

    # Duty-period counters — reset on 10 h or 34 h rest
    shift_start            = now
    shift_driving          = 0.0
    cumulative_since_break = 0.0

    # Trip progress
    driving_left_leg1  = float(leg1_driving_hours)
    driving_left_leg2  = float(leg2_driving_hours)
    miles_since_fuel   = 0.0
    total_drive_h_done = 0.0   # cumulative driving hours (for geometry fraction in views.py)

    phase = "leg1"   # "leg1" | "pickup" | "leg2" | "dropoff" | "done"

    # ── inner helpers ────────────────────────────────────────────────────────────

    def _cur_leg() -> int:
        return 1 if phase in ("leg1", "pickup") else 2

    def push(status: str, hours: float, location: str, remark: str,
             dist: float = 0.0) -> None:
        nonlocal now
        if hours <= 1e-12:
            return
        segments.append(Segment(
            status=status,
            start=now,
            end=now + timedelta(hours=hours),
            location=location,
            remark=remark,
            distance_miles=dist,
            leg=_cur_leg(),
            cumulative_drive_h=total_drive_h_done,
        ))
        now = now + timedelta(hours=hours)

    def do_10h_rest(loc: str) -> None:
        nonlocal shift_start, shift_driving, cumulative_since_break
        push("off_duty", 10.0, loc, "overnight rest")
        shift_start = now
        shift_driving = 0.0
        cumulative_since_break = 0.0

    def do_34h_restart(loc: str) -> None:
        nonlocal cycle_used, shift_start, shift_driving, cumulative_since_break
        push("off_duty", 34.0, loc, "34h restart")
        cycle_used = 0.0   # ← hard reset: cycle starts fresh after 34 h off
        shift_start = now
        shift_driving = 0.0
        cumulative_since_break = 0.0

    def rest_loc() -> str:
        """Placeholder resolved to a real city via reverse-geocoding in views.py."""
        return "rest area"

    # ── pre-trip: immediate restart if cycle already exhausted ───────────────────
    if cycle_used >= 70.0 - 1e-9:
        do_34h_restart(current_location)

    # ── main loop ────────────────────────────────────────────────────────────────
    _guard = 0
    while phase != "done":
        _guard += 1
        if _guard > 50_000:
            break  # safety valve — should never be needed

        # ── pickup ───────────────────────────────────────────────────────────
        if phase == "pickup":
            win = (now - shift_start).total_seconds() / 3600
            if win + pickup_duration_hours > 14.0 - 1e-9:
                do_10h_rest(pickup_location)
            if cycle_used + pickup_duration_hours > 70.0 - 1e-9:
                do_34h_restart(pickup_location)
            push("on_duty", pickup_duration_hours, pickup_location, "pickup")
            cycle_used += pickup_duration_hours
            phase = "leg2"
            continue

        # ── dropoff ──────────────────────────────────────────────────────────
        if phase == "dropoff":
            win = (now - shift_start).total_seconds() / 3600
            if win + dropoff_duration_hours > 14.0 - 1e-9:
                do_10h_rest(dropoff_location)
            if cycle_used + dropoff_duration_hours > 70.0 - 1e-9:
                do_34h_restart(dropoff_location)
            push("on_duty", dropoff_duration_hours, dropoff_location, "dropoff")
            cycle_used += dropoff_duration_hours
            phase = "done"
            continue

        # ── driving (leg1 or leg2) ────────────────────────────────────────────
        driving_left  = driving_left_leg1 if phase == "leg1" else driving_left_leg2
        current_speed = speed1 if phase == "leg1" else speed2

        # Leg finished → advance phase
        if driving_left <= 1e-9:
            phase = "pickup" if phase == "leg1" else "dropoff"
            continue

        # Hard cycle cap: no driving once cycle is at 70 h
        if cycle_used >= 70.0 - 1e-9:
            do_34h_restart(rest_loc())
            continue

        # Compute available driving time for this segment
        win        = (now - shift_start).total_seconds() / 3600
        cap_shift  = 11.0 - shift_driving
        cap_break  = 8.0  - cumulative_since_break
        cap_window = 14.0 - win
        cap_cycle  = 70.0 - cycle_used   # ← HARD cap: included in min()

        # Fuel: hours until the trip would reach the 1,000-mile mark
        if miles_since_fuel + driving_left * current_speed > 1000.0 + 1e-9:
            hours_to_fuel = (1000.0 - miles_since_fuel) / current_speed
        else:
            hours_to_fuel = float("inf")

        # All five HOS/operational caps applied together
        can_drive_raw = min(
            cap_shift, cap_break, cap_window,
            hours_to_fuel, driving_left, cap_cycle,
        )

        if can_drive_raw <= 1e-9:
            # Resolve the blocking constraint
            if cap_cycle <= 1e-9:
                do_34h_restart(rest_loc())
            elif cap_shift <= 1e-9 or cap_window <= 1e-9:
                do_10h_rest(rest_loc())
            elif hours_to_fuel <= 1e-9:
                if cycle_used + 0.5 <= 70.0 - 1e-9:
                    push("on_duty", 0.5, rest_loc(), "fuel stop")
                    cycle_used += 0.5
                miles_since_fuel = 0.0
            else:
                # cap_break == 0: mandatory 30-min break
                push("off_duty", 0.5, rest_loc(), "30-min break")
                cumulative_since_break = 0.0
            continue

        # Floor to quarter-hour for clean log times
        can_drive = _floor_quarter(can_drive_raw)
        if can_drive < 1e-9 and can_drive_raw > 0:
            # Less than 15 min before some constraint fires.
            # Identify the binding constraint and resolve it NOW without driving a
            # sub-quarter-hour fragment (which would produce off-grid log times).
            # Priority: cycle cap > shift/window > fuel > break > tiny tail.
            eps = can_drive_raw + 1e-6
            if cap_cycle <= eps:
                do_34h_restart(rest_loc())
                continue
            if cap_shift <= eps or cap_window <= eps:
                do_10h_rest(rest_loc())
                continue
            if hours_to_fuel != float("inf") and hours_to_fuel <= eps:
                if cycle_used + 0.5 <= 70.0 - 1e-9:
                    push("on_duty", 0.5, rest_loc(), "fuel stop")
                    cycle_used += 0.5
                miles_since_fuel = 0.0
                continue
            if cap_break <= eps:
                push("off_duty", 0.5, rest_loc(), "30-min break")
                cumulative_since_break = 0.0
                continue
            # driving_left is the only binding constraint — tiny tail.
            # Round UP to 0.25 h; safe because all HOS caps are ≥ 0.25 h here.
            # (Re-check cap_cycle to guard against rounding past 70 h.)
            if cap_cycle >= 0.25 - 1e-9:
                can_drive = 0.25
            else:
                can_drive = can_drive_raw  # fallback: accept tiny non-grid segment

        dist = can_drive * current_speed
        push("driving", can_drive, "en route", "driving", dist=dist)
        total_drive_h_done     += can_drive
        shift_driving          += can_drive
        cumulative_since_break += can_drive
        miles_since_fuel       += dist
        cycle_used             += can_drive

        if phase == "leg1":
            driving_left_leg1 -= can_drive
        else:
            driving_left_leg2 -= can_drive

        # Fuel stop if 1,000-mile mark reached and more driving remains
        remaining_h = driving_left_leg1 + driving_left_leg2
        if miles_since_fuel >= 1000.0 - 1e-9 and remaining_h > 1e-9:
            if cycle_used + 0.5 <= 70.0 - 1e-9:
                push("on_duty", 0.5, rest_loc(), "fuel stop")
                cycle_used += 0.5
            miles_since_fuel = 0.0

        # Post-drive: mandatory rest if shift or window exhausted (only if driving remains)
        remaining_drive = driving_left_leg1 + driving_left_leg2
        if remaining_drive > 1e-9:
            win_now = (now - shift_start).total_seconds() / 3600
            if shift_driving >= 11.0 - 1e-9 or win_now >= 14.0 - 1e-9:
                do_10h_rest(rest_loc())
            elif cumulative_since_break >= 8.0 - 1e-9:
                push("off_duty", 0.5, rest_loc(), "30-min break")
                cumulative_since_break = 0.0

    return segments


# ---------------------------------------------------------------------------
# Daily-totals aggregation
# ---------------------------------------------------------------------------

def compute_daily_totals(segments: list[Segment]) -> list[dict]:
    """Aggregate duty-status hours per UTC calendar day.

    Segments spanning midnight are split at the boundary so each day's
    totals are independent.

    Returns a list of dicts ordered by date, each with:
      "date" (datetime.date), "off_duty", "sleeper", "driving", "on_duty" (hours).
    """
    if not segments:
        return []

    day_totals: dict = defaultdict(
        lambda: {"off_duty": 0.0, "sleeper": 0.0, "driving": 0.0, "on_duty": 0.0}
    )

    for seg in segments:
        current = seg.start
        while current < seg.end:
            next_mid = (current + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            chunk_end = min(seg.end, next_mid)
            day_totals[current.date()][seg.status] += (
                (chunk_end - current).total_seconds() / 3600
            )
            current = chunk_end

    result = []
    for day in sorted(day_totals):
        t = day_totals[day]
        result.append({
            "date":     day,
            "off_duty": t["off_duty"],
            "sleeper":  t["sleeper"],
            "driving":  t["driving"],
            "on_duty":  t["on_duty"],
        })
    return result
