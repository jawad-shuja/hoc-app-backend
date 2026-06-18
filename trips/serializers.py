from rest_framework import serializers


class TripRequestSerializer(serializers.Serializer):
    """Input payload for POST /api/trips/.

    Locations are free-text (geocoded server-side). current_cycle_used is
    hours already used in the driver's current 70-hour/8-day cycle.
    """

    current_location = serializers.CharField(max_length=255)
    pickup_location = serializers.CharField(max_length=255)
    dropoff_location = serializers.CharField(max_length=255)
    current_cycle_used = serializers.FloatField(min_value=0, max_value=70)
    # Accept as raw string to preserve the timezone offset the client sends.
    # Parsed manually in the view so DRF doesn't silently convert to UTC.
    start_datetime = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)
