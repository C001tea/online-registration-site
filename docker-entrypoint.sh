#!/bin/sh
set -e

# При деплое миграции и статика готовятся заранее, пока работает старый
# контейнер (см. .github/workflows/deploy.yml), и здесь их пропускаем.
# При обычном запуске (перезагрузка сервера) — выполняем
if [ "$SKIP_PREPARE" != "1" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
fi

exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-3}"
