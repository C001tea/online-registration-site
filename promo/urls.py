from django.urls import path

from .views import MasterLinkView, PromoView, WindowsView

urlpatterns = [
    path('', PromoView.as_view()),
    path('master/<int:pk>/', MasterLinkView.as_view()),
    path('windows/', WindowsView.as_view()),
]
