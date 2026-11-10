from django.urls import path

from . import views

urlpatterns = [
    path('auth/login/', views.LoginView.as_view(), name='super-login'),
    path('auth/verify/', views.VerifyView.as_view(), name='super-verify'),
    path('auth/backup-codes/', views.BackupCodesView.as_view(), name='super-backup-codes'),
    path('me/', views.MeView.as_view(), name='super-me'),
    path('log/', views.LogView.as_view(), name='super-log'),
]
