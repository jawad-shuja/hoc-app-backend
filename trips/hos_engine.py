from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Segment:
    """One duty-status interval in a driver's log."""
    status: str   # "off_duty" | "sleeper" | "driving" | "on_duty"
    start: datetime
    end: datetime
    location: str
    remark: str

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

    Args:
        driving_hours: Pure driving time (from routing API), excluding all stops.
        distance_miles: Total route distance, used to place fuel stops.
        cycle_used_hours: Hours already consumed in the driver's 70h/8-day cycle.
        start_dt: Datetime when the driver begins the first on-duty period.
        pickup_duration_hours: On-duty (not driving) time for pickup.
        dropoff_duration_hours: On-duty (not driving) time for dropoff.

    Returns:
        Contiguous list of Segment objects from start_dt through end of final rest.
        Segments cover: pickup on-duty, driving legs, mandatory 30-min break after
        8h driving, fuel stops every 1,000 miles, 10h overnight rests, a 34h restart
        if the 70h cycle cap is hit, and the dropoff on-duty block.
    """
    raise NotImplementedError


def compute_daily_totals(segments: list[Segment]) -> list[dict]:
    """Aggregate duty-status hours per calendar day (UTC).

    Segments that span midnight are split at the day boundary so each day's
    totals are independent.

    Returns:
        List of dicts ordered by date, each with keys:
        "date" (datetime.date), "off_duty", "sleeper", "driving", "on_duty"
        (all floats in hours).  Days with no segments are omitted.
    """
    raise NotImplementedError
