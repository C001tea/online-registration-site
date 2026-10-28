from django.urls import path
from rest_framework.routers import SimpleRouter

from notifications.views import StaffTelegramView

from . import views

# SimpleRouter — без корневой страницы API (она была бы доступна без прав)
router = SimpleRouter()
router.register('appointments', views.AppointmentViewSet, basename='staff-appointments')
router.register('categories', views.CategoryViewSet, basename='staff-categories')
router.register('services', views.ServiceViewSet, basename='staff-services')
router.register('masters', views.MasterViewSet, basename='staff-masters')

urlpatterns = [
    path('auth/login/', views.StaffLoginView.as_view(), name='staff-login'),
    path('auth/refresh/', views.StaffTokenRefreshView.as_view(), name='staff-refresh'),
    path('auth/password/', views.StaffPasswordView.as_view(), name='staff-password'),
    path('me/', views.StaffMeView.as_view(), name='staff-me'),
    path('free-times/', views.FreeTimesView.as_view(), name='staff-free-times'),
    path('clients/lookup/', views.ClientLookupView.as_view(), name='staff-client-lookup'),
    path('days-off/', views.DaysOffListView.as_view(), name='staff-days-off'),
    path('company/', views.CompanyView.as_view(), name='staff-company'),
    path('telegram/', StaffTelegramView.as_view(), name='staff-telegram'),
] + router.urls
