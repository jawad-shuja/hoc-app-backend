# Spotter Trip Planner — Backend

Django + Django REST Framework API that plans FMCSA-compliant truck trips. Given a current location, pickup, dropoff, and hours already used in the driver's 70-hour cycle, it geocodes the locations via OpenRouteService, fetches HGV routes, runs the HOS simulation, and returns route geometry, stop markers, a duty-status segment list, per-day log totals, and a trip summary.

## Stack

- Python 3.10 · Django 5.2 · Django REST Framework
- django-cors-headers
- OpenRouteService (free geocoding + HGV routing — no credit card required)
- SQLite (no external DB needed; Render uses the ephemeral filesystem)
- Gunicorn + Whitenoise for production

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in OPENROUTESERVICE_API_KEY
python manage.py migrate
python manage.py runserver
```

Server listens at `http://localhost:8000/`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes (prod) | Django secret key — auto-generated on Render |
| `DEBUG` | no | `True` in dev, `False` in prod |
| `ALLOWED_HOSTS` | no | Comma-separated hostnames (Render injects its own automatically) |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated frontend origins, e.g. `https://your-app.vercel.app` |
| `OPENROUTESERVICE_API_KEY` | **yes** | Free key from [openrouteservice.org](https://openrouteservice.org/dev/#/signup) |

---

## API reference

### `POST /api/trips/`

Plan an HOS-compliant trip for a property-carrying CMV driver on the 70-hour/8-day cycle.

#### Request body

```json
{
  "current_location":   "Chicago, IL",
  "pickup_location":    "Indianapolis, IN",
  "dropoff_location":   "Columbus, OH",
  "current_cycle_used": 10.0,
  "start_datetime":     "2026-06-19T08:00:00-05:00",
  "has_sleeper_berth":  true,
  "sleeper_strategy":   "conservative_10h"
}
```

| Field | Type | Constraints | Description |
|---|---|---|---|
| `current_location` | string | max 255 chars | Where the driver is right now (free-text, geocoded server-side) |
| `pickup_location` | string | max 255 chars | Pickup address or city |
| `dropoff_location` | string | max 255 chars | Dropoff address or city |
| `current_cycle_used` | float | 0 – 70 | Hours already used in the rolling 70h/8-day cycle |
| `start_datetime` | ISO 8601 string | optional | Trip departure time with UTC offset (e.g. `"2026-06-19T08:00:00-05:00"`). Send the driver's **local** offset so log times appear in local time. Defaults to the next round UTC hour if omitted. |
| `has_sleeper_berth` | boolean | optional, default `true` | If `false`, overnight rests are logged as Off Duty instead of Sleeper Berth. |
| `sleeper_strategy` | `"conservative_10h"` \| `"allow_split_sleeper"` | optional, default `"conservative_10h"` | Only relevant when `has_sleeper_berth=true`. `allow_split_sleeper` generates a 3h off-duty + 7h sleeper split pair (valid under FMCSA §395.1(g)(1)(i)) instead of a full 10h rest. |

#### Response body

```json
{
  "trip_start":           "2026-06-19T08:00:00-05:00",
  "trip_start_assumed":   false,
  "trip_timezone_offset": "-05:00",
  "route": {
    "geometry":       [[lon, lat], "..."],
    "distance_miles": 356.4,
    "duration_hours": 5.8,
    "legs": [
      { "from": "Chicago, IL",    "to": "Indianapolis, IN", "distance_miles": 181.2, "duration_hours": 2.9 },
      { "from": "Indianapolis, IN", "to": "Columbus, OH",   "distance_miles": 175.3, "duration_hours": 2.9 }
    ]
  },
  "segments": [
    {
      "status":         "on_duty",
      "start":          "2026-06-19T08:00:00-05:00",
      "end":            "2026-06-19T09:00:00-05:00",
      "location":       "Indianapolis, IN",
      "remark":         "Pickup – Indianapolis, IN",
      "duration_hours": 1.0,
      "distance_miles": 0.0,
      "coords":         [-86.158, 39.768]
    }
  ],
  "stops": [
    {
      "type":           "pickup",
      "location":       "Indianapolis, IN",
      "coords":         [-86.158, 39.768],
      "arrival":        "2026-06-19T08:00:00-05:00",
      "departure":      "2026-06-19T09:00:00-05:00",
      "remark":         "Pickup – Indianapolis, IN",
      "duration_hours": 1.0
    }
  ],
  "days": [
    {
      "date":             "2026-06-19",
      "off_duty":         0.0,
      "sleeper":          0.0,
      "driving":          5.75,
      "on_duty":          2.0,
      "from_location":    "Chicago, IL",
      "to_location":      "Columbus, OH",
      "miles_today":      356.4,
      "cycle_used_start": 10.0,
      "after_restart":    false
    }
  ],
  "summary": {
    "total_miles":         356.4,
    "total_driving_hours": 5.75,
    "total_on_duty_hours": 7.75,
    "num_fuel_stops":      0,
    "num_rest_breaks":     0,
    "num_overnight_rests": 0,
    "restart_planned":     false,
    "num_days":            1
  }
}
```

**`trip_timezone_offset`** — UTC offset derived from `start_datetime` (e.g. `"-05:00"`). All segment timestamps and day-boundary splits use this offset so log times appear in the driver's local time.

**`segments`** — every duty-status interval in chronological order. `status` is one of `off_duty | sleeper | driving | on_duty`. `coords` is `[lon, lat]` interpolated along the route polyline. Driving segments have an empty `location` string.

**`stops`** — map-marker subset of segments. `type` is one of `pickup | dropoff | fuel | rest`. Pickup/dropoff use exact geocoded coordinates; others are interpolated.

**`days`** — per-calendar-day totals. `date` is the driver's local calendar date. `from_location` / `to_location` are the driver's position at local midnight and 23:59. `cycle_used_start` is the rolling 70h total at the start of that day's first active duty period. Sums of `off_duty + sleeper + driving + on_duty` equal the hours in that local day covered by the trip.

**`summary`** — trip-level aggregates for UI display. `restart_planned` is `true` when the planner scheduled a 34-hour off-duty period because no prior 7-day duty history was provided and natural rolling-hour recovery could not be calculated. FMCSA does not mandate a 34-hour restart — it is one valid option a driver may choose to reset the cycle.

#### Error responses

| Status | When |
|---|---|
| `400 Bad Request` | Missing/invalid fields, or a location string could not be geocoded |
| `502 Bad Gateway` | OpenRouteService routing call failed |

---

## Test scenarios (Postman)

### Short trip — single day, no stops

```json
{
  "current_location":   "Chicago, IL",
  "pickup_location":    "Gary, IN",
  "dropoff_location":   "Indianapolis, IN",
  "current_cycle_used": 0
}
```

Expected: 1 day entry, no fuel stops, total driving ≤ 8 h.

### Medium trip — overnight rest required

```json
{
  "current_location":   "Chicago, IL",
  "pickup_location":    "Indianapolis, IN",
  "dropoff_location":   "Columbus, OH",
  "current_cycle_used": 10
}
```

Expected: 2 day entries, one `rest` stop with `duration_hours` 10.

### Long trip — multi-day, fuel stop, 30-min break

```json
{
  "current_location":   "Los Angeles, CA",
  "pickup_location":    "Phoenix, AZ",
  "dropoff_location":   "Dallas, TX",
  "current_cycle_used": 0
}
```

Expected: 2–3 day entries, at least one `fuel` stop, at least one segment with `"30-minute break"` in `remark`.

### Cycle cap — 34-hour restart

```json
{
  "current_location":   "Denver, CO",
  "pickup_location":    "Kansas City, MO",
  "dropoff_location":   "St. Louis, MO",
  "current_cycle_used": 69.5
}
```

Expected: a segment with `status: "off_duty"`, `duration_hours: 34`, and `"34-hour restart"` in `remark`. `summary.restart_planned` will be `true`. Note: FMCSA does not require a 34-hour restart — the planner selects one here as a conservative fallback because prior 7-day duty history was not provided and natural rolling-hour recovery cannot be calculated.

### Split sleeper

```json
{
  "current_location":   "Chicago, IL",
  "pickup_location":    "Indianapolis, IN",
  "dropoff_location":   "Nashville, TN",
  "current_cycle_used": 0,
  "has_sleeper_berth":  true,
  "sleeper_strategy":   "allow_split_sleeper"
}
```

Expected: overnight rest split into a `sleeper` segment (7 h) and `off_duty` segment (3 h).

### Validation errors

```json
{ "current_location": "Chicago, IL", "pickup_location": "Indianapolis, IN", "current_cycle_used": 0 }
```
Expected: `400` — `dropoff_location` is required.

```json
{ "current_location": "Chicago, IL", "pickup_location": "Indianapolis, IN", "dropoff_location": "Columbus, OH", "current_cycle_used": 75 }
```
Expected: `400` — `current_cycle_used` max is 70.

---

## HOS rules enforced

| Rule | Value |
|---|---|
| Driving cap per shift | 11 h |
| On-duty window per shift | 14 h (from shift start) |
| Mandatory break | ≥ 30 min after 8 cumulative driving hours |
| Rest between shifts | 10 h consecutive off-duty or sleeper |
| Fuel stops | Every 1,000 miles (planned windows; no real station lookup) |
| Pickup / dropoff duty | 1 h on-duty each |
| Cycle cap | 70 h / 8 days |
| Restart | 34 h off-duty resets cycle to 0 |
| Sleeper split | 7 h sleeper + 3 h off-duty valid pair (§395.1(g)(1)(i)) |

## FMCSA coverage matrix

| Rule | Status | Notes |
|---|---|---|
| 11-hour driving limit | Implemented | Hard cap per duty period |
| 14-hour on-duty window | Implemented | Clock-based from shift start |
| 30-minute break | Implemented | After 8 cumulative driving hours |
| Sleeper berth — full 10h | Implemented | Selectable via `has_sleeper_berth` |
| Sleeper berth — split pair | Implemented | `allow_split_sleeper` strategy |
| 70-hour/8-day cycle | Implemented | Hard cap with 34h restart |
| 34-hour restart | Implemented | Inserted before cycle violation; resets to 0 |
| Driver's log / ELD | Implemented | SVG log sheet: 24h grid, 15-min increments, 4 rows, remarks, totals |
| Adverse driving conditions | Not modeled | Extension not selectable |
| Short-haul exemption | Not modeled | Out of scope |
| Personal conveyance / yard moves | Not modeled | Not generated; not classified |
| Passenger-carrying rules | Not modeled | Out of scope |
| Hazmat-specific rules | Not modeled | Out of scope |
| Verified fuel stations | Not modeled | Planned windows every 1,000 mi, not real lookups |

---

## Running tests

```bash
# 92 unit tests: HOS engine, daily totals, phase ordering, cycle cap, split sleeper
python manage.py test trips

# 11 deterministic scenario tests (no live API calls)
python manage.py validate_hos_scenarios
```

---

## Deployment (Render)

A `render.yaml` is included. Set three environment variables in the Render dashboard:

| Variable | Value |
|---|---|
| `ALLOWED_HOSTS` | `your-app.onrender.com` |
| `CORS_ALLOWED_ORIGINS` | `https://your-frontend.vercel.app` |
| `OPENROUTESERVICE_API_KEY` | Your ORS key |

`SECRET_KEY` is auto-generated by Render via `generateValue: true`.

> **Cold-start note (Render free tier):** Free services spin down after ~15 minutes of inactivity. The first request after idle can take 20–60 seconds while the instance boots. Subsequent requests in the session are faster.
