"""
Unit tests for the HOS (Hours of Service) engine.

All tests for simulate_trip() and compute_daily_totals() live here.
The HOS rules enforced:
  - 11h driving cap per on-duty shift
  - 14h on-duty window per shift (driving + on_duty combined)
  - ≥30-min break required after 8 cumulative driving hours
  - 10h consecutive rest between shifts
  - Fuel stop at least every 1,000 miles
  - 1h on-duty block for pickup, 1h for dropoff
  - 70h/8-day cycle cap with 34h restart
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase

from .hos_engine import Segment, compute_daily_totals, simulate_trip

UTC = ZoneInfo("UTC")

# Fixed reference datetime used across all simulate_trip tests.
_START = datetime(2026, 1, 6, 8, 0, tzinfo=UTC)  # Tuesday 08:00 UTC


def _dt(hours_offset: float, base: datetime = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) -> datetime:
    """Return base + hours_offset as a datetime."""
    return base + timedelta(hours=hours_offset)


def _driving_hours(segments: list[Segment]) -> float:
    return sum(s.duration_hours for s in segments if s.status == "driving")


def _on_duty_window_hours(segments: list[Segment], shift_start: datetime) -> float:
    """Hours between shift_start and the end of the last driving or on_duty segment."""
    active = [s for s in segments if s.status in ("driving", "on_duty") and s.start >= shift_start]
    if not active:
        return 0.0
    return (active[-1].end - shift_start).total_seconds() / 3600


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
        self.assertAlmostEqual(totals[0]["sleeper"], 2.0, places=4)  # 22:00-24:00
        self.assertAlmostEqual(totals[1]["sleeper"], 8.0, places=4)  # 00:00-08:00

    def test_empty_segments_returns_empty_list(self):
        self.assertEqual(compute_daily_totals([]), [])

    def test_two_day_trip_produces_two_entries(self):
        """A driving segment on each of two calendar days gives two entries."""
        segments = [
            Segment("driving", _dt(6.0),  _dt(17.0), "en route", "driving"),  # day 1
            Segment("off_duty", _dt(17.0), _dt(27.0), "Motel",   "rest"),      # spans midnight
            Segment("driving", _dt(27.0), _dt(33.0), "en route", "driving"),  # day 2
        ]
        totals = compute_daily_totals(segments)
        self.assertEqual(len(totals), 2)


# ---------------------------------------------------------------------------
# Structural invariants on simulate_trip output
# ---------------------------------------------------------------------------

class TestSimulateTripStructure(TestCase):
    """simulate_trip() output must satisfy basic structural properties."""

    def _run(self, driving_hours=6.0, distance_miles=360.0, cycle_used=0.0):
        return simulate_trip(
            driving_hours=driving_hours,
            distance_miles=distance_miles,
            cycle_used_hours=cycle_used,
            start_dt=_START,
        )

    def test_returns_nonempty_list(self):
        self.assertGreater(len(self._run()), 0)

    def test_segments_are_contiguous(self):
        segments = self._run()
        for i in range(1, len(segments)):
            self.assertEqual(
                segments[i].start,
                segments[i - 1].end,
                f"Gap between segment {i-1} ({segments[i-1].status}) "
                f"and segment {i} ({segments[i].status})",
            )

    def test_first_segment_starts_at_start_dt(self):
        segments = self._run()
        self.assertEqual(segments[0].start, _START)

    def test_no_zero_duration_segment(self):
        segments = self._run()
        for s in segments:
            self.assertGreater(
                (s.end - s.start).total_seconds(), 0,
                f"Zero-duration segment: {s}",
            )

    def test_all_statuses_are_valid(self):
        valid = {"off_duty", "sleeper", "driving", "on_duty"}
        for s in self._run():
            self.assertIn(s.status, valid, f"Unknown status: {s.status}")

    def test_trip_contains_driving_segments(self):
        self.assertTrue(any(s.status == "driving" for s in self._run()))

    def test_trip_contains_on_duty_segments(self):
        # pickup + dropoff must appear as on_duty
        self.assertTrue(any(s.status == "on_duty" for s in self._run()))


# ---------------------------------------------------------------------------
# HOS rule enforcement
# ---------------------------------------------------------------------------

class TestHOSRules(TestCase):
    """Each FMCSA rule is verified independently."""

    def test_driving_cap_11h_per_shift(self):
        """No shift may have more than 11 hours of driving."""
        segments = simulate_trip(
            driving_hours=15.0,
            distance_miles=900.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        # Walk segments and accumulate driving within each shift.
        shift_driving = 0.0
        for s in segments:
            if s.status == "driving":
                shift_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0:
                # A qualifying rest resets the shift.
                self.assertLessEqual(
                    shift_driving, 11.0 + 1e-6,
                    "Shift driving exceeded 11h before rest",
                )
                shift_driving = 0.0
        self.assertLessEqual(shift_driving, 11.0 + 1e-6, "Final shift driving exceeded 11h")

    def test_14h_on_duty_window_per_shift(self):
        """Driving + on_duty time within a shift window must not exceed 14 hours."""
        segments = simulate_trip(
            driving_hours=13.0,
            distance_miles=780.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        # Identify shift boundaries (≥10h rest) and check each window.
        shift_start = segments[0].start
        window_hours = 0.0
        for s in segments:
            if s.status in ("driving", "on_duty"):
                window_hours = (s.end - shift_start).total_seconds() / 3600
                self.assertLessEqual(
                    window_hours, 14.0 + 1e-6,
                    f"On-duty window reached {window_hours:.2f}h (max 14h)",
                )
            elif s.status in ("off_duty", "sleeper") and s.duration_hours >= 10.0:
                shift_start = s.end
                window_hours = 0.0

    def test_30min_break_required_after_8h_driving(self):
        """After 8 cumulative driving hours, a ≥30-min break must precede more driving."""
        segments = simulate_trip(
            driving_hours=10.0,
            distance_miles=600.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        cumulative_driving = 0.0
        for s in segments:
            if s.status == "driving":
                cumulative_driving += s.duration_hours
            elif s.status in ("off_duty", "sleeper"):
                if s.duration_hours >= 0.5:
                    cumulative_driving = 0.0  # qualifying break resets the counter
                # If break is shorter than 30 min and cumulative > 8h, that's a violation.
                if s.duration_hours < 0.5 - 1e-6 and cumulative_driving > 8.0 + 1e-6:
                    self.fail(
                        f"Driving resumed after {cumulative_driving:.2f}h with only a "
                        f"{s.duration_hours * 60:.0f}-min break (need ≥30 min)"
                    )

    def test_break_inserted_before_exceeding_8h_driving(self):
        """For a 10h drive, a break must appear before 8h of continuous driving elapses."""
        segments = simulate_trip(
            driving_hours=10.0,
            distance_miles=600.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
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
        segments = simulate_trip(
            driving_hours=15.0,
            distance_miles=900.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        totals = compute_daily_totals(segments)
        if len(totals) < 2:
            return  # Single-day trip — no between-shift rest to check.

        # Find contiguous rest blocks (off_duty or sleeper) that exceed 30 min
        # but are less than 34h (34h rests are restarts, not regular overnight).
        for s in segments:
            if s.status in ("off_duty", "sleeper"):
                h = s.duration_hours
                if 1.0 < h < 34.0:
                    self.assertGreaterEqual(
                        h, 10.0 - 1e-6,
                        f"Between-shift rest is only {h:.2f}h (need ≥10h)",
                    )

    def test_fuel_stop_every_1000_miles(self):
        """A trip over 1,000 miles must include at least one fuel stop."""
        segments = simulate_trip(
            driving_hours=20.0,
            distance_miles=1_200.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreater(len(fuel_stops), 0, "No fuel stop found for 1,200-mile trip")

    def test_two_fuel_stops_for_2000_plus_miles(self):
        """A 2,200-mile trip must have at least two fuel stops."""
        segments = simulate_trip(
            driving_hours=37.0,
            distance_miles=2_200.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreaterEqual(len(fuel_stops), 2, "Expected ≥2 fuel stops for 2,200-mile trip")

    def test_no_fuel_stop_under_1000_miles(self):
        """A 900-mile trip should not produce a fuel stop."""
        segments = simulate_trip(
            driving_hours=15.0,
            distance_miles=900.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        fuel_stops = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel_stops), 0, "Unexpected fuel stop for 900-mile trip")

    def test_pickup_block_is_1h_on_duty(self):
        """Pickup must appear as a 1h on_duty segment."""
        segments = simulate_trip(
            driving_hours=5.0,
            distance_miles=300.0,
            cycle_used_hours=0.0,
            start_dt=_START,
            pickup_duration_hours=1.0,
            dropoff_duration_hours=1.0,
        )
        on_duty = [s for s in segments if s.status == "on_duty"]
        durations = [s.duration_hours for s in on_duty]
        # At least one on_duty segment must be exactly 1h (pickup).
        self.assertTrue(
            any(abs(d - 1.0) < 1e-6 for d in durations),
            f"No 1h on_duty block found; on_duty durations: {durations}",
        )

    def test_dropoff_block_is_1h_on_duty(self):
        """Dropoff must appear as a 1h on_duty segment."""
        segments = simulate_trip(
            driving_hours=5.0,
            distance_miles=300.0,
            cycle_used_hours=0.0,
            start_dt=_START,
            pickup_duration_hours=1.0,
            dropoff_duration_hours=1.0,
        )
        on_duty = [s for s in segments if s.status == "on_duty"]
        # There must be at least two 1h on_duty blocks (pickup + dropoff).
        one_hour_blocks = [s for s in on_duty if abs(s.duration_hours - 1.0) < 1e-6]
        self.assertGreaterEqual(
            len(one_hour_blocks), 2,
            "Expected at least two 1h on_duty blocks (pickup + dropoff)",
        )

    def test_total_on_duty_at_least_2h(self):
        """Total on_duty time must be ≥ 2h (1h pickup + 1h dropoff)."""
        segments = simulate_trip(
            driving_hours=6.0,
            distance_miles=360.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        total = sum(s.duration_hours for s in segments if s.status == "on_duty")
        self.assertGreaterEqual(total, 2.0 - 1e-6)


# ---------------------------------------------------------------------------
# 70h/8-day cycle cap and 34h restart
# ---------------------------------------------------------------------------

class TestCycleCap(TestCase):

    def test_cycle_cap_triggers_34h_restart(self):
        """With 69h already used, a trip needing more on-duty time must restart."""
        segments = simulate_trip(
            driving_hours=4.0,
            distance_miles=240.0,
            cycle_used_hours=69.0,
            start_dt=_START,
        )
        long_rests = [
            s for s in segments
            if s.status in ("off_duty", "sleeper")
            and s.duration_hours >= 34.0 - 1e-6
        ]
        self.assertGreater(len(long_rests), 0, "Expected a 34h restart when cycle is nearly full")

    def test_restart_is_at_least_34h(self):
        """The restart period must be ≥ 34 consecutive hours."""
        segments = simulate_trip(
            driving_hours=4.0,
            distance_miles=240.0,
            cycle_used_hours=69.0,
            start_dt=_START,
        )
        for s in segments:
            if s.status in ("off_duty", "sleeper") and s.duration_hours >= 34.0 - 1e-6:
                self.assertGreaterEqual(s.duration_hours, 34.0 - 1e-6)
                return
        self.fail("No ≥34h restart segment found")

    def test_zero_cycle_used_short_trip_no_restart(self):
        """With 0h used, a short trip should not need a 34h restart."""
        segments = simulate_trip(
            driving_hours=5.0,
            distance_miles=300.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        long_rests = [
            s for s in segments
            if s.status in ("off_duty", "sleeper")
            and s.duration_hours >= 34.0 - 1e-6
        ]
        self.assertEqual(len(long_rests), 0, "Short trip with 0 cycle hours should not restart")

    def test_full_cycle_immediately_triggers_restart(self):
        """A driver at exactly 70h used cannot drive at all without a restart first."""
        segments = simulate_trip(
            driving_hours=3.0,
            distance_miles=180.0,
            cycle_used_hours=70.0,
            start_dt=_START,
        )
        # First active segment should be a rest, not driving or on_duty.
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


# ---------------------------------------------------------------------------
# End-to-end trip scenarios
# ---------------------------------------------------------------------------

class TestTripScenarios(TestCase):
    """Three representative scenarios from the execution plan."""

    def test_short_trip_single_day(self):
        """
        Short trip: 2h driving, 120 miles.
        Should complete in one calendar day with no overnight rest and no fuel stop.
        """
        segments = simulate_trip(
            driving_hours=2.0,
            distance_miles=120.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        totals = compute_daily_totals(segments)
        self.assertEqual(len(totals), 1, "Short trip should fit in one calendar day")
        self.assertEqual(totals[0]["driving"], 2.0)
        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel), 0)

    def test_medium_trip_one_overnight(self):
        """
        Medium trip: 8h driving, 480 miles.
        Needs one 10h overnight rest; no fuel stop; no mandatory break (≤8h driving).
        """
        segments = simulate_trip(
            driving_hours=8.0,
            distance_miles=480.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        # Exactly 8h driving total, no break needed (limit is >8h).
        total_driving = _driving_hours(segments)
        self.assertAlmostEqual(total_driving, 8.0, places=4)
        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertEqual(len(fuel), 0)

    def test_long_multi_day_trip(self):
        """
        Long trip: 20h driving, 1,200 miles.
        Must span multiple days, include fuel stop(s), and enforce 11h cap per shift.
        """
        segments = simulate_trip(
            driving_hours=20.0,
            distance_miles=1_200.0,
            cycle_used_hours=0.0,
            start_dt=_START,
        )
        totals = compute_daily_totals(segments)
        self.assertGreater(len(totals), 1, "Long trip must span multiple calendar days")

        fuel = [s for s in segments if "fuel" in s.remark.lower()]
        self.assertGreater(len(fuel), 0, "1,200-mile trip must have fuel stop(s)")

        for day in totals:
            self.assertLessEqual(day["driving"], 11.0 + 1e-6, "Daily driving cap exceeded")

    def test_total_driving_hours_preserved(self):
        """The sum of all driving segments must equal the input driving_hours."""
        for hours, miles in [(3.0, 180.0), (8.0, 480.0), (11.0, 660.0)]:
            with self.subTest(driving_hours=hours):
                segments = simulate_trip(
                    driving_hours=hours,
                    distance_miles=miles,
                    cycle_used_hours=0.0,
                    start_dt=_START,
                )
                total = _driving_hours(segments)
                self.assertAlmostEqual(
                    total, hours, places=4,
                    msg=f"Expected {hours}h driving, got {total:.4f}h",
                )
