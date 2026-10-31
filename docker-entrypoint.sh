#!/bin/sh
set -e

# Статика собрана в образе (Dockerfile). Миграции при деплое выполняются заранее,
# пока работает старый контейнер (deploy/deploy.sh), и здесь их пропускаем.
# При обычном запуске (перезагрузка сервера) — выполняем
if [ "$SKIP_PREPARE" != "1" ]; then
    python manage.py migrate --noinput
    python manage.py createcachetable  # общий кеш лимитов (CACHES в settings.py)
fi

# --preload: Django загружается до открытия порта. Иначе порт открыт сразу,
# а воркеры ещё ~30 с грузят приложение — запросы, попавшие в новый
# контейнер при деплое, висят (см. docs/OPERATIONS.md)
exec gunicorn config.wsgi:application --preload --bind 0.0.0.0:8000 --workers "${GUNICORN_WORKERS:-3}"
