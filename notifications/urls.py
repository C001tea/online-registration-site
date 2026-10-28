from django.urls import path

from . import views

urlpatterns = [
    path('webhook/', views.TelegramWebhookView.as_view(), name='telegram-webhook'),
]
