from django.urls import path

from .views import ReviewsView

urlpatterns = [
    path('', ReviewsView.as_view()),
]
