# Spotter Trip Planner — Backend

Django + Django REST Framework API that plans truck trips and generates
FMCSA-style daily log data: given a current location, pickup, dropoff, and
hours already used in the driver's current cycle, it returns route geometry,
stops, and a day-by-day duty-status schedule.

## Stack

- Django 5 + Django REST Framework
- django-cors-headers
- OpenRouteService (routing/geocoding) — key required, see below
- SQLite (no external DB needed for this assessment)

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in OPENROUTESERVICE_API_KEY
python manage.py migrate
python manage.py runserver
```

API will be available at `http://localhost:8000/`.

## Environment variables

See `.env.example`. `OPENROUTESERVICE_API_KEY` is free — sign up at
https://openrouteservice.org/dev/#/signup.

## API

`POST /api/trips/`

Request body:

```json
{
  "current_location": "Chicago, IL",
  "pickup_location": "Indianapolis, IN",
  "dropoff_location": "Columbus, OH",
  "current_cycle_used": 12.5
}
```

Currently returns a stub response (route/stops/days empty) — the HOS engine
and OpenRouteService integration are next.

## Deployment

Targeting Render (free web service tier). `gunicorn` is already in
`requirements.txt`; Render's build command is `pip install -r
requirements.txt && python manage.py migrate`, start command `gunicorn
config.wsgi`.
