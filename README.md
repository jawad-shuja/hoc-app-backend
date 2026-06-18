# Spotter Trip Planner — Backend

Django + Django REST Framework API that plans truck trips and generates
FMCSA-style daily log data: given a current location, pickup, dropoff, and
hours already used in the driver's current cycle, it geocodes the locations
via OpenRouteService, calculates HGV routes, and runs the FMCSA HOS simulation
to return route geometry, stop markers, and a day-by-day duty-status schedule.

## Stack

- Django 5 + Django REST Framework
- django-cors-headers
- OpenRouteService (geocoding + HGV routing) — free key, no credit card
- SQLite (no external DB needed)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then fill in OPENROUTESERVICE_API_KEY
python manage.py migrate
python manage.py runserver
```

Server runs at `http://localhost:8000/`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | yes (prod) | Django secret key |
| `DEBUG` | no | `True` in dev, `False` in prod |
| `ALLOWED_HOSTS` | no | Comma-separated hostnames |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated frontend origins |
| `OPENROUTESERVICE_API_KEY` | **yes** | Free key from openrouteservice.org |

Sign up for a free ORS key at https://openrouteservice.org/dev/#/signup — no
credit card required.

---

## API

### `POST /api/trips/`

Plan an HOS-compliant trip for a property-carrying truck driver on the
70-hour/8-day cycle.

#### Request body

```json
{
  "current_location": "string (free-text city/address)",
  "pickup_location":  "string",
  "dropoff_location": "string",
  "current_cycle_used": 0.0
}
```

| Field | Type | Constraints | Description |
|---|---|---|---|
| `current_location` | string | max 255 chars | Where the driver is right now |
| `pickup_location` | string | max 255 chars | Pickup address |
| `dropoff_location` | string | max 255 chars | Dropoff address |
| `current_cycle_used` | float | 0 – 70 | Hours already used in the 70h/8-day cycle |

#### Response body

```json
{
  "route": {
    "geometry": [[lon, lat], ...],
    "distance_miles": 234.5,
    "duration_hours": 3.9,
    "legs": [
      { "from": "Chicago, IL", "to": "Indianapolis, IN", "distance_miles": 181.2, "duration_hours": 2.8 },
      { "from": "Indianapolis, IN", "to": "Columbus, OH",  "distance_miles": 175.3, "duration_hours": 2.7 }
    ]
  },
  "segments": [
    {
      "status": "on_duty",
      "start": "2026-06-18T08:00:00+00:00",
      "end":   "2026-06-18T09:00:00+00:00",
      "location": "pickup location",
      "remark": "pickup",
      "duration_hours": 1.0,
      "coords": [-86.158, 39.768]
    }
  ],
  "stops": [
    {
      "type": "pickup",
      "location": "pickup location",
      "coords": [-86.158, 39.768],
      "arrival":   "2026-06-18T08:00:00+00:00",
      "departure": "2026-06-18T09:00:00+00:00",
      "remark": "pickup",
      "duration_hours": 1.0
    }
  ],
  "days": [
    {
      "date": "2026-06-18",
      "off_duty": 0.0,
      "sleeper":  0.0,
      "driving":  5.5,
      "on_duty":  2.0
    }
  ]
}
```

**`segments`** — every duty-status interval in chronological order (pickup,
driving legs, breaks, fuel stops, overnight rests, dropoff). All four FMCSA
statuses appear: `off_duty`, `sleeper`, `driving`, `on_duty`. The `coords`
field is an `[lon, lat]` pair interpolated along the route polyline based on
how far into the total drive each segment begins.

**`stops`** — subset of segments suitable for map markers, with type
`pickup | dropoff | fuel | rest`. Pickup and dropoff use exact geocoded
coordinates; fuel and rest stops are interpolated along the polyline.

**`days`** — per-calendar-day totals in hours (segments spanning midnight are
split). All four status columns plus `date` (ISO date string). Column values
sum to the number of hours in that calendar day that the trip covers.

#### Error responses

| Status | When |
|---|---|
| `400 Bad Request` | Missing/invalid fields, or a location string could not be geocoded |
| `502 Bad Gateway` | OpenRouteService routing call failed |

---

## Testing with Postman

### 1. Import the request

1. Open Postman and click **New → HTTP Request**.
2. Set method to **POST**.
3. Set URL to `http://localhost:8000/api/trips/`.
4. Under the **Headers** tab add:
   - `Content-Type: application/json`
5. Under the **Body** tab, select **raw → JSON**.

### 2. Short trip (single day, no fuel stop)

```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Gary, IN",
  "dropoff_location": "Indianapolis, IN",
  "current_cycle_used": 0
}
```

Expected: `days` has 1 entry, no `fuel` stops, driving ≤ 8 h.

---

### 3. Medium trip (overnight rest, no fuel stop)

```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Indianapolis, IN",
  "dropoff_location": "Columbus, OH",
  "current_cycle_used": 10
}
```

Expected: `days` has 2 entries, one `rest` stop with `duration_hours: 10`.

---

### 4. Long trip (multi-day, fuel stop, 30-min break)

```json
{
  "current_location": "Los Angeles, CA",
  "pickup_location": "Phoenix, AZ",
  "dropoff_location": "Dallas, TX",
  "current_cycle_used": 0
}
```

Expected: `days` has 2–3 entries, at least one `fuel` stop in `stops`, at
least one `off_duty` segment with `remark: "30-min break"` in `segments`.

---

### 5. Cycle cap (34-hour restart)

```json
{
  "current_location": "Denver, CO",
  "pickup_location": "Kansas City, MO",
  "dropoff_location": "St. Louis, MO",
  "current_cycle_used": 69.5
}
```

Expected: `segments` contains an `off_duty` entry with `duration_hours: 34`
and `remark: "34h restart"`.

---

### 6. Validation errors

Missing field:
```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Indianapolis, IN",
  "current_cycle_used": 0
}
```
Expected: `400` with DRF validation errors.

Cycle used out of range:
```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Indianapolis, IN",
  "dropoff_location": "Columbus, OH",
  "current_cycle_used": 75
}
```
Expected: `400` — `current_cycle_used` max is 70.

---

## HOS rules enforced

| Rule | Value |
|---|---|
| Driving cap per shift | 11 h |
| On-duty window per shift | 14 h (clock-based from shift start) |
| Mandatory break | ≥ 30 min after every 8 cumulative driving hours |
| Rest between shifts | 10 h consecutive |
| Fuel stops | Every 1,000 miles |
| Pickup / dropoff | 1 h on-duty each |
| Cycle cap | 70 h / 8 days with 34 h restart |

---

## Running the tests

```bash
python manage.py test trips
```

36 unit tests covering `simulate_trip` (HOS rule enforcement, structural
invariants, three trip scenarios, cycle cap) and `compute_daily_totals`
(midnight splits, transcript-verified totals).

## Deployment

Targeting Render (free web service tier). Build command:

```
pip install -r requirements.txt && python manage.py migrate
```

Start command:

```
gunicorn config.wsgi
```
