#!/bin/sh
# Деплой без простоя («синий-зелёный»). Запускается на сервере из
# .github/workflows/deploy.yml после git pull. Подробности — docs/OPERATIONS.md
set -e
cd "$(dirname "$0")/.."
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 1. Пока работает старый контейнер: новый образ (статика собирается в нём) и миграции
docker compose build web telegram
docker compose run --rm --no-deps web sh -c "python manage.py migrate --noinput && python manage.py createcachetable"

# 2. Caddy с актуальным конфигом (Caddyfile подключён файлом — нужен reload).
#    Reload плавный: начатые запросы дорабатывают со старым конфигом.
#    Переменные из `-e` подставляются в Caddyfile ({$UPSTREAM})
caddy_reload() {
    docker compose exec -T "$@" caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
}
docker compose up -d --no-build --no-deps caddy
caddy_reload

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

# Диагностика: память и адреса, пока работают оба контейнера
echo "Контейнеры web (имя, IP): $(docker inspect -f '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $OLD $NEW | tr '\n' ' ')"
free -m || true
docker stats --no-stream --format '{{.Name}}: {{.MemUsage}}, CPU {{.CPUPerc}}' || true

# 5. Caddy — только в новый контейнер, затем старый плавно останавливаем
#    (gunicorn дорабатывает начатые запросы). Без переключения запросы шли
#    в останавливаемый контейнер, повисали и обрывались (502)
NEW_NAME=$(docker inspect -f '{{.Name}}' "$NEW" | sed 's#^/##')
caddy_reload -e UPSTREAM="$NEW_NAME:8000"
if [ -n "$OLD" ]; then
    docker stop -t 30 $OLD
    docker rm $OLD
fi
# Обратно на имя сервиса — теперь это только новый контейнер
caddy_reload

# Telegram-бот: фоновый процесс на новом коде и адрес вебхука (без токена — пропускается)
docker compose up -d --no-build --no-deps --force-recreate telegram
docker exec "$NEW" python manage.py telegram_setup || echo "Вебхук Telegram не обновлён (деплой продолжается)"
# Загрузка фото работает только с ключами Cloudinary, Sentry — с SENTRY_DSN (значения не печатаются)
docker exec "$NEW" python manage.py shell -c "from django.conf import settings as s; c = s.CLOUDINARY_STORAGE; print('Cloudinary (фото):', 'ключи заданы' if c['API_KEY'] and c['API_SECRET'] else 'ключей нет - фото не загрузятся'); print('Sentry (ошибки):', 'включён' if s.SENTRY_DSN else 'выключен - нет SENTRY_DSN')" || true

docker image prune -f

# 6. Ошибки прокси за время деплоя (502 и т.п.) — видны в логе GitHub Actions
ERRORS=$(docker logs caddy_salon --since "$START" 2>&1 | grep '"logger":"http.log.error"' || true)
if [ -n "$ERRORS" ]; then
    echo "Ошибки прокси во время деплоя: $(echo "$ERRORS" | grep -c .)"
    echo "$ERRORS" | tail -20 | cut -c1-300
else
    echo "Ошибок прокси во время деплоя нет"
fi
# Последние убийства процессов из-за нехватки памяти (если есть sudo без пароля)
sudo -n dmesg -T 2>/dev/null | grep -i -E 'out of memory|killed process' | tail -5 || true
