FROM python:3.15-slim

WORKDIR /app

# Логи Python сразу в docker logs (без буферизации)
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Статика в образе (whitenoise): при деплое её не нужно собирать отдельно
RUN SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput

CMD ["sh", "docker-entrypoint.sh"]
