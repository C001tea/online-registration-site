from django.urls import path

from .views import MasterWorksView, VisitPhotoView, VisitWorkView, WorkPhotoView

urlpatterns = [
    path('visit/<int:pk>/', VisitWorkView.as_view()),
    path('visit/<int:pk>/photos/', VisitPhotoView.as_view()),
    path('master/<int:pk>/', MasterWorksView.as_view()),
    path('<int:pk>/', WorkPhotoView.as_view()),
]
