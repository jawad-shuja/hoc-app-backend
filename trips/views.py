from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import TripRequestSerializer


class TripPlanView(APIView):
    """POST /api/trips/

    Accepts the four trip inputs and will eventually return:
      - route: geometry + distance/duration for the map
      - stops: rest/fuel/overnight stops with location, time, reason
      - days: a list of per-24h duty-status segments for the log sheets

    Currently returns a placeholder payload so the frontend can be wired
    up against a stable shape before the HOS engine and routing calls
    are implemented.
    """

    def post(self, request):
        serializer = TripRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        return Response(
            {
                "input": data,
                "route": {
                    "geometry": [],
                    "distance_miles": 0,
                    "duration_hours": 0,
                },
                "stops": [],
                "days": [],
                "note": "stub response — HOS engine and routing not yet implemented",
            }
        )
