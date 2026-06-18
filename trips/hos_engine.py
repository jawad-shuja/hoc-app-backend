from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Segment:
    """One duty-status interval in a driver's log."""
    status: str   # "off_duty" | "sleeper" | "driving" | "on_duty"
    start: datetime
    end: datetime
    location: str
    remark: str
    distance_miles: float = 0.0  # populated only for "driving" segments

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def simulate_trip(
    driving_hours: float,
    distance_miles: float,
    cycle_used_hours: float,
    start_dt: datetime,
    pickup_duration_hours: float = 1.0,
    dropoff_duration_hours: float = 1.0,
) -> list[Segment]:
    """Simulate an HOS-compliant trip and return a flat list of duty-status segments.

    Rules enforced:
      - 11h driving cap per shift
      - 14h on-duty window per shift
      - 30-min break required after 8 cumulative driving hours
      - 10h consecutive rest between shifts
      - Fuel stop every 1,000 miles
      - 1h on-duty blocks for pickup and dropoff
      - 70h/8-day cycle cap with 34h restart
    """
    segments: list[Segment] = []
    now = start_dt
    cycle_used = cycle_used_hours
    driving_left = driving_hours
    speed = distance_miles / driving_hours if driving_hours > 0 else 60.0

    shift_start = now
    shift_driving = 0.0
    cumulative_since_break = 0.0
    miles_since_fuel = 0.0

    def push(status: str, hours: float, location: str, remark: str) -> None:
        nonlocal now
        end = now + timedelta(hours=hours)
        segments.append(Segment(status, now, end, location, remark))
        now = end

    def do_shift_rest(hours: float, remark: str) -> None:
        nonlocal shift_start, shift_driving, cumulative_since_break
        push("off_duty", hours, "rest area", remark)
        shift_start = now
        shift_driving = 0.0
        cumulative_since_break = 0.0

    # Immediate 34h restart if cycle is already exhausted
    if cycle_used >= 70.0 - 1e-9:
        push("off_duty", 34.0, "rest area", "34h restart")
        cycle_used = 0.0
        shift_start = now

    # Pickup (on-duty, not driving)
    push("on_duty", pickup_duration_hours, "pickup location", "pickup")
    cycle_used += pickup_duration_hours

    while driving_left > 1e-9:
        # Cycle cap hit mid-trip → 34h restart
        if cycle_used >= 70.0 - 1e-9:
            do_shift_rest(34.0, "34h restart")
            cycle_used = 0.0
            continue

        window_used = (now - shift_start).total_seconds() / 3600
        cap_shift = 11.0 - shift_driving
        cap_break = 8.0 - cumulative_since_break
        cap_window = 14.0 - window_used

        # Fuel: only impose a cap when the remaining trip distance crosses 1,000 miles
        if miles_since_fuel + driving_left * speed > 1000.0 + 1e-9:
            hours_to_fuel = (1000.0 - miles_since_fuel) / speed
        else:
            hours_to_fuel = float("inf")

        can_drive = min(cap_shift, cap_break, cap_window, hours_to_fuel, driving_left)

        if can_drive <= 1e-9:
            # Decide whether to take a full shift rest or a short break
            if shift_driving >= 11.0 - 1e-9 or cap_window <= 1e-9:
                do_shift_rest(10.0, "overnight rest")
            else:
                # 30-min break to satisfy the 8h break rule
                push("off_duty", 0.5, "rest area", "30-min break")
                cumulative_since_break = 0.0
            continue

        # Drive the next segment
        push("driving", can_drive, "en route", "driving")
        segments[-1].distance_miles = can_drive * speed
        driving_left -= can_drive
        shift_driving += can_drive
        cumulative_since_break += can_drive
        miles_since_fuel += can_drive * speed
        cycle_used += can_drive

        # Fuel stop if threshold reached and more driving remains
        if miles_since_fuel >= 1000.0 - 1e-9 and driving_left > 1e-9:
            push("on_duty", 0.5, "fuel stop", "fuel stop")
            cycle_used += 0.5
            miles_since_fuel = 0.0

        # Post-drive: shift rest or short break
        if (shift_driving >= 11.0 - 1e-9 or
                (now - shift_start).total_seconds() / 3600 >= 14.0 - 1e-9):
            if driving_left > 1e-9:
                do_shift_rest(10.0, "overnight rest")
        elif cumulative_since_break >= 8.0 - 1e-9 and driving_left > 1e-9:
            push("off_duty", 0.5, "rest area", "30-min break")
            cumulative_since_break = 0.0

    # Dropoff (on-duty, not driving)
    push("on_duty", dropoff_duration_hours, "dropoff location", "dropoff")

    return segments


def compute_daily_totals(segments: list[Segment]) -> list[dict]:
    """Aggregate duty-status hours per calendar day.

    Segments spanning midnight are split at the day boundary so each day's
    totals are independent.

    Returns a list of dicts ordered by date, each with keys:
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
            # Next midnight in the same timezone
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
            "date": day,
            "off_duty": t["off_duty"],
            "sleeper": t["sleeper"],
            "driving": t["driving"],
            "on_duty": t["on_duty"],
        })
    return result
