from django.urls import include, path, re_path
from rest_framework.routers import SimpleRouter

from finance.views import CheckoutView
from notifications.views import StaffTelegramView

from . import clients, views

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
    path('clients/', clients.ClientListView.as_view(), name='staff-clients'),
    path('clients/card/', clients.ClientCardView.as_view(), name='staff-client-card'),
    path('clients/note/', clients.ClientNoteView.as_view(), name='staff-client-note'),
    path('days-off/', views.DaysOffListView.as_view(), name='staff-days-off'),
    path('work-days/', views.WorkDaysListView.as_view(), name='staff-work-days'),
    path('company/', views.CompanyView.as_view(), name='staff-company'),
    re_path(r'^company/(?P<kind>logo|main-photo)/$', views.CompanyPhotoView.as_view(), name='staff-company-photo'),
    path('telegram/', StaffTelegramView.as_view(), name='staff-telegram'),
    path('appointments/<int:pk>/checkout/', CheckoutView.as_view(), name='staff-checkout'),
    path('finance/', include('finance.urls')),
] + router.urls
