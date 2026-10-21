#!/bin/sh
# Деплой без простоя («синий-зелёный»). Запускается на сервере из
# .github/workflows/deploy.yml после git pull. Подробности — docs/OPERATIONS.md
set -e
cd "$(dirname "$0")/.."

# 1. Пока работает старый контейнер: новый образ, миграции и статика
docker compose build web
docker compose run --rm --no-deps web sh -c "python manage.py migrate --noinput && python manage.py collectstatic --noinput"

# 2. Caddy с актуальным конфигом (Caddyfile подключён файлом — нужен reload)
docker compose up -d --no-build --no-deps caddy
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile

# 3. Новый контейнер web рядом со старым
OLD=$(docker compose ps -q web)
COUNT=$(echo "$OLD" | grep -c . || true)
SKIP_PREPARE=1 docker compose up -d --no-build --no-deps --no-recreate --scale web=$((COUNT + 1)) web
NEW=$(docker compose ps -q web | grep -v -x -F "${OLD:-none}" || true)
if [ -z "$NEW" ]; then
    echo "Не удалось запустить новый контейнер web"
    exit 1
fi

# 4. Ждём, пока новый ответит на /api/health/ (Host — разрешённый домен)
HOST=$(grep -s '^API_DOMAIN=' .env | cut -d= -f2-)
HOST=${HOST:-localhost}
ready=""
for _ in $(seq 1 120); do
    if docker exec "$NEW" python -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:8000/api/health/', headers={'Host': '$HOST'})
urllib.request.urlopen(req, timeout=3)
" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [ -z "$ready" ]; then
    echo "Новый контейнер не ответил на /api/health/ — оставляем старый"
    docker logs --tail 50 "$NEW" || true
    docker rm -f "$NEW"
    exit 1
fi

# 5. Старый контейнер: Caddy уже ходит и в новый; старый плавно
#    останавливаем (gunicorn дорабатывает начатые запросы)
if [ -n "$OLD" ]; then
    docker stop -t 30 $OLD
    docker rm $OLD
fi

docker image prune -f
