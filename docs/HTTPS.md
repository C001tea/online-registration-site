# HTTPS для API (Caddy)

Зачем: сейчас Vercel ходит на бэк по `http://158.101.163.232`, и пароли персонала
и клиентов идут по сети открытым текстом. После перехода Vercel ходит на
`https://<домен>`, а сертификат Let's Encrypt Caddy получает и продлевает сам.

Как устроено: контейнер `caddy` (порты 80 и 443) принимает запросы и передаёт их
в `web` (gunicorn, порт 8000 наружу больше не открыт). Конфиг — `deploy/Caddyfile`.

## Шаги (строго по порядку)

### 1. Проверки на сервере — до мёржа PR
```bash
sudo ss -ltnp | grep -E ':(80|443) '   # 80 — только наш caddy_salon, 443 — пусто
```
Если 443 кем-то занят — остановиться и разобраться (иначе после деплоя caddy не
запустится, а web уже не будет открыт на 80 — сайт ляжет).

В Oracle Cloud: Networking → Virtual Cloud Networks → сеть → Security Lists →
Default Security List → Add Ingress Rules: Source `0.0.0.0/0`, TCP, порт **443**
(80 уже открыт). Без этого сертификат получить можно, но HTTPS снаружи не откроется.

### 2. Домен
Нужно имя, которое указывает на `158.101.163.232`:
- **Свой домен** (рекомендуется): у регистратора добавить A-запись
  `api.<ваш-домен>` → `158.101.163.232`. Проверка: `dig +short api.<ваш-домен>`
  должна вернуть IP (может занять до часа).
- **Временно, без покупки**: `158-101-163-232.sslip.io` — публичный сервис, который
  сам указывает на этот IP. Подходит, чтобы включить HTTPS сегодня; позже заменить
  на свой домен (поменять `API_DOMAIN` и адрес в `vercel.json`).

### 3. `.env` на сервере
```
API_DOMAIN=api.<ваш-домен>        # или 158-101-163-232.sslip.io
HTTPS_ONLY=False                  # включим на шаге 5
```
`ALLOWED_HOSTS` и `CSRF_TRUSTED_ORIGINS` домен получают автоматически из `API_DOMAIN`.
IP `158.101.163.232` в `ALLOWED_HOSTS` пока оставить — Vercel ходит по нему до шага 4.

### 4. Деплой и переключение Vercel
1. Мёрж PR → автодеплой выполнит `docker compose up -d --build web caddy`.
2. Проверка (через 10–30 секунд, пока caddy получает сертификат):
   ```bash
   docker logs caddy_salon --tail 30          # ищем "certificate obtained successfully"
   curl -sI https://api.<ваш-домен>/api/companies/jcnails/info/   # HTTP/2 200
   ```
3. Во фронте `vercel.json`: `"destination": "https://api.<ваш-домен>/api/$1"` →
   мёрж → Vercel передеплоит. С этого момента трафик идёт по HTTPS.

### 5. Куки Django admin только по HTTPS
В `.env`: `HTTPS_ONLY=True`, затем `docker compose up -d web`.
Django admin теперь открывать по `https://api.<ваш-домен>/admin/`.

### 6. Закрыть старый HTTP — сделано
Блок `:80 { ... }` удалён из `deploy/Caddyfile`. После этого по HTTP
отвечает только редирект домена на HTTPS, а запросы на голый IP не обслуживаются.
Из `ALLOWED_HOSTS` можно убрать IP.

## Если что-то пошло не так
- **Сертификат не выдаётся** (`docker logs caddy_salon`: `challenge failed`, `timeout`):
  домен не указывает на IP, или закрыт порт 80/443 в Security List. Caddy сам
  повторяет попытки.
- **Сайт не отвечает после деплоя**: `docker ps` — запущен ли `caddy_salon`;
  `docker logs caddy_salon`. Частая причина — занят порт 443 (шаг 1).
- **Откат**: вернуть предыдущий коммит `main`, затем на сервере
  `docker compose stop caddy && docker compose rm -f caddy && docker compose up -d web`
  (web снова откроется на порту 80).
- Лимиты Let's Encrypt: не больше 5 повторных сертификатов на одно имя в неделю —
  не пересоздавать том `caddy_data` без нужды (в нём хранятся сертификаты).
