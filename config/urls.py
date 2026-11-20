"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import path, include

from bookings.auth_views import ClientTokenRefreshView
from bookings.security import limit_admin_login
from notifications.views import TelegramLoginStartView, TelegramLoginVerifyView
from feedback.public import review_page
from retention.public import rhythm_opt_out

# Вход во встроенную админку Django — с тем же лимитом перебора, что и вход в панель
admin.site.login = limit_admin_login(admin.site.login)

urlpatterns = [
    path(f'{settings.DJANGO_ADMIN_PATH}/', admin.site.urls),
    path('api/auth/refresh/', ClientTokenRefreshView.as_view(), name='client-refresh'),
    path('api/auth/telegram/start/', TelegramLoginStartView.as_view(), name='telegram-login-start'),
    path('api/auth/telegram/verify/', TelegramLoginVerifyView.as_view(), name='telegram-login-verify'),
    # Отписка от напоминаний «пора записаться» по ссылке из письма
    path('api/rhythm/off/<str:token>/', rhythm_opt_out, name='rhythm-opt-out'),
    # Оценка визита по ссылке из письма
    path('api/review/<str:token>/', review_page, name='visit-review'),
    path('api/', include('bookings.urls')),
    # API панели салона (персонал: владелец, администраторы, мастера)
    path('api/staff/', include('dashboard.urls')),
    path('api/telegram/', include('notifications.urls')),
    # Супер-админка платформы (только суперпользователь, вход с кодом из приложения)
    path('api/super/', include('superadmin.urls')),
]
