from django.urls import path, re_path

from . import health, salons, views

urlpatterns = [
    path('auth/login/', views.LoginView.as_view(), name='super-login'),
    path('auth/verify/', views.VerifyView.as_view(), name='super-verify'),
    path('auth/backup-codes/', views.BackupCodesView.as_view(), name='super-backup-codes'),
    path('me/', views.MeView.as_view(), name='super-me'),
    path('log/', views.LogView.as_view(), name='super-log'),
    path('health/', health.HealthView.as_view(), name='super-health'),
    path('overview/', salons.OverviewView.as_view(), name='super-overview'),
    path('salons/', salons.SalonListView.as_view(), name='super-salons'),
    path('salons/<int:pk>/', salons.SalonDetailView.as_view(), name='super-salon'),
    path('salons/<int:pk>/impersonate/', salons.ImpersonateView.as_view(), name='super-impersonate'),
    re_path(r'^salons/(?P<pk>\d+)/(?P<action>status|trial|resend-verification|password-reset|note)/$',
            salons.SalonActionView.as_view(), name='super-salon-action'),
]
