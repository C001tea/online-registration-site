"""Техническое состояние платформы для супер-админки.

Без персональных данных: из очереди Telegram — только числа и тексты ошибок отправки.
"""
from datetime import timedelta

import django
from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Min
from django.utils import timezone
from rest_framework.response import Response

from notifications import email, telegram
from notifications.models import OutgoingMessage

from .models import HealthMark
from .views import SuperView

WORKER_STALE = timedelta(minutes=5)
BACKUP_STALE = timedelta(hours=30)
RESTORE_STALE = timedelta(days=8)
QUEUE_STALE = timedelta(minutes=10)


def iso(value):
    return value.isoformat() if value else None


def pending_migrations():
    try:
        executor = MigrationExecutor(connection)
        return len(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    except Exception:  # noqa: BLE001 — состояние не должно ломать страницу
        return None


def database_size():
    if connection.vendor != 'postgresql':
        return None
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_database_size(current_database())')
        return cursor.fetchone()[0]


def mark_state(mark, stale_after, now):
    """ok | stale | failed | missing."""
    if mark is None:
        return 'missing'
    if not mark.ok:
        return 'failed'
    if now - mark.at > stale_after:
        return 'stale'
    return 'ok'


class HealthView(SuperView):
    def get(self, request):
        now = timezone.now()
        marks = {m.key: m for m in HealthMark.objects.all()}

        unsent = OutgoingMessage.objects.filter(sent_at__isnull=True)
        oldest = unsent.aggregate(at=Min('created_at'))['at']
        failing = unsent.filter(attempts__gt=0)
        errors = [{'at': iso(m.created_at), 'attempts': m.attempts, 'error': m.error[:300]}
                  for m in OutgoingMessage.objects.exclude(error='').order_by('-created_at')[:10]]
        queue = {
            'pending': unsent.count(),
            'failing': failing.count(),
            'oldest_pending_at': iso(oldest),
            'sent_24h': OutgoingMessage.objects.filter(sent_at__gte=now - timedelta(hours=24)).count(),
            'stuck': bool(oldest and now - oldest > QUEUE_STALE),
            'errors': errors,
        }

        def mark_row(key, stale_after):
            m = marks.get(key)
            return {'state': mark_state(m, stale_after, now), 'at': iso(m.at) if m else None,
                    'details': m.details if m else {}}

        migrations = pending_migrations()
        checks = {
            'worker': mark_row('worker', WORKER_STALE),
            'backup': mark_row('backup', BACKUP_STALE),
            'restore_check': mark_row('restore_check', RESTORE_STALE),
        }
        config = {
            'telegram': telegram.enabled(),
            'email': email.enabled(),
            'sentry': bool(settings.SENTRY_DSN),
            'photos': bool(settings.CLOUDINARY_STORAGE.get('API_KEY') and settings.CLOUDINARY_STORAGE.get('API_SECRET')),
            'signup': settings.SIGNUP_ENABLED,
            'debug': settings.DEBUG,
        }
        problems = []
        if checks['worker']['state'] != 'ok':
            problems.append('Воркер сообщений не отвечает — напоминания и Telegram не уходят')
        if queue['stuck']:
            problems.append('Очередь Telegram стоит больше 10 минут')
        if checks['backup']['state'] in ('failed', 'stale', 'missing'):
            problems.append('Нет свежего бэкапа базы')
        if checks['restore_check']['state'] == 'failed':
            problems.append('Проверка восстановления бэкапа не прошла')
        if migrations:
            problems.append('Есть непримененные миграции базы')
        if settings.DEBUG:
            problems.append('На сервере включён DEBUG')
        return Response({
            'now': iso(now),
            'problems': problems,
            'checks': checks,
            'queue': queue,
            'config': config,
            'server': {
                'django': django.get_version(),
                'database': connection.vendor,
                'database_size': database_size(),
                'pending_migrations': migrations,
            },
        })
