from django.urls import path

from .views import TripPlanView

urlpatterns = [
    path("trips/", TripPlanView.as_view(), name="trip-plan"),
]
