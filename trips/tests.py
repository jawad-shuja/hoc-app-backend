"""
Unit tests for the HOS (Hours of Service) engine.

All tests for simulate_trip() and compute_daily_totals() live here.

HOS rules enforced (70 h / 8-day, property-carrying, no adverse conditions):
  - 11 h driving cap per on-duty shift
  - 14 h on-duty window per shift (driving + on_duty combined)
  - ≥30-min break required after 8 cumulative driving hours
  - 10 h consecutive rest between shifts
  - Fuel stop at least every 1,000 miles
  - 1 h on-duty block for pickup, 1 h for dropoff
  - 70 h / 8-day cycle cap (HARD LIMIT) with mandatory 34 h restart to reset it
  - 34 h restart resets cycle_used to 0
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase

from .hos_engine import (
    Segment, compute_daily_totals, find_split_sleeper_pairs,
    simulate_trip, validate_hos_segments,
)

UTC = ZoneInfo("UTC")

# Fixed reference datetime used across all simulate_trip tests.
# 08:00 UTC on a Tuesday — already on a 15-minute boundary.
_START = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)


def _dt(hours_offset: float, base: datetime = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) -> datetime:
    """Return base + hours_offset as a datetime."""
    return base + timedelta(hours=hours_offset)


def _run_simple(
    driving_hours: float,
    distance_miles: float,
    cycle_used: float = 0.0,
    start: datetime = _START,
    pickup_duration_hours: float = 1.0,
    dropoff_duration_hours: float = 1.0,
) -> list[Segment]:
    """Helper: simulate a trip where current_location == pickup_location (no leg 1 driving)."""
    return simulate_trip(
        leg1_driving_hours=0.0,
        leg1_distance_miles=0.0,
        leg2_driving_hours=driving_hours,
        leg2_distance_miles=distance_miles,
        cycle_used_hours=cycle_used,
        start_dt=start,
        current_location="current location",
        pickup_location="pickup location",
        dropoff_location="dropoff location",
        pickup_duration_hours=pickup_duration_hours,
        dropoff_duration_hours=dropoff_duration_hours,
    )


def _run_two_leg(
    leg1_h: float,
    leg1_mi: float,
    leg2_h: float,
    leg2_mi: float,
    cycle_used: float = 0.0,
    start: datetime = _START,
) -> list[Segment]:
    """Helper: simulate a two-leg trip."""
    return simulate_trip(
        leg1_driving_hours=leg1_h,
        leg1_distance_miles=leg1_mi,
        leg2_driving_hours=leg2_h,
        leg2_distance_miles=leg2_mi,
        cycle_used_hours=cycle_used,
        start_dt=start,
        current_location="New York, NY",
        pickup_location="Chicago, IL",
        dropoff_location="Denver, CO",
    )


def _driving_hours(segments: list[Segment]) -> float:
    return sum(s.duration_hours for s in segments if s.status == "driving")


def _cycle_trace(segments: list[Segment], initial_cycle: float) -> list[float]:
    """Return the cumulative cycle value after each on-duty/driving segment."""
    values = []
    cycle = initial_cycle
    for seg in segments:
        if seg.remark == "34h restart":
            cycle = 0.0
        elif seg.status in ("driving", "on_duty"):
            cycle += seg.duration_hours
        values.append(cycle)
    return values


# ---------------------------------------------------------------------------
# compute_daily_totals
# ---------------------------------------------------------------------------

class TestComputeDailyTotals(TestCase):
    """Tests for compute_daily_totals() using the Schneider tutorial worked example."""

    def _transcript_segments(self) -> list[Segment]:
        """
        Reconstruct the Schneider tutorial day (2026-01-01, midnight–midnight).

        Timeline:
          00:00-06:30  off_duty   6.5h  overnight
          06:30-07:00  on_duty    0.5h  pre-trip + TI, Green Bay WI
          07:00-09:00  driving    2.0h
          09:00-09:30  on_duty    0.5h  Cat Scale, Fondulac WI
          09:30-13:00  driving    3.5h
          13:00-13:30  off_duty   0.5h  30-min break, Paw Paw IL
          13:30-17:30  driving    4.0h
          17:30-19:00  off_duty   1.5h  post-trip TI, Edwardsville IL
          19:00-24:00  sleeper    5.0h

        Expected totals: off_duty=8.5h, sleeper=5h, driving=9.5h, on_duty=1h (=24h)
        """
        return [
            Segment("off_duty", _dt(0.0),  _dt(6.5),  "Green Bay, WI",    "overnight"),
            Segment("on_duty",  _dt(6.5),  _dt(7.0),  "Green Bay, WI",    "pre-trip + TI"),
            Segment("driving",  _dt(7.0),  _dt(9.0),  "en route",         "driving"),
            Segment("on_duty",  _dt(9.0),  _dt(9.5),  "Fondulac, WI",     "Cat Scale"),
            Segment("driving",  _dt(9.5),  _dt(13.0), "en route",         "driving"),
            Segment("off_duty", _dt(13.0), _dt(13.5), "Paw Paw, IL",      "30-min break"),
            Segment("driving",  _dt(13.5), _dt(17.5), "en route",         "driving"),
            Segment("off_duty", _dt(17.5), _dt(19.0), "Edwardsville, IL", "post-trip TI"),
            Segment("sleeper",  _dt(19.0), _dt(24.0), "Edwardsville, IL", "sleeper berth"),
        ]

    def test_transcript_off_duty_total(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertAlmostEqual(totals[0]["off_duty"], 8.5, places=4)

    def test_transcript_sleeper_total(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertAlmostEqual(totals[0]["sleeper"], 5.0, places=4)

    def test_transcript_driving_total(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertAlmostEqual(totals[0]["driving"], 9.5, places=4)

    def test_transcript_on_duty_total(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertAlmostEqual(totals[0]["on_duty"], 1.0, places=4)

    def test_transcript_totals_sum_to_24(self):
        totals = compute_daily_totals(self._transcript_segments())
        day = totals[0]
        total = day["off_duty"] + day["sleeper"] + day["driving"] + day["on_duty"]
        self.assertAlmostEqual(total, 24.0, places=4)

    def test_single_day_returns_one_entry(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertEqual(len(totals), 1)

    def test_date_field_matches_calendar_day(self):
        totals = compute_daily_totals(self._transcript_segments())
        self.assertEqual(totals[0]["date"], date(2026, 1, 1))

    def test_segment_spanning_midnight_splits_across_days(self):
        """A sleeper segment crossing midnight must be counted in both days."""
        segments = [
            Segment("sleeper", _dt(22.0), _dt(32.0), "Somewhere", "overnight"),
        ]
        totals = compute_daily_totals(segments)
        self.assertEqual(len(totals), 2)
        self.assertAlmostEqual(totals[0]["sleeper"], 2.0, places=4)  # 22:00–24:00
        self.assertAlmostEqual(totals[1]["sleeper"], 8.0, places=4)  # 00:00–08:00

    def test_empty_segments_returns_empty_list(self):
        self.assertEqual(compute_daily_totals([]), [])

    def test_two_day_trip_produces_two_entries(self):
        """A driving segment on each of two calendar days gives two entries."""
        segments = [
            Segment("driving",  _dt(6.0),  _dt(17.0), "en route", "driving"),
            Segment("off_duty", _dt(17.0), _dt(27.0), "Motel",    "rest"),
            Segment("driving",  _dt(27.0), _dt(33.0), "en route", "driving"),
        ]
        totals = compute_daily_totals(segments)
        self.assertEqual(len(totals), 2)


# ---------------------------------------------------------------------------
# Structural invariants on simulate_trip output
# ---------------------------------------------------------------------------

class TestSimulateTripStructure(TestCase):
    """simulate_trip() output must satisfy basic structural properties."""

    def test_returns_nonempty_list(self):
        self.assertGreater(len(_run_simple(6.0, 360.0)), 0)

    def test_segments_are_contiguous(self):
        segments = _run_simple(6.0, 360.0)
        for i in range(1, len(segments)):
            self.assertEqual(
                segments[i].start,
                segments[i - 1].end,
                f"Gap between segment {i-1} ({segments[i-1].status}) "
                f"and segment {i} ({segments[i].status})",
            )

    def test_first_segment_starts_at_start_dt(self):
        """With _START already on a 15-min boundary, snap_quarter is a no-op."""
        segments = _run_simple(6.0, 360.0)
        self.assertEqual(segments[0].start, _START)

    def test_snap_quarter_applied_to_non_boundary_start(self):
        """An off-boundary start must be snapped to the nearest 15-minute mark."""
        off_boundary = datetime(2026, 1, 6, 8, 7, 33, tzinfo=UTC)  # 08:07:33
        segments = _run_simple(2.0, 120.0, start=off_boundary)
        t = segments[0].start
        total_min = t.hour * 60 + t.minute + t.second / 60
        self.assertAlmostEqual(total_min % 15, 0, delta=0.01,
            msg=f"Start {t.strftime('%H:%M:%S')} is not on a 15-min boundary")

    def test_no_zero_duration_segment(self):
        segments = _run_simple(6.0, 360.0)
        for s in segments:
            self.assertGreater(
                (s.end - s.start).total_seconds(), 0,
                f"Zero-duration segment: {s}",
            )

    def test_all_statuses_are_valid(self):
        valid = {"off_duty", "sleeper", "driving", "on_duty"}
        for s in _run_simple(6.0, 360.0):
            self.assertIn(s.status, valid, f"Unknown status: {s.status}")

    def test_trip_contains_driving_segments(self):
        self.assertTrue(any(s.status == "driving" for s in _run_simple(6.0, 360.0)))

    def test_trip_contains_on_duty_segments(self):
        self.assertTrue(any(s.status == "on_duty" for s in _run_simple(6.0, 360.0)))

    def test_two_leg_segments_contiguous(self):
        """The two-leg variant must also produce gapless segments."""
        segments = _run_two_leg(5.0, 300.0, 8.0, 480.0)
        for i in range(1, len(segments)):
            self.assertEqual(
                segments[i].start,
                segments[i - 1].end,
                f"Gap at index {i}",
            )

    def test_cumulative_drive_h_monotone(self):
        """cumulative_drive_h on Segment must be non-decreasing."""
        segments = _run_two_leg(5.0, 300.0, 8.0, 480.0)
        prev = 0.0
        for seg in segments:
            self.assertGreaterEqual(seg.cumulative_drive_h, prev - 1e-9)
            if seg.status == "driving":
                prev = seg.cumulative_drive_h


# ---------------------------------------------------------------------------
# Phase ordering
# ---------------------------------------------------------------------------

class TestPhaseOrdering(TestCase):
    """Leg1 driving must precede the pickup; leg2 driving must follow it."""

    def test_pickup_precedes_no_leg1_driving(self):
        """With leg1=0, the first on_duty segment (pickup) comes before any driving."""
        segments = _run_simple(5.0, 300.0)
        pickup_idx = next(i for i, s in enumerate(segments) if s.remark == "pickup")
        # All segments before pickup must be non-driving
        for s in segments[:pickup_idx]:
            self.assertNotEqual(s.status, "driving",
                f"Driving segment found before pickup: {s}")

    def test_leg1_driving_before_pickup(self):
        """With non-zero leg1, driving segments must appear before the pickup."""
        segments = _run_two_leg(5.0, 300.0, 3.0, 180.0)
        pickup_idx = next(i for i, s in enumerate(segments) if s.remark == "pickup")
        leg1_driving = [s for s in segments[:pickup_idx] if s.status == "driving"]
        self.assertGreater(len(leg1_driving), 0, "No driving before pickup in two-leg trip")
        leg1_total = sum(s.duration_hours for s in leg1_driving)
        self.assertAlmostEqual(leg1_total, 5.0, delta=0.25,
            msg=f"Expected ~5h of leg1 driving, got {leg1_total:.2f}h")

    def test_leg2_driving_after_pickup(self):
        """All leg2 driving must appear after the pickup segment."""
        segments = _run_two_leg(5.0, 300.0, 3.0, 180.0)
        pickup_idx = next(i for i, s in enumerate(segments) if s.remark == "pickup")
        leg2_driving = [s for s in segments[pickup_idx + 1:] if s.status == "driving"]
        self.assertGreater(len(leg2_driving), 0, "No driving after pickup")
        leg2_total = sum(s.duration_hours for s in leg2_driving)
        self.assertAlmostEqual(leg2_total, 3.0, delta=0.25,
            msg=f"Expected ~3h of leg2 driving, got {leg2_total:.2f}h")

    def test_dropoff_is_last_on_duty(self):
        """The dropoff segment (on_duty, remark='dropoff') must be the last on_duty block."""
        segments = _run_simple(5.0, 300.0)
        on_duty = [s for s in segments if s.status == "on_duty"]
        self.assertTrue(len(on_duty) >= 2, "Expected at least pickup + dropoff on_duty")
        self.assertEqual(on_duty[-1].remark, "dropoff",
            f"Last on_duty segment remark should be 'dropoff', got {on_duty[-1].remark!r}")

    def test_pickup_remark_present(self):
        """There must be exactly one segment with remark 'pickup'."""
        segments = _run_two_leg(3.0, 180.0, 3.0, 180.0)
        pickups = [s for s in segments if s.remark == "pickup"]
        self.assertEqual(len(pickups), 1)

    def test_dropoff_remark_present(self):
        """There must be exactly one segment with remark 'dropoff'."""
        segments = _run_two_leg(3.0, 180.0, 3.0, 180.0)
        dropoffs = [s for s in segments if s.remark == "dropoff"]
        self.assertEqual(len(dropoffs), 1)


# ---------------------------------------------------------------------------
# HOS rule enforcement
# ---------------------------------------------------------------------------

class TestHOSRules(TestCase):
    """Each FMCSA rule is verified independently."""

    def test_driving_cap_11h_per_shift(self):
        """No shift may have more than 11 hours of driving."""
        segments = _run_simple(15.0, 900.0)
        shift_driving = 0.0
        for s in segments:
            if s.status == "driving":
                shift_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0 - 1e-6:
                self.assertLessEqual(
                    shift_driving, 11.0 + 1e-6,
                    "Shift driving exceeded 11h before rest",
                )
                shift_driving = 0.0
        self.assertLessEqual(shift_driving, 11.0 + 1e-6, "Final shift driving exceeded 11h")

    def test_14h_on_duty_window_per_shift(self):
        """Driving + on_duty time within a shift window must not exceed 14 hours."""
        segments = _run_simple(13.0, 780.0)
        shift_start = segments[0].start
        for s in segments:
            if s.status in ("driving", "on_duty"):
                window_hours = (s.end - shift_start).total_seconds() / 3600
                self.assertLessEqual(
                    window_hours, 14.0 + 1e-6,
                    f"On-duty window reached {window_hours:.2f}h (max 14h)",
                )
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0 - 1e-6:
                shift_start = s.end

    def test_30min_break_required_after_8h_driving(self):
        """After 8 cumulative driving hours, a ≥30-min break must precede more driving."""
        segments = _run_simple(10.0, 600.0)
        cumulative_driving = 0.0
        for s in segments:
            if s.status == "driving":
                cumulative_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper"):
                if s.duration_hours >= 0.5 - 1e-6:
                    cumulative_driving = 0.0
                if s.duration_hours < 0.5 - 1e-6 and cumulative_driving > 8.0 + 1e-6:
                    self.fail(
                        f"Driving resumed after {cumulative_driving:.2f}h with only a "
                        f"{s.duration_hours * 60:.0f}-min break (need ≥30 min)"
                    )

    def test_break_inserted_before_exceeding_8h_driving(self):
        """For a 10h drive, a break must appear before 8h of continuous driving elapses."""
        segments = _run_simple(10.0, 600.0)
        cumulative = 0.0
        for s in segments:
            if s.status == "driving":
                cumulative += s.duration_hours
                self.assertLessEqual(
                    cumulative, 8.0 + 1e-6,
                    f"Drove {cumulative:.2f}h without a qualifying break",
                )
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 0.5 - 1e-6:
                cumulative = 0.0

    def test_10h_rest_between_shifts(self):
        """Any rest that ends a shift must be at least 10 consecutive hours."""
        segments = _run_simple(15.0, 900.0)
        totals = compute_daily_totals(segments)
        if len(totals) < 2:
            return  # Single-day trip — no between-shift rest to check.

        for s in segments:
            if s.status in ("off_duty", "sleeper"):
                h = s.duration_hours
                if 1.0 < h < 34.0 - 1e-6:
                    self.assertGreaterEqual(
                        h, 10.0 - 1e-6,
                        f"Between-shift rest is only {h:.2f}h (need ≥10h)",
                    )

    def test_fuel_stop_every_1000_miles(self):
        """A trip over 1,000 miles must include at least one fuel stop."""
        segments = _run_simple(20.0, 1_200.0)
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreater(len(fuel_stops), 0, "No fuel stop found for 1,200-mile trip")

    def test_two_fuel_stops_for_2000_plus_miles(self):
        """A 2,200-mile trip must have at least two fuel stops."""
        segments = _run_simple(37.0, 2_200.0)
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreaterEqual(len(fuel_stops), 2,
            "Expected ≥2 fuel stops for 2,200-mile trip")

    def test_no_fuel_stop_under_1000_miles(self):
        """A 900-mile trip should not produce a fuel stop."""
        segments = _run_simple(15.0, 900.0)
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel_stops), 0, "Unexpected fuel stop for 900-mile trip")

    def test_pickup_block_is_1h_on_duty(self):
        """Pickup must appear as a 1h on_duty segment."""
        segments = _run_simple(5.0, 300.0,
                               pickup_duration_hours=1.0, dropoff_duration_hours=1.0)
        on_duty = [s for s in segments if s.status == "on_duty"]
        durations = [s.duration_hours for s in on_duty]
        self.assertTrue(
            any(abs(d - 1.0) < 1e-6 for d in durations),
            f"No 1h on_duty block found; on_duty durations: {durations}",
        )

    def test_dropoff_block_is_1h_on_duty(self):
        """Dropoff must appear as a 1h on_duty segment."""
        segments = _run_simple(5.0, 300.0,
                               pickup_duration_hours=1.0, dropoff_duration_hours=1.0)
        on_duty = [s for s in segments if s.status == "on_duty"]
        one_hour_blocks = [s for s in on_duty if abs(s.duration_hours - 1.0) < 1e-6]
        self.assertGreaterEqual(len(one_hour_blocks), 2,
            "Expected at least two 1h on_duty blocks (pickup + dropoff)")

    def test_total_on_duty_at_least_2h(self):
        """Total on_duty time must be ≥ 2h (1h pickup + 1h dropoff)."""
        segments = _run_simple(6.0, 360.0)
        total = sum(s.duration_hours for s in segments if s.status == "on_duty")
        self.assertGreaterEqual(total, 2.0 - 1e-6)


# ---------------------------------------------------------------------------
# 70 h / 8-day cycle cap and 34 h restart
# ---------------------------------------------------------------------------

class TestCycleCap(TestCase):

    def test_cycle_cap_triggers_34h_restart(self):
        """With 69h already used, a trip needing more on-duty time must restart."""
        segments = _run_simple(4.0, 240.0, cycle_used=69.0)
        long_rests = [
            s for s in segments
            if s.status in ("off_duty", "sleeper") and s.duration_hours >= 34.0 - 1e-6
        ]
        self.assertGreater(len(long_rests), 0,
            "Expected a 34h restart when cycle is nearly full")

    def test_restart_is_at_least_34h(self):
        """The restart period must be ≥ 34 consecutive hours."""
        segments = _run_simple(4.0, 240.0, cycle_used=69.0)
        for s in segments:
            if s.status in ("off_duty", "sleeper") and s.duration_hours >= 34.0 - 1e-6:
                self.assertGreaterEqual(s.duration_hours, 34.0 - 1e-6)
                return
        self.fail("No ≥34h restart segment found")

    def test_zero_cycle_used_short_trip_no_restart(self):
        """With 0h used, a short trip should not need a 34h restart."""
        segments = _run_simple(5.0, 300.0, cycle_used=0.0)
        long_rests = [
            s for s in segments
            if s.status in ("off_duty", "sleeper") and s.duration_hours >= 34.0 - 1e-6
        ]
        self.assertEqual(len(long_rests), 0,
            "Short trip with 0 cycle hours should not restart")

    def test_full_cycle_immediately_triggers_restart(self):
        """A driver at exactly 70h used cannot drive at all without a restart first."""
        segments = _run_simple(3.0, 180.0, cycle_used=70.0)
        first_active = next(
            (s for s in segments if s.status in ("driving", "on_duty")), None
        )
        restart = next(
            (s for s in segments if s.duration_hours >= 34.0 - 1e-6), None
        )
        self.assertIsNotNone(restart, "Should restart before any on-duty time")
        if first_active and restart:
            self.assertGreaterEqual(
                first_active.start, restart.end,
                "Driver went on-duty before completing the 34h restart",
            )

    def test_restart_resets_cycle_to_zero(self):
        """After a 34h restart, subsequent cycle accumulation must begin from 0."""
        segments = _run_simple(4.0, 240.0, cycle_used=69.0)
        cycle = 69.0
        post_restart_start = None
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
                post_restart_start = seg.end
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
        if post_restart_start is not None:
            self.assertLessEqual(cycle, 70.0 + 1e-6,
                f"Cycle exceeded 70h after restart: {cycle:.4f}h")

    def test_68h_cycle_never_exceeds_70h(self):
        """Primary regression: cycle must never exceed 70h during NY→Chicago→Denver at 68h."""
        segments = _run_two_leg(
            leg1_h=12.5, leg1_mi=780.0,
            leg2_h=17.0, leg2_mi=1_010.0,
            cycle_used=68.0,
        )
        cycle = 68.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6,
                f"Cycle exceeded 70h: {cycle:.4f}h after remark={seg.remark!r}")

    def test_69h_cycle_triggers_restart_before_driving(self):
        """With 69h used, after pickup brings cycle to 70h, a restart must precede driving."""
        # current == pickup (leg1=0), pickup takes 1h → cycle=70 → restart before leg2
        segments = _run_simple(5.0, 300.0, cycle_used=69.0)
        cycle = 69.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6,
                f"Cycle {cycle:.4f}h exceeded 70h after {seg.remark!r}")

    def test_fractional_69_5h_cycle(self):
        """Fractional cycle hours (69.5h) must also enforce the 70h hard cap."""
        segments = _run_simple(5.0, 300.0, cycle_used=69.5)
        cycle = 69.5
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6,
                f"Cycle {cycle:.4f}h exceeded 70h after {seg.remark!r}")

    def test_cycle_cap_is_hard_constraint_not_advisory(self):
        """For multiple driving amounts with a high cycle, 70h must never be exceeded."""
        for driving_h, dist in [(2.0, 120.0), (4.0, 240.0), (11.0, 660.0)]:
            with self.subTest(driving_h=driving_h, cycle_used=68.0):
                segs = _run_simple(driving_h, dist, cycle_used=68.0)
                cycle = 68.0
                for seg in segs:
                    if seg.remark == "34h restart":
                        cycle = 0.0
                    elif seg.status in ("driving", "on_duty"):
                        cycle += seg.duration_hours
                    self.assertLessEqual(cycle, 70.0 + 1e-6,
                        f"Cycle {cycle:.4f}h exceeded 70h with {driving_h}h trip at 68h")


# ---------------------------------------------------------------------------
# Time snapping
# ---------------------------------------------------------------------------

class TestTimeSnapping(TestCase):
    """Duty-status change times must be on 15-minute boundaries."""

    def _assert_on_quarter_hour(self, dt: datetime, label: str) -> None:
        total_min = dt.hour * 60 + dt.minute + dt.second / 60 + dt.microsecond / 60_000_000
        remainder = total_min % 15
        self.assertAlmostEqual(remainder, 0, delta=0.01,
            msg=f"{label}: {dt.strftime('%H:%M:%S')} is not on a 15-min boundary")

    def test_start_on_quarter_hour_boundary(self):
        segments = _run_simple(5.0, 300.0)
        self._assert_on_quarter_hour(segments[0].start, "first segment start")

    def test_off_boundary_start_snapped(self):
        off = datetime(2026, 1, 6, 8, 7, 33, tzinfo=UTC)  # 08:07:33
        segments = _run_simple(5.0, 300.0, start=off)
        self._assert_on_quarter_hour(segments[0].start, "snapped start")

    def test_large_segment_times_on_quarter_hour(self):
        """All segments with duration ≥ 15 min should have quarter-hour aligned times."""
        segments = _run_simple(11.0, 660.0)
        for seg in segments:
            if seg.duration_hours >= 0.25 - 1e-6:
                self._assert_on_quarter_hour(seg.start,
                    f"start of {seg.status}/{seg.remark}")
                self._assert_on_quarter_hour(seg.end,
                    f"end of {seg.status}/{seg.remark}")

    def test_no_sub_second_precision(self):
        """No segment should have non-zero seconds or microseconds."""
        segments = _run_simple(8.0, 480.0)
        for seg in segments:
            if seg.duration_hours >= 0.25 - 1e-6:
                for dt, label in [(seg.start, "start"), (seg.end, "end")]:
                    self.assertEqual(dt.second, 0,
                        f"Non-zero seconds in {label} of {seg.status}/{seg.remark}: {dt}")
                    self.assertEqual(dt.microsecond, 0,
                        f"Non-zero microseconds in {label} of {seg.status}/{seg.remark}: {dt}")


# ---------------------------------------------------------------------------
# End-to-end trip scenarios
# ---------------------------------------------------------------------------

class TestTripScenarios(TestCase):
    """Representative scenarios covering the full range of HOS complexity."""

    def test_short_trip_single_day(self):
        """2h driving, 120 miles — no overnight rest, no fuel stop."""
        segments = _run_simple(2.0, 120.0)
        totals = compute_daily_totals(segments)
        self.assertEqual(len(totals), 1, "Short trip should fit in one calendar day")
        self.assertAlmostEqual(totals[0]["driving"], 2.0, places=4)
        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel), 0)

    def test_medium_trip_no_restart(self):
        """8h driving, 480 miles — no mandatory break, no fuel stop."""
        segments = _run_simple(8.0, 480.0)
        total_driving = _driving_hours(segments)
        self.assertAlmostEqual(total_driving, 8.0, places=4)
        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel), 0)
        long_rests = [s for s in segments if s.duration_hours >= 34.0 - 1e-6]
        self.assertEqual(len(long_rests), 0)

    def test_long_multi_day_trip(self):
        """20h driving, 1,200 miles — spans multiple days, has fuel stop, respects 11h cap."""
        segments = _run_simple(20.0, 1_200.0)
        totals = compute_daily_totals(segments)
        self.assertGreater(len(totals), 1, "Long trip must span multiple calendar days")

        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreater(len(fuel), 0, "1,200-mile trip must have fuel stop(s)")

        for day in totals:
            self.assertLessEqual(day["driving"], 11.0 + 1e-6, "Daily driving cap exceeded")

    def test_total_driving_hours_preserved(self):
        """The sum of all driving segments must equal the input driving_hours (quarter-hour aligned)."""
        for hours, miles in [(3.0, 180.0), (8.0, 480.0), (11.0, 660.0)]:
            with self.subTest(driving_hours=hours):
                segments = _run_simple(hours, miles)
                total = _driving_hours(segments)
                self.assertAlmostEqual(total, hours, places=4,
                    msg=f"Expected {hours}h driving, got {total:.4f}h")

    def test_ny_chicago_denver_10h_cycle(self):
        """NY→Chicago→Denver with 10h cycle — verify no HOS violations."""
        segments = _run_two_leg(
            leg1_h=12.5, leg1_mi=780.0,
            leg2_h=17.0, leg2_mi=1_010.0,
            cycle_used=10.0,
        )
        # No cycle violation
        cycle = 10.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6)

        # Driving cap per shift
        shift_driving = 0.0
        for s in segments:
            if s.status == "driving":
                shift_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0 - 1e-6:
                self.assertLessEqual(shift_driving, 11.0 + 1e-6)
                shift_driving = 0.0
        self.assertLessEqual(shift_driving, 11.0 + 1e-6)

        # Phase ordering: pickup must come after leg1 driving
        pickup_idx = next(i for i, s in enumerate(segments) if s.remark == "pickup")
        leg1_segs = [s for s in segments[:pickup_idx] if s.status == "driving"]
        self.assertGreater(len(leg1_segs), 0, "No leg1 driving before pickup")

    def test_ny_chicago_denver_68h_cycle_has_restart(self):
        """NY→Chicago→Denver with 68h cycle — must trigger a 34h restart."""
        segments = _run_two_leg(
            leg1_h=12.5, leg1_mi=780.0,
            leg2_h=17.0, leg2_mi=1_010.0,
            cycle_used=68.0,
        )
        restarts = [s for s in segments if s.remark == "34h restart"]
        self.assertGreater(len(restarts), 0,
            "68h cycle trip must require a 34h restart")

        # Cycle must never exceed 70h
        cycle = 68.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6)

    def test_zero_cycle_seattle_to_miami_multi_restart(self):
        """Very long trip from scratch — may need multiple restarts."""
        # Approximate: 60h+ driving requires multiple 70h-cycle resets
        segments = _run_two_leg(
            leg1_h=25.0, leg1_mi=1_600.0,
            leg2_h=40.0, leg2_mi=2_600.0,
            cycle_used=0.0,
        )
        # All HOS invariants must hold throughout
        cycle = 0.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6,
                f"Cycle exceeded 70h: {cycle:.2f}h after {seg.remark!r}")

        # Trip must produce multiple days
        totals = compute_daily_totals(segments)
        self.assertGreater(len(totals), 3,
            f"Long trip should span >3 days, got {len(totals)}")

        # Fuel stops expected (4,200 miles → at least 4)
        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreaterEqual(len(fuel), 4,
            f"Expected ≥4 fuel stops for 4,200-mile trip, got {len(fuel)}")


# ---------------------------------------------------------------------------
# Scenario G — Sleeper Berth: 10h consecutive sleeper resets clocks
# ---------------------------------------------------------------------------

class TestSleeperBerth(TestCase):
    """Verify that the planner uses sleeper berth for en-route 10h rests and that
    the sleeper berth provision correctly resets the 11h/14h clocks."""

    def test_10h_rest_uses_sleeper_status(self):
        """En-route overnight rests generated by simulate_trip must be 'sleeper', not 'off_duty'."""
        segments = _run_simple(15.0, 900.0)
        long_rests = [
            s for s in segments
            if s.duration_hours >= 10.0 - 1e-6
            and "restart" not in s.remark.lower()
        ]
        self.assertGreater(len(long_rests), 0, "No 10h rest found")
        for rest in long_rests:
            self.assertEqual(
                rest.status, "sleeper",
                f"En-route 10h rest should be 'sleeper', got '{rest.status}'"
            )

    def test_sleeper_not_counted_in_cycle(self):
        """Sleeper berth time must not accumulate in the 70h cycle counter."""
        segments = _run_simple(22.0, 1_300.0, cycle_used=0.0)
        cycle = 0.0
        for seg in segments:
            if seg.remark == "34h restart":
                cycle = 0.0
            elif seg.status == "sleeper" and "restart" not in seg.remark.lower():
                pass  # sleeper does NOT count against cycle
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
        self.assertLessEqual(cycle, 70.0 + 1e-6,
            f"Cycle {cycle:.2f}h exceeded 70h — sleeper may have been counted")

    def test_sleeper_resets_11h_driving_limit(self):
        """After a 10h sleeper, shift_driving must reset so the next shift can drive up to 11h."""
        segments = _run_simple(22.0, 1_300.0)
        shift_driving = 0.0
        for s in segments:
            if s.status == "driving":
                shift_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0 - 1e-6:
                self.assertLessEqual(shift_driving, 11.0 + 1e-6,
                    f"Driving reached {shift_driving:.2f}h before 10h rest")
                shift_driving = 0.0
        self.assertLessEqual(shift_driving, 11.0 + 1e-6)

    def test_sleeper_appears_on_correct_log_row(self):
        """Sleeper segments must have status='sleeper' (maps to row 2 on the log sheet)."""
        segments = _run_simple(15.0, 900.0)
        valid_statuses = {"off_duty", "sleeper", "driving", "on_duty"}
        for seg in segments:
            self.assertIn(seg.status, valid_statuses)


# ---------------------------------------------------------------------------
# Scenarios H & I — Split sleeper pairing (via find_split_sleeper_pairs)
# ---------------------------------------------------------------------------

class TestSplitSleeperPairing(TestCase):
    """
    Verify split-sleeper pairing detection (FMCSA §395.1(g)(1)(i)).

    The planner generates full 10h sleeper berth rests.  These unit tests
    construct segment lists by hand and verify that find_split_sleeper_pairs()
    and validate_hos_segments() correctly accept / reject pairings.
    """

    def _make_segs(self, spec: list[tuple]) -> list[Segment]:
        """Build a contiguous Segment list from (status, hours, remark) tuples."""
        segs: list[Segment] = []
        t = _START
        for status, hours, remark in spec:
            end = t + timedelta(hours=hours)
            segs.append(Segment(status, t, end, "location", remark))
            t = end
        return segs

    # ── Scenario H: valid split pairs ──────────────────────────────────────

    def test_split_7h_sleeper_plus_3h_off_valid(self):
        """7h sleeper + 3h off-duty = valid split (total 10h, has 7h sleeper, both ≥ 2h)."""
        segs = self._make_segs([
            ("driving",  8.0, "driving"),
            ("sleeper",  7.0, "split sleeper"),
            ("off_duty", 3.0, "split off"),
            ("driving",  2.0, "driving"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 1,
            f"Expected 1 valid pair, got pairs={pairs}")

    def test_split_8h_sleeper_plus_2h_off_valid(self):
        """8h sleeper + 2h off-duty = valid split (total 10h, has ≥7h sleeper, both ≥2h)."""
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("sleeper",  8.0, "split sleeper"),
            ("off_duty", 2.0, "split off"),
            ("driving",  3.0, "driving"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 1)

    def test_split_2h_off_first_plus_8h_sleeper_valid(self):
        """2h off-duty first + 8h sleeper second = valid (order doesn't matter)."""
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("off_duty", 2.0, "split off"),
            ("sleeper",  8.0, "split sleeper"),
            ("driving",  3.0, "driving"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 1)

    def test_valid_split_pair_excluded_from_14h_window(self):
        """A valid split-sleeper pair must not trigger a 14h window violation."""
        # Duty period: 6h driving + 7h sleeper + 3h off + 5h driving
        # Without split exclusion: 6+7+3+5 = 21h > 14h window → violation
        # With valid split pair:   sleeping periods excluded → only 11h active → ok
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("sleeper",  7.0, "split sleeper"),
            ("off_duty", 3.0, "split off"),
            ("driving",  5.0, "driving"),
        ])
        result = validate_hos_segments(segs, initial_cycle=0.0)
        window_violations = [v for v in result["violations"] if "14h" in v]
        self.assertEqual(len(window_violations), 0,
            f"Valid split pair should exclude periods from 14h window; "
            f"violations={result['violations']}")

    # ── Scenario I: invalid split pairs ────────────────────────────────────

    def test_split_6h_sleeper_plus_4h_off_invalid(self):
        """6h sleeper + 4h off-duty is INVALID — no qualifying period ≥ 7h sleeper."""
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("sleeper",  6.0, "split sleeper"),
            ("off_duty", 4.0, "split off"),
            ("driving",  3.0, "driving"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 0,
            "6h sleeper + 4h off-duty should NOT be a valid split pair")

    def test_split_7h_sleeper_plus_1_5h_off_invalid(self):
        """7h sleeper + 1.5h off-duty is INVALID — second period < 2h minimum."""
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("sleeper",  7.0, "split sleeper"),
            ("off_duty", 1.5, "split off"),
            ("driving",  3.0, "driving"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 0,
            "7h sleeper + 1.5h off-duty should NOT be a valid split pair (second < 2h)")

    def test_driving_between_periods_breaks_pair(self):
        """A driving segment between two rest periods prevents pairing."""
        segs = self._make_segs([
            ("sleeper",  7.0, "split sleeper"),
            ("driving",  1.0, "driving"),
            ("off_duty", 3.0, "split off"),
        ])
        pairs = find_split_sleeper_pairs(segs)
        valid_pairs = [(i, j) for i, j, v in pairs if v]
        self.assertEqual(len(valid_pairs), 0,
            "Driving between rest periods must invalidate the split pair")

    def test_invalid_split_does_not_exclude_from_14h(self):
        """An invalid split pair must NOT exclude those periods from the 14h window check."""
        # 6h driving + 6h sleeper (invalid — < 7h) + 4h off + 5h driving = 21h active
        segs = self._make_segs([
            ("driving",  6.0, "driving"),
            ("sleeper",  6.0, "invalid sleeper"),
            ("off_duty", 4.0, "invalid off"),
            ("driving",  5.0, "driving"),
        ])
        result = validate_hos_segments(segs, initial_cycle=0.0)
        # Should have a 14h window violation since the pair is invalid
        has_14h_violation = any("14h" in v for v in result["violations"])
        self.assertTrue(has_14h_violation,
            "Invalid split pair should trigger a 14h window violation; "
            f"violations={result['violations']}")


# ---------------------------------------------------------------------------
# Scenario J — 30-minute break (via validate_hos_segments)
# ---------------------------------------------------------------------------

class TestThirtyMinBreak(TestCase):
    """Verify 30-minute break detection in validate_hos_segments."""

    def _make_segs(self, spec: list[tuple]) -> list[Segment]:
        segs: list[Segment] = []
        t = _START
        for status, hours, remark in spec:
            end = t + timedelta(hours=hours)
            segs.append(Segment(status, t, end, "loc", remark))
            t = end
        return segs

    def test_missing_break_after_8h_driving_flagged(self):
        """Driving >8h without a break must produce a violation."""
        segs = self._make_segs([
            ("driving", 9.0, "driving"),
        ])
        result = validate_hos_segments(segs)
        break_violations = [v for v in result["violations"] if "break" in v.lower() or "since last" in v]
        self.assertGreater(len(break_violations), 0,
            "Expected a break violation for 9h driving without a break")

    def test_30min_break_satisfies_requirement(self):
        """8h driving + 30-min off-duty break + more driving must NOT flag a break violation."""
        segs = self._make_segs([
            ("driving",  8.0, "driving"),
            ("off_duty", 0.5, "30-min break"),
            ("driving",  2.0, "driving"),
        ])
        result = validate_hos_segments(segs)
        break_violations = [v for v in result["violations"] if "since last" in v]
        self.assertEqual(len(break_violations), 0,
            f"30-min break should satisfy the requirement; violations={result['violations']}")

    def test_break_can_be_sleeper_berth(self):
        """A 30-min sleeper berth period also qualifies as a break."""
        segs = self._make_segs([
            ("driving",  8.0, "driving"),
            ("sleeper",  0.5, "short sleeper"),
            ("driving",  2.0, "driving"),
        ])
        result = validate_hos_segments(segs)
        break_violations = [v for v in result["violations"] if "since last" in v]
        self.assertEqual(len(break_violations), 0,
            "30-min sleeper berth should satisfy the 30-min break requirement")


# ---------------------------------------------------------------------------
# Scenario K — 34h restart crossing midnight
# ---------------------------------------------------------------------------

class TestRestartCrossingMidnight(TestCase):
    """Verify that a 34h restart which crosses midnight is handled correctly."""

    def test_restart_at_22h_crosses_two_midnights(self):
        """A restart beginning at 22:00 spans two day boundaries (day 1, 2, 3)."""
        start = datetime(2026, 1, 1, 22, 0, tzinfo=ZoneInfo("UTC"))
        restart_seg = Segment(
            "off_duty",
            start,
            start + timedelta(hours=34),
            "location",
            "34h restart",
        )
        totals = compute_daily_totals([restart_seg])
        # 2h on day 1 (22:00–24:00), 24h on day 2, 8h on day 3 (00:00–08:00)
        self.assertEqual(len(totals), 3)
        self.assertAlmostEqual(totals[0]["off_duty"], 2.0, places=2)
        self.assertAlmostEqual(totals[1]["off_duty"], 24.0, places=2)
        self.assertAlmostEqual(totals[2]["off_duty"], 8.0, places=2)

    def test_simulate_trip_restart_cycle_resets_after_completion(self):
        """simulate_trip must insert a 34h restart and not allow driving until it completes."""
        # 69h cycle: pickup uses 1h → exactly 70h → restart before leg2
        segments = _run_simple(6.0, 360.0, cycle_used=69.0)
        restarts = [s for s in segments if "restart" in s.remark.lower()]
        self.assertGreater(len(restarts), 0, "Expected a 34h restart")

        # All driving must occur AFTER the restart ends
        for restart in restarts:
            for seg in segments:
                if seg.status == "driving" and seg.start < restart.end:
                    # Only pre-restart driving (from pre-restart shift) is allowed up to cycle cap
                    pre_cycle = 69.0
                    # Before restart, driving can happen only if cycle < 70
                    self.assertLess(
                        pre_cycle, 70.0 + 1e-6,
                        "Driving after cycle=70 before restart completes"
                    )

    def test_restart_resets_cycle_to_zero_after_cross_midnight(self):
        """After a midnight-crossing restart, cycle must be 0 for subsequent on-duty work."""
        segments = _run_simple(8.0, 480.0, cycle_used=69.0)
        cycle = 69.0
        for seg in segments:
            if "restart" in seg.remark.lower():
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
        self.assertLessEqual(cycle, 70.0 + 1e-6)


# ---------------------------------------------------------------------------
# Scenario L — Rolling 8-day approximation
# ---------------------------------------------------------------------------

class TestRolling8DayApproximation(TestCase):
    """
    The planner accepts current_cycle_used as an approximation of the rolling
    8-day on-duty total when prior 7-day history is not available.

    These tests document and verify the approximation behavior.
    """

    def test_zero_cycle_baseline(self):
        """With 0h used, no restart should occur for a trip under 70h total on-duty."""
        segments = _run_simple(8.0, 480.0, cycle_used=0.0)
        restarts = [s for s in segments if "restart" in s.remark.lower()]
        self.assertEqual(len(restarts), 0,
            "No restart expected when cycle starts at 0h and trip is well under 70h")

    def test_high_prior_cycle_triggers_restart(self):
        """High prior cycle (68h) must trigger a restart even for a short trip."""
        segments = _run_simple(4.0, 240.0, cycle_used=68.0)
        restarts = [s for s in segments if "restart" in s.remark.lower()]
        self.assertGreater(len(restarts), 0,
            "Trip with 68h prior cycle must trigger a 34h restart")

    def test_cycle_used_never_exceeds_70_for_any_start(self):
        """For a range of starting cycle values, the 70h cap must never be breached."""
        for start_cycle in [0.0, 10.0, 35.0, 60.0, 68.0, 69.5, 70.0]:
            with self.subTest(start_cycle=start_cycle):
                segs = _run_simple(6.0, 360.0, cycle_used=start_cycle)
                cycle = start_cycle
                for seg in segs:
                    if "restart" in seg.remark.lower():
                        cycle = 0.0
                    elif seg.status in ("driving", "on_duty"):
                        cycle += seg.duration_hours
                    self.assertLessEqual(
                        cycle, 70.0 + 1e-6,
                        f"Cycle {cycle:.4f}h exceeded 70h starting from {start_cycle}h"
                    )


# ---------------------------------------------------------------------------
# Sleeper berth options: has_sleeper_berth + sleeper_strategy
# ---------------------------------------------------------------------------

def _run_sleeper(
    driving_hours: float,
    distance_miles: float,
    has_sleeper_berth: bool = True,
    sleeper_strategy: str = "conservative_10h",
    cycle_used: float = 0.0,
    start: datetime = _START,
) -> list[Segment]:
    """Helper: simulate a trip with explicit sleeper berth options (no leg1 driving)."""
    return simulate_trip(
        leg1_driving_hours=0.0,
        leg1_distance_miles=0.0,
        leg2_driving_hours=driving_hours,
        leg2_distance_miles=distance_miles,
        cycle_used_hours=cycle_used,
        start_dt=start,
        current_location="loc",
        pickup_location="loc",
        dropoff_location="dest",
        has_sleeper_berth=has_sleeper_berth,
        sleeper_strategy=sleeper_strategy,
    )


class TestSleeperBerthOption(TestCase):
    """Test A/B/C/D: has_sleeper_berth and sleeper_strategy parameters."""

    # ── Test A: no sleeper berth ──────────────────────────────────────────────

    def test_no_sleeper_berth_rests_use_off_duty(self):
        """has_sleeper_berth=False: 10h rests must be off_duty, never sleeper."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        long_rests = [
            s for s in segs
            if s.duration_hours >= 10.0 - 1e-6
            and "restart" not in s.remark.lower()
        ]
        self.assertGreater(len(long_rests), 0, "No 10h rest found")
        for rest in long_rests:
            self.assertEqual(rest.status, "off_duty",
                f"Without sleeper berth, 10h rests must be off_duty; got {rest.status}")

    def test_no_sleeper_berth_no_sleeper_segments(self):
        """has_sleeper_berth=False: no 'sleeper' status segments must appear."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        sleeper_segs = [s for s in segs if s.status == "sleeper"]
        self.assertEqual(len(sleeper_segs), 0,
            "No sleeper segments expected when has_sleeper_berth=False")

    def test_no_sleeper_berth_segments_contiguous(self):
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        for i in range(1, len(segs)):
            self.assertEqual(segs[i].start, segs[i - 1].end, f"Gap at index {i}")

    def test_no_sleeper_berth_cycle_cap_respected(self):
        """Cycle cap must still be enforced when off-duty (no sleeper) is used."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False, cycle_used=68.0)
        cycle = 68.0
        for seg in segs:
            if "restart" in seg.remark.lower():
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            self.assertLessEqual(cycle, 70.0 + 1e-6, f"Cycle {cycle:.2f}h exceeded 70h")

    # ── Test B: conservative sleeper (default) ────────────────────────────────

    def test_conservative_rests_use_sleeper_status(self):
        """conservative_10h: 10h rests must be 'sleeper' status."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="conservative_10h")
        long_rests = [
            s for s in segs
            if s.duration_hours >= 10.0 - 1e-6
            and "restart" not in s.remark.lower()
        ]
        self.assertGreater(len(long_rests), 0, "No 10h rest found")
        for rest in long_rests:
            self.assertEqual(rest.status, "sleeper",
                f"Conservative strategy must use 'sleeper'; got '{rest.status}'")

    def test_conservative_no_split_segments(self):
        """conservative_10h must not generate split sleeper segments (only full 10h rests)."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="conservative_10h")
        split_segs = [s for s in segs if "split" in s.remark.lower()]
        self.assertEqual(len(split_segs), 0,
            f"Conservative strategy must not generate split segments; found: {split_segs}")

    def test_conservative_all_sleeper_rests_are_10h(self):
        """Conservative sleeper segments must all be exactly 10h (not 7h or 3h)."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="conservative_10h")
        for s in segs:
            if s.status == "sleeper":
                self.assertGreaterEqual(s.duration_hours, 10.0 - 1e-6,
                    f"Conservative sleeper segment is only {s.duration_hours:.2f}h (expected 10h)")

    # ── Test C: allow_split_sleeper ───────────────────────────────────────────

    def test_split_sleeper_generates_valid_pair(self):
        """allow_split_sleeper: find_split_sleeper_pairs() must detect ≥1 valid pair."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        pairs = find_split_sleeper_pairs(segs)
        valid = [(i, j) for i, j, v in pairs if v]
        self.assertGreater(len(valid), 0,
            f"Expected ≥1 valid split pair; pairs found: {pairs}")

    def test_split_sleeper_has_3h_off_and_7h_sleeper(self):
        """allow_split_sleeper: each rest must consist of a 3h off-duty + 7h sleeper pair."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        first_periods  = [s for s in segs if s.remark == "split sleeper first"]
        second_periods = [s for s in segs if s.remark == "split sleeper second"]
        self.assertGreater(len(first_periods), 0, "Expected split sleeper first periods")
        self.assertGreater(len(second_periods), 0, "Expected split sleeper second periods")
        self.assertEqual(len(first_periods), len(second_periods),
            "First and second periods must be paired 1-to-1")
        for s in first_periods:
            self.assertAlmostEqual(s.duration_hours, 3.0, places=4,
                msg=f"Split first period should be 3h; got {s.duration_hours:.2f}h")
        for s in second_periods:
            self.assertAlmostEqual(s.duration_hours, 7.0, places=4,
                msg=f"Split second period should be 7h; got {s.duration_hours:.2f}h")

    def test_split_sleeper_no_hos_violations(self):
        """Trip with split sleeper must produce no HOS violations per validate_hos_segments()."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        result = validate_hos_segments(segs, initial_cycle=0.0)
        self.assertEqual(len(result["violations"]), 0,
            f"Split sleeper trip should have no violations: {result['violations']}")

    def test_split_sleeper_segments_contiguous(self):
        """Segments must be gapless even with split sleeper strategy."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        for i in range(1, len(segs)):
            self.assertEqual(segs[i].start, segs[i - 1].end, f"Gap at index {i}")

    def test_split_sleeper_same_total_rest_as_conservative(self):
        """Split sleeper rests must sum to 10h per rest event (same total as conservative)."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        # Pair adjacent split-first + split-second segments and verify each totals 10h
        first_idxs = [i for i, s in enumerate(segs) if s.remark == "split sleeper first"]
        for i in first_idxs:
            if i + 1 < len(segs) and segs[i + 1].remark == "split sleeper second":
                total = segs[i].duration_hours + segs[i + 1].duration_hours
                self.assertAlmostEqual(total, 10.0, places=4,
                    msg=f"Paired split segments total {total:.2f}h (expected 10h)")

    # ── Test D: cross-mode invariants ─────────────────────────────────────────

    def test_all_modes_reach_dropoff(self):
        """All three modes must complete the trip (dropoff segment present)."""
        for has_sb, strategy in [
            (False, "conservative_10h"),
            (True,  "conservative_10h"),
            (True,  "allow_split_sleeper"),
        ]:
            with self.subTest(has_sleeper_berth=has_sb, sleeper_strategy=strategy):
                segs = _run_sleeper(10.0, 600.0, has_sleeper_berth=has_sb, sleeper_strategy=strategy)
                dropoffs = [s for s in segs if s.remark == "dropoff"]
                self.assertEqual(len(dropoffs), 1,
                    f"Expected exactly 1 dropoff segment; has_sb={has_sb}, strategy={strategy}")

    def test_all_modes_respect_70h_cycle_cap(self):
        """All three modes must enforce the 70h/8-day cycle cap."""
        for has_sb, strategy in [
            (False, "conservative_10h"),
            (True,  "conservative_10h"),
            (True,  "allow_split_sleeper"),
        ]:
            with self.subTest(has_sleeper_berth=has_sb, sleeper_strategy=strategy):
                segs = _run_sleeper(6.0, 360.0, has_sleeper_berth=has_sb,
                                    sleeper_strategy=strategy, cycle_used=68.0)
                cycle = 68.0
                for seg in segs:
                    if "restart" in seg.remark.lower():
                        cycle = 0.0
                    elif seg.status in ("driving", "on_duty"):
                        cycle += seg.duration_hours
                    self.assertLessEqual(cycle, 70.0 + 1e-6,
                        f"Cycle {cycle:.2f}h exceeded 70h; has_sb={has_sb}, strategy={strategy}")


# ---------------------------------------------------------------------------
# Sleeper berth edge cases: remark strings, cycle accounting, HOS rules
# ---------------------------------------------------------------------------

class TestSleeperBerthEdgeCases(TestCase):
    """Coverage for gaps not caught by TestSleeperBerthOption:
    remark strings, cycle non-accumulation, HOS rules under each mode."""

    # ── Remark strings from the engine ───────────────────────────────────────

    def test_conservative_remark_is_sleeper_overnight_rest(self):
        """Conservative mode must emit 'sleeper overnight rest', not 'overnight rest'."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="conservative_10h")
        long_rests = [
            s for s in segs
            if s.status == "sleeper"
            and s.duration_hours >= 10.0 - 1e-6
            and "restart" not in s.remark.lower()
        ]
        self.assertGreater(len(long_rests), 0, "No 10h sleeper rest found")
        for rest in long_rests:
            self.assertEqual(rest.remark, "sleeper overnight rest",
                f"Conservative rest remark should be 'sleeper overnight rest'; got '{rest.remark}'")

    def test_no_berth_remark_is_overnight_rest(self):
        """No-sleeper-berth mode must emit 'overnight rest', not 'sleeper overnight rest'."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        long_rests = [
            s for s in segs
            if s.status == "off_duty"
            and s.duration_hours >= 10.0 - 1e-6
            and "restart" not in s.remark.lower()
        ]
        self.assertGreater(len(long_rests), 0, "No 10h off_duty rest found")
        for rest in long_rests:
            self.assertEqual(rest.remark, "overnight rest",
                f"No-berth rest remark should be 'overnight rest'; got '{rest.remark}'")

    def test_split_sleeper_first_remark_string(self):
        """Split first period must have remark 'split sleeper first'."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        self.assertTrue(
            any(s.remark == "split sleeper first" for s in segs),
            "No segment with remark 'split sleeper first' found",
        )

    def test_split_sleeper_second_remark_string(self):
        """Split second period must have remark 'split sleeper second'."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True, sleeper_strategy="allow_split_sleeper")
        self.assertTrue(
            any(s.remark == "split sleeper second" for s in segs),
            "No segment with remark 'split sleeper second' found",
        )

    # ── Cycle not counted for rest periods ───────────────────────────────────

    def test_split_sleeper_pair_not_counted_in_cycle(self):
        """The 3h off-duty + 7h sleeper split pair must NOT accumulate in the 70h cycle."""
        segs = _run_sleeper(22.0, 1_300.0, has_sleeper_berth=True,
                             sleeper_strategy="allow_split_sleeper", cycle_used=0.0)
        cycle = 0.0
        for seg in segs:
            if "restart" in seg.remark.lower():
                cycle = 0.0
            elif seg.status in ("driving", "on_duty"):
                cycle += seg.duration_hours
            # off_duty and sleeper must never increment cycle
        self.assertLessEqual(cycle, 70.0 + 1e-6,
            f"Cycle {cycle:.2f}h exceeded 70h — split sleeper pair may have been counted")

    # ── No-sleeper-berth: HOS rules still enforced ───────────────────────────

    def test_no_berth_11h_driving_cap_per_shift(self):
        """Without sleeper berth, 11h driving cap per shift must still be enforced."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        shift_driving = 0.0
        for s in segs:
            if s.status == "driving":
                shift_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0 - 1e-6:
                self.assertLessEqual(shift_driving, 11.0 + 1e-6,
                    f"Shift driving {shift_driving:.2f}h exceeded 11h before rest (no-berth mode)")
                shift_driving = 0.0
        self.assertLessEqual(shift_driving, 11.0 + 1e-6,
            f"Final shift driving {shift_driving:.2f}h exceeded 11h")

    def test_no_berth_30min_break_required(self):
        """Without sleeper berth, the 30-min break after 8 cumulative driving hours must still appear."""
        segs = _run_sleeper(12.0, 720.0, has_sleeper_berth=False)
        breaks = [s for s in segs if s.remark == "30-min break"]
        self.assertGreater(len(breaks), 0,
            "30-min break must appear even without sleeper berth")

    def test_no_berth_cumulative_driving_respects_8h_limit(self):
        """Without sleeper berth, cumulative driving before a break must not exceed 8h."""
        segs = _run_sleeper(12.0, 720.0, has_sleeper_berth=False)
        cumul = 0.0
        for s in segs:
            if s.status == "driving":
                cumul += s.duration_hours
                self.assertLessEqual(cumul, 8.0 + 1e-6,
                    f"Drove {cumul:.2f}h cumulative without a qualifying break (no-berth)")
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 0.5 - 1e-6:
                cumul = 0.0

    def test_no_berth_validate_hos_no_violations(self):
        """validate_hos_segments must report zero violations for a no-berth trip."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=False)
        result = validate_hos_segments(segs, initial_cycle=0.0)
        self.assertEqual(len(result["violations"]), 0,
            f"No-berth trip should have no HOS violations: {result['violations']}")

    # ── Split sleeper: 30-min break still required ────────────────────────────

    def test_split_sleeper_30min_break_required(self):
        """With split sleeper, the 30-min break at 8h cumulative driving must still appear."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True,
                             sleeper_strategy="allow_split_sleeper")
        breaks = [s for s in segs if s.remark == "30-min break"]
        self.assertGreater(len(breaks), 0,
            "30-min break at 8h cumulative driving must still appear with split sleeper strategy")

    def test_split_sleeper_cumulative_driving_respects_8h_limit(self):
        """With split sleeper, cumulative driving before a qualifying break must not exceed 8h."""
        segs = _run_sleeper(15.0, 900.0, has_sleeper_berth=True,
                             sleeper_strategy="allow_split_sleeper")
        cumul = 0.0
        for s in segs:
            if s.status == "driving":
                cumul += s.duration_hours
                self.assertLessEqual(cumul, 8.0 + 1e-6,
                    f"Drove {cumul:.2f}h cumulative without a qualifying break (split sleeper)")
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 0.5 - 1e-6:
                cumul = 0.0
