from django.urls import path

from .views import RhythmMarkView, RhythmView

urlpatterns = [
    path('', RhythmView.as_view()),
    path('mark/', RhythmMarkView.as_view()),
]
