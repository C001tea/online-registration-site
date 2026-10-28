#!/bin/bash
# Проверка резервной копии: последняя копия из ~/backups восстанавливается во ВРЕМЕННУЮ
# базу restore_check, число строк в главных таблицах сравнивается с рабочей базой,
# временная база удаляется. Рабочая база только читается (count).
# Запуск: Actions → Backup restore check (раз в неделю) или на сервере: bash deploy/restore-check.sh
# Печатает только числа строк — никаких имён и телефонов.
set -euo pipefail

CONTAINER=${CONTAINER:-postgres_db}
DB_USER=${DB_USER:-artacsercs}
LIVE_DB=${LIVE_DB:-business_db}
TMP_DB=restore_check
BACKUPS=${BACKUPS:-$HOME/backups}
MAX_AGE_HOURS=${MAX_AGE_HOURS:-48}
# Главные таблицы: если в рабочей базе есть строки, в копии они тоже должны быть
CORE="bookings_company bookings_master bookings_service bookings_appointment auth_user"
# Для сведения: могли появиться позже копии (новые разделы) или быть пустыми
EXTRA="bookings_clientprofile finance_payment"

# Число строк; таблицы нет — «нет»
psql_count() {
    docker exec "$CONTAINER" psql -U "$DB_USER" -d "$1" -tAc "SELECT count(*) FROM $2" 2>/dev/null || echo "нет"
}

drop_tmp() {
    docker exec "$CONTAINER" dropdb -U "$DB_USER" --if-exists "$TMP_DB" >/dev/null 2>&1 || true
}
trap drop_tmp EXIT

LATEST=$(ls -1t "$BACKUPS"/business_db-*.dump 2>/dev/null | head -1 || true)
if [ -z "$LATEST" ]; then
    echo "ОШИБКА: в $BACKUPS нет копий business_db-*.dump"
    exit 1
fi
AGE_HOURS=$(( ($(date +%s) - $(stat -c %Y "$LATEST")) / 3600 ))
echo "Последняя копия: $(basename "$LATEST"), $(du -h "$LATEST" | cut -f1), ей $AGE_HOURS ч"
STATUS=0
if [ "$AGE_HOURS" -gt "$MAX_AGE_HOURS" ]; then
    echo "ОШИБКА: копия старше $MAX_AGE_HOURS ч — ежедневный Backup не работает?"
    STATUS=1
fi

drop_tmp
docker exec "$CONTAINER" createdb -U "$DB_USER" "$TMP_DB"
# --exit-on-error: любая ошибка восстановления — провал проверки
docker exec -i "$CONTAINER" pg_restore -U "$DB_USER" -d "$TMP_DB" --no-owner --exit-on-error < "$LATEST"
echo "Копия восстановлена во временную базу $TMP_DB"

# Кириллица — 2 байта на букву, printf считает байты
printf '%-33s %17s %15s\n' "таблица" "рабочая" "копия"
for table in $CORE $EXTRA; do
    live=$(psql_count "$LIVE_DB" "$table")
    restored=$(psql_count "$TMP_DB" "$table")
    printf '%-26s %10s %10s\n' "$table" "$live" "$restored"
    case " $EXTRA " in *" $table "*) continue ;; esac
    if [ "$restored" = "нет" ] || [ "$live" = "нет" ]; then
        echo "ОШИБКА: нет таблицы $table"
        STATUS=1
        continue
    fi
    # Копия ночная: строк может быть меньше, чем сейчас, но не пусто, если в рабочей есть данные
    if [ "$live" -gt 0 ] && [ "$restored" -eq 0 ]; then
        echo "ОШИБКА: в копии пустая таблица $table"
        STATUS=1
    fi
    # И не больше, чем сейчас, с большим запасом на удаления (иначе это чужая/старая база)
    if [ "$restored" -gt $(( live * 2 + 100 )) ]; then
        echo "ОШИБКА: в копии в $table намного больше строк, чем в рабочей базе"
        STATUS=1
    fi
done

if [ "$STATUS" -eq 0 ]; then
    echo "Проверка пройдена: копия восстанавливается, данные на месте"
fi
exit $STATUS
