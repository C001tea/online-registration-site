"""Вход в супер-админку: пароль → код из приложения (при первом входе — QR-код) → токен на 4 часа."""
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.login_limit import (
    LOGIN_BLOCKED_MESSAGE, is_login_blocked, login_limit_key, register_login_failure, reset_login_failures,
)
from dashboard.tokens import password_fingerprint

from . import totp
from .auth import IsSuperAdmin, SuperJWTAuthentication, issue_token, log, make_ticket, read_ticket
from .models import SuperAdminLog, SuperAdminTOTP

LOGIN_FAILED = 'Неверный логин или пароль'
CODE_FAILED = 'Неверный код'
CODE_BLOCKED = 'Слишком много неверных кодов. Попробуйте через 15 минут'
TICKET_EXPIRED = 'Время на ввод кода вышло, войдите заново'


class SuperView(APIView):
    authentication_classes = [SuperJWTAuthentication]
    permission_classes = [IsSuperAdmin]


class LoginView(APIView):
    """Шаг 1: логин и пароль суперпользователя Django. Ответ — пропуск ко второму шагу."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        username = str(request.data.get('username') or '').strip()
        password = str(request.data.get('password') or '')
        limit_key = login_limit_key(f'super:{username}')
        if is_login_blocked(limit_key):
            return Response({'error': LOGIN_BLOCKED_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        user = authenticate(request, username=username, password=password) if username and password else None
        if user is None or not user.is_superuser:
            register_login_failure(limit_key)
            return Response({'error': LOGIN_FAILED}, status=status.HTTP_400_BAD_REQUEST)
        reset_login_failures(limit_key)

        record = SuperAdminTOTP.objects.filter(user=user).first()
        if record and record.confirmed_at:
            return Response({'step': 'code', 'ticket': make_ticket(user, 'code')})
        # Первый вход (или QR так и не подтвердили): новый секрет при каждом входе
        secret = totp.new_secret()
        SuperAdminTOTP.objects.update_or_create(user=user, defaults={'secret': secret, 'confirmed_at': None,
                                                                    'last_step': 0, 'backup_codes': []})
        return Response({
            'step': 'setup',
            'ticket': make_ticket(user, 'setup'),
            'secret': secret,
            'otpauth_url': totp.otpauth_url(secret, user.username),
        })


class VerifyView(APIView):
    """Шаг 2: код из приложения (или резервный код). При первом входе выдаёт резервные коды."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = read_ticket(request.data.get('ticket'))
        if data is None:
            return Response({'error': TICKET_EXPIRED}, status=status.HTTP_400_BAD_REQUEST)
        user_id, stage, pwd = data
        user = User.objects.filter(pk=user_id, is_active=True, is_superuser=True).first()
        if user is None or not constant_time_compare(pwd or '', password_fingerprint(user)):
            return Response({'error': TICKET_EXPIRED}, status=status.HTTP_400_BAD_REQUEST)

        limit_key = f'super-code:{user.pk}'
        if is_login_blocked(limit_key):
            return Response({'error': CODE_BLOCKED}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        code = str(request.data.get('code') or '').strip()
        with transaction.atomic():
            record = SuperAdminTOTP.objects.select_for_update().filter(user=user).first()
            if record is None or (stage == 'code') != bool(record.confirmed_at):
                return Response({'error': TICKET_EXPIRED}, status=status.HTTP_400_BAD_REQUEST)

            backup_codes = None
            used_backup = False
            step = totp.match_step(record.secret, code)
            if step is not None and step > record.last_step:
                record.last_step = step
            elif stage == 'code' and totp.hash_backup(code) in record.backup_codes:
                record.backup_codes = [h for h in record.backup_codes if h != totp.hash_backup(code)]
                used_backup = True
            else:
                register_login_failure(limit_key)
                log(request, 'login_failed', user=user, details={'stage': stage})
                return Response({'error': CODE_FAILED}, status=status.HTTP_400_BAD_REQUEST)

            if stage == 'setup':
                record.confirmed_at = timezone.now()
                backup_codes = totp.new_backup_codes()
                record.backup_codes = [totp.hash_backup(c) for c in backup_codes]
            record.save()

        reset_login_failures(limit_key)
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        if stage == 'setup':
            log(request, 'totp_setup', user=user)
        log(request, 'login', user=user, details={'backup_code': True} if used_backup else {})
        body = {'token': issue_token(user), 'me': me_data(user, record)}
        if backup_codes:
            body['backup_codes'] = backup_codes
        return Response(body)


def me_data(user, record):
    return {'username': user.username, 'backup_codes_left': len(record.backup_codes)}


class MeView(SuperView):
    def get(self, request):
        return Response(me_data(request.user, request.user.superadmin_totp))


class BackupCodesView(SuperView):
    """Новые резервные коды (старые перестают действовать). Нужен текущий код из приложения."""

    def post(self, request):
        limit_key = f'super-code:{request.user.pk}'
        if is_login_blocked(limit_key):
            return Response({'error': CODE_BLOCKED}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        with transaction.atomic():
            record = SuperAdminTOTP.objects.select_for_update().get(user=request.user)
            step = totp.match_step(record.secret, request.data.get('code'))
            if step is None or step <= record.last_step:
                register_login_failure(limit_key)
                return Response({'error': CODE_FAILED}, status=status.HTTP_400_BAD_REQUEST)
            codes = totp.new_backup_codes()
            record.last_step = step
            record.backup_codes = [totp.hash_backup(c) for c in codes]
            record.save()
        log(request, 'backup_codes_regenerated')
        return Response({'backup_codes': codes})


class LogView(SuperView):
    """Последние 200 действий в супер-админке."""

    def get(self, request):
        rows = SuperAdminLog.objects.select_related('user', 'company')[:200]
        return Response([{
            'id': r.id,
            'at': r.created_at.isoformat(),
            'user': r.user.username if r.user else None,
            'action': r.action,
            'company': {'id': r.company.id, 'name': r.company.name, 'slug': r.company.slug} if r.company else None,
            'details': r.details,
            'ip': r.ip,
        } for r in rows])
