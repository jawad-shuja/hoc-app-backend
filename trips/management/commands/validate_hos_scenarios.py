"""
Management command: python manage.py validate_hos_scenarios

Runs deterministic FMCSA HOS validation scenarios A–F, J, K, L using the
simulate_trip() engine with hardcoded route distances/durations (no live API
calls). Reports PASS / FAIL for each scenario and exits non-zero if any fail.

Approximate distances used
--------------------------
  NY → Chicago:        780 mi / 12.5 h
  Chicago → Denver:  1 010 mi / 17.0 h
  Chicago → Chicago:     0 mi /  0.0 h  (same-city pickup)
  Dallas → Fort Worth:  35 mi /  0.5 h
  Fort Worth → Austin: 200 mi /  3.0 h
  San Diego → LA:      120 mi /  2.0 h
  LA → San Francisco:  380 mi /  6.0 h
  Seattle → Boston:  3 050 mi / 50.0 h  (long-haul)
  Boston → Miami:    1 500 mi / 25.0 h
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

from django.core.management.base import BaseCommand

from trips.hos_engine import Segment, compute_daily_totals, find_split_sleeper_pairs, simulate_trip, validate_hos_segments

_START = datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Shared assertion helpers
# ---------------------------------------------------------------------------

def _assert(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def _cycle_trace(segments: list[Segment], initial: float) -> float:
    """Return final cycle value, honouring 34h restarts."""
    cycle = float(initial)
    for seg in segments:
        if "restart" in seg.remark.lower():
            cycle = 0.0
        elif seg.status in ("driving", "on_duty"):
            cycle += seg.duration_hours
    return cycle


def _max_cycle(segments: list[Segment], initial: float) -> float:
    """Return the peak cycle value seen at any point during the trip."""
    cycle = float(initial)
    peak  = cycle
    for seg in segments:
        if "restart" in seg.remark.lower():
            cycle = 0.0
        elif seg.status in ("driving", "on_duty"):
            cycle += seg.duration_hours
        if cycle > peak:
            peak = cycle
    return peak


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def scenario_a() -> tuple[str, list[str]]:
    """A. Cycle almost exhausted (68 h) — NY → Chicago → Denver."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=12.5, leg1_distance_miles=780.0,
        leg2_driving_hours=17.0, leg2_distance_miles=1_010.0,
        cycle_used_hours=68.0,
        start_dt=_START,
        current_location="New York, NY",
        pickup_location="Chicago, IL",
        dropoff_location="Denver, CO",
    )

    _assert(
        any("restart" in s.remark.lower() for s in segs),
        "Expected a 34h restart with 68h cycle used",
        failures,
    )
    _assert(
        _max_cycle(segs, 68.0) <= 70.0 + 1e-6,
        f"Cycle exceeded 70h — peak={_max_cycle(segs, 68.0):.2f}h",
        failures,
    )
    pickup_idx = next((i for i, s in enumerate(segs) if s.remark == "pickup"), None)
    _assert(pickup_idx is not None, "No pickup segment found", failures)
    if pickup_idx is not None:
        leg1_drive_h = sum(
            s.duration_hours for s in segs[:pickup_idx] if s.status == "driving"
        )
        _assert(
            leg1_drive_h > 0,
            "No leg1 driving before pickup (phase ordering failure)",
            failures,
        )
    _assert(
        any(s.remark == "dropoff" for s in segs),
        "No dropoff segment found",
        failures,
    )
    totals = compute_daily_totals(segs)
    for day in totals:
        day_total = sum(day[k] for k in ("off_duty", "sleeper", "driving", "on_duty"))
        _assert(
            abs(day_total - 24.0) < 0.01 or day_total < 24.0,
            f"Day {day['date']} total {day_total:.2f}h != 24.0h",
            failures,
        )

    return "A — Cycle almost exhausted (NY→Chicago→Denver, 68h)", failures


def scenario_b() -> tuple[str, list[str]]:
    """B. Pickup consumes remaining cycle — Chicago == current, dropoff Denver, cycle 69h."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=17.0, leg2_distance_miles=1_010.0,
        cycle_used_hours=69.0,
        start_dt=_START,
        current_location="Chicago, IL",
        pickup_location="Chicago, IL",
        dropoff_location="Denver, CO",
    )

    _assert(
        any("restart" in s.remark.lower() for s in segs),
        "Expected a 34h restart after pickup exhausts cycle at 70h",
        failures,
    )
    _assert(
        _max_cycle(segs, 69.0) <= 70.0 + 1e-6,
        f"Cycle exceeded 70h — peak={_max_cycle(segs, 69.0):.2f}h",
        failures,
    )
    pickup = next((s for s in segs if s.remark == "pickup"), None)
    _assert(pickup is not None, "No pickup segment found", failures)
    if pickup is not None:
        _assert(
            abs(pickup.duration_hours - 1.0) < 1e-6,
            f"Pickup should be 1h on_duty, got {pickup.duration_hours:.4f}h",
            failures,
        )

    return "B — Pickup exhausts cycle (Chicago→Denver, 69h)", failures


def scenario_c() -> tuple[str, list[str]]:
    """C. Normal cycle (10h) — NY → Chicago → Denver."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=12.5, leg1_distance_miles=780.0,
        leg2_driving_hours=17.0, leg2_distance_miles=1_010.0,
        cycle_used_hours=10.0,
        start_dt=_START,
        current_location="New York, NY",
        pickup_location="Chicago, IL",
        dropoff_location="Denver, CO",
    )

    _assert(
        not any("restart" in s.remark.lower() for s in segs),
        "Unexpected 34h restart with only 10h cycle used",
        failures,
    )
    pickup_idx = next((i for i, s in enumerate(segs) if s.remark == "pickup"), None)
    _assert(pickup_idx is not None, "No pickup segment", failures)
    if pickup_idx is not None:
        leg1_drive_h = sum(
            s.duration_hours for s in segs[:pickup_idx] if s.status == "driving"
        )
        _assert(leg1_drive_h > 0, "No leg1 driving before pickup", failures)
    days = compute_daily_totals(segs)
    for day in days:
        _assert(
            day["driving"] <= 11.0 + 1e-6,
            f"Day {day['date']} driving {day['driving']:.2f}h exceeds 11h",
            failures,
        )
    miles_days = [d for d in days if d["driving"] > 0]
    _assert(len(miles_days) > 0, "No days with driving recorded", failures)

    return "C — Normal cycle (NY→Chicago→Denver, 10h)", failures


def scenario_d() -> tuple[str, list[str]]:
    """D. Short trip — Dallas → Fort Worth → Austin, cycle 20h."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=0.5, leg1_distance_miles=35.0,
        leg2_driving_hours=3.0, leg2_distance_miles=200.0,
        cycle_used_hours=20.0,
        start_dt=_START,
        current_location="Dallas, TX",
        pickup_location="Fort Worth, TX",
        dropoff_location="Austin, TX",
    )

    _assert(
        not any("restart" in s.remark.lower() for s in segs),
        "Unexpected restart for short trip with 20h cycle",
        failures,
    )
    fuel = [s for s in segs if "fuel" in s.remark.lower()]
    _assert(
        len(fuel) == 0,
        f"Unexpected fuel stop for 235-mile trip: {len(fuel)} found",
        failures,
    )
    days = compute_daily_totals(segs)
    _assert(
        len(days) <= 2,
        f"Short trip should be 1-2 days, got {len(days)}",
        failures,
    )

    return "D — Short trip (Dallas→FW→Austin, 20h)", failures


def scenario_e() -> tuple[str, list[str]]:
    """E. Fractional cycle — San Diego → LA → San Francisco, cycle 69.5h."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=2.0, leg1_distance_miles=120.0,
        leg2_driving_hours=6.0, leg2_distance_miles=380.0,
        cycle_used_hours=69.5,
        start_dt=_START,
        current_location="San Diego, CA",
        pickup_location="Los Angeles, CA",
        dropoff_location="San Francisco, CA",
    )

    _assert(
        any("restart" in s.remark.lower() for s in segs),
        "Expected 34h restart: only 0.5h remains before 70h cap",
        failures,
    )
    _assert(
        _max_cycle(segs, 69.5) <= 70.0 + 1e-6,
        f"Cycle exceeded 70h — peak={_max_cycle(segs, 69.5):.2f}h",
        failures,
    )

    return "E — Fractional cycle (San Diego→LA→SF, 69.5h)", failures


def scenario_f() -> tuple[str, list[str]]:
    """F. Long trip — Seattle → Boston → Miami, cycle 0h."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=50.0, leg1_distance_miles=3_050.0,
        leg2_driving_hours=25.0, leg2_distance_miles=1_500.0,
        cycle_used_hours=0.0,
        start_dt=_START,
        current_location="Seattle, WA",
        pickup_location="Boston, MA",
        dropoff_location="Miami, FL",
    )

    totals = compute_daily_totals(segs)
    _assert(len(totals) > 3, f"Long trip should span >3 days, got {len(totals)}", failures)

    sleeper_rests = [
        s for s in segs
        if s.status in ("sleeper", "off_duty")
        and s.duration_hours >= 10.0 - 1e-6
        and "restart" not in s.remark.lower()
    ]
    _assert(len(sleeper_rests) >= 2, "Expected multiple 10h rests for long trip", failures)

    breaks_30 = [s for s in segs if "30-min break" in s.remark.lower() or "break" in s.remark.lower() and s.duration_hours <= 1.0]
    _assert(len(breaks_30) >= 1, "Expected at least one 30-minute break", failures)

    fuel = [s for s in segs if "fuel" in s.remark.lower()]
    _assert(len(fuel) >= 4, f"Expected ≥4 fuel stops for 4,550-mile trip, got {len(fuel)}", failures)

    _assert(
        _max_cycle(segs, 0.0) <= 70.0 + 1e-6,
        f"Cycle exceeded 70h — peak={_max_cycle(segs, 0.0):.2f}h",
        failures,
    )

    return "F — Long trip (Seattle→Boston→Miami, 0h)", failures


def scenario_j() -> tuple[str, list[str]]:
    """J. 30-minute break appears after 8 cumulative driving hours."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=11.0, leg2_distance_miles=660.0,
        cycle_used_hours=0.0,
        start_dt=_START,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
    )

    breaks = [s for s in segs if "break" in s.remark.lower() and s.duration_hours >= 0.5 - 1e-6]
    _assert(len(breaks) >= 1, "Expected ≥1 30-minute break for 11h driving", failures)

    for b in breaks:
        _assert(
            b.duration_hours >= 0.5 - 1e-6,
            f"Break is only {b.duration_hours * 60:.0f} min (need ≥ 30)",
            failures,
        )

    cumul = 0.0
    for s in segs:
        if s.status == "driving":
            cumul += s.duration_hours
            _assert(
                cumul <= 8.0 + 1e-6,
                f"Drove {cumul:.2f}h without qualifying break",
                failures,
            )
        elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 0.5 - 1e-6:
            cumul = 0.0

    return "J — 30-minute break after 8 cumulative driving hours", failures


def scenario_k() -> tuple[str, list[str]]:
    """K. 34h restart crossing midnight — cycle resets only after full 34h."""
    failures: list[str] = []
    # Near-full cycle: trigger a restart that starts at ~22:00 and crosses 2 midnights
    start = datetime(2026, 6, 19, 22, 0, tzinfo=timezone.utc)
    segs = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=6.0, leg2_distance_miles=360.0,
        cycle_used_hours=70.0,
        start_dt=start,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
    )

    restarts = [s for s in segs if "restart" in s.remark.lower()]
    _assert(len(restarts) >= 1, "Expected a 34h restart", failures)
    for r in restarts:
        _assert(
            r.duration_hours >= 34.0 - 1e-6,
            f"Restart is only {r.duration_hours:.2f}h (need ≥ 34h)",
            failures,
        )
    # After restart, cycle must be 0 → final cycle ≤ 70
    _assert(
        _max_cycle(segs, 70.0) <= 70.0 + 1e-6,
        f"Cycle exceeded 70h after restart — peak={_max_cycle(segs, 70.0):.2f}h",
        failures,
    )
    totals = compute_daily_totals(segs)
    _assert(len(totals) >= 2, "Restart crossing midnight should span ≥ 2 days", failures)

    return "K — 34h restart crossing midnight", failures


def scenario_m() -> tuple[str, list[str]]:
    """M. No sleeper berth — rests must use off_duty status, not sleeper."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=15.0, leg2_distance_miles=900.0,
        cycle_used_hours=0.0,
        start_dt=_START,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
        has_sleeper_berth=False,
        sleeper_strategy="conservative_10h",
    )

    long_rests = [
        s for s in segs
        if s.duration_hours >= 10.0 - 1e-6
        and "restart" not in s.remark.lower()
    ]
    _assert(len(long_rests) > 0, "Expected at least one 10h rest for 15h trip", failures)
    for rest in long_rests:
        _assert(
            rest.status == "off_duty",
            f"Without sleeper berth, 10h rest must be off_duty; got '{rest.status}'",
            failures,
        )

    sleeper_segs = [s for s in segs if s.status == "sleeper"]
    _assert(
        len(sleeper_segs) == 0,
        f"No sleeper segments expected when has_sleeper_berth=False; found {len(sleeper_segs)}",
        failures,
    )

    _assert(
        any(s.remark == "dropoff" for s in segs),
        "No dropoff segment found",
        failures,
    )

    return "M — No sleeper berth (15h trip, off_duty rests)", failures


def scenario_n() -> tuple[str, list[str]]:
    """N. Allow split sleeper — planner generates 3h off + 7h sleeper valid pairs."""
    failures: list[str] = []
    segs = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=15.0, leg2_distance_miles=900.0,
        cycle_used_hours=0.0,
        start_dt=_START,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
        has_sleeper_berth=True,
        sleeper_strategy="allow_split_sleeper",
    )

    pairs = find_split_sleeper_pairs(segs)
    valid_pairs = [(i, j) for i, j, v in pairs if v]
    _assert(
        len(valid_pairs) > 0,
        f"Expected ≥1 valid split sleeper pair; found pairs={pairs}",
        failures,
    )

    first_periods  = [s for s in segs if s.remark == "split sleeper first"]
    second_periods = [s for s in segs if s.remark == "split sleeper second"]
    _assert(len(first_periods) > 0, "Expected split sleeper first periods", failures)
    _assert(len(second_periods) > 0, "Expected split sleeper second periods", failures)
    _assert(
        len(first_periods) == len(second_periods),
        f"First/second period counts don't match ({len(first_periods)} vs {len(second_periods)})",
        failures,
    )

    result = validate_hos_segments(segs, initial_cycle=0.0)
    _assert(
        len(result["violations"]) == 0,
        f"Split sleeper trip has HOS violations: {result['violations']}",
        failures,
    )

    _assert(
        any(s.remark == "dropoff" for s in segs),
        "No dropoff segment found",
        failures,
    )

    return "N — Split sleeper planner (3h off + 7h sleeper, valid pairs)", failures


def scenario_l() -> tuple[str, list[str]]:
    """L. Rolling 8-day approximation — current_cycle_used is the total at trip start."""
    failures: list[str] = []
    # Verify that cycle_used is treated as the full rolling total, not just today's hours.
    # With 68h used, the first 2h of driving/on-duty exhaust the budget → restart.
    segs_high = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=5.0, leg2_distance_miles=300.0,
        cycle_used_hours=68.0,
        start_dt=_START,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
    )
    _assert(
        any("restart" in s.remark.lower() for s in segs_high),
        "68h prior cycle must trigger a restart (rolling total, not just today)",
        failures,
    )

    # With 0h used, no restart for the same route.
    segs_zero = simulate_trip(
        leg1_driving_hours=0.0, leg1_distance_miles=0.0,
        leg2_driving_hours=5.0, leg2_distance_miles=300.0,
        cycle_used_hours=0.0,
        start_dt=_START,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
    )
    _assert(
        not any("restart" in s.remark.lower() for s in segs_zero),
        "0h prior cycle should not trigger a restart for a short trip",
        failures,
    )

    return "L — Rolling 8-day approximation via current_cycle_used", failures


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

SCENARIOS: list[Callable[[], tuple[str, list[str]]]] = [
    scenario_a, scenario_b, scenario_c, scenario_d,
    scenario_e, scenario_f, scenario_j, scenario_k, scenario_l,
    scenario_m, scenario_n,
]


class Command(BaseCommand):
    help = (
        "Validate FMCSA HOS planning scenarios (A–F, J, K, L) without live API calls. "
        "Exits with code 1 if any scenario fails."
    )

    def handle(self, *args, **options):
        self.stdout.write(self.style.HTTP_INFO(
            "\n=== FMCSA HOS Scenario Validation ==="
        ))
        total = len(SCENARIOS)
        passed = 0
        failed = 0

        for fn in SCENARIOS:
            label, failures = fn()
            if failures:
                self.stdout.write(self.style.ERROR(f"  FAIL  {label}"))
                for f in failures:
                    self.stdout.write(f"         ✗ {f}")
                failed += 1
            else:
                self.stdout.write(self.style.SUCCESS(f"  PASS  {label}"))
                passed += 1

        self.stdout.write("")
        self.stdout.write(
            f"Results: {passed}/{total} passed, {failed}/{total} failed"
        )

        if failed:
            self.stderr.write(self.style.ERROR("\nValidation FAILED."))
            sys.exit(1)
        else:
            self.stdout.write(self.style.SUCCESS("\nAll scenarios passed."))
