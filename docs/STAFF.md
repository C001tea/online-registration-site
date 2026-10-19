# Панель салона: персонал и доступ

Панель салона — фронт `/admin-panel`, API — `/api/staff/` (приложение `dashboard`).
Вход по логину и паролю, отдельные JWT персонала (claim `scope=staff`, access 12 часов, refresh 7 дней).
Клиентские токены (`/api/auth/login/`) в панель не пускают (403).

## Роли

| Роль | Кто | Что может |
|---|---|---|
| `owner` | владелец салона (`AdminProfile.role='owner'`) | всё: журнал, записи, услуги и категории, мастера, графики, выходные, доступы мастеров, данные салона |
| `admin` | администратор (`AdminProfile.role='admin'`) | то же, что владелец; не может менять пароль/логин и снимать доступ у аккаунтов владельцев и админов через «Доступ мастера» |
| `master` | мастер с привязанным пользователем (`Master.user`, мастер активен) | только свои записи (создать к себе, менять статус и заметку), просмотр услуг, своего графика и данных салона |

Роль определяется на каждый запрос: если мастера скрыть или отвязать от него пользователя,
доступ пропадает сразу, даже с действующим токеном.
У салона может быть несколько владельцев/администраторов; у пользователя — только один `AdminProfile`.

## Как создать владельца на проде

```bash
docker compose exec web python manage.py create_staff --company jcnails --username owner --role owner
```

Пароль будет запрошен дважды (не попадает в историю shell). Другие варианты:

```bash
# Администратор
docker compose exec web python manage.py create_staff --company jcnails --username olga --role admin --name "Ольга"

# Мастер (id мастера — из админки Django или GET /api/staff/masters/)
docker compose exec web python manage.py create_staff --company jcnails --username anna --role master --master-id 3

# Владелец, который сам работает мастером
docker compose exec web python manage.py create_staff --company jcnails --username owner --role owner --master-id 1

# Сбросить пароль существующему пользователю
docker compose exec web python manage.py create_staff --company jcnails --username owner --role owner --reset-password
```

- Если пользователь с таким логином уже есть, команда привязывает **существующий** аккаунт (и пишет об этом);
  пароль меняется только с `--reset-password`.
- Мастер должен быть из указанного салона; если у мастера уже другой пользователь или пользователь
  привязан к другому мастеру — ошибка. Если у пользователя `AdminProfile` другого салона — ошибка.
- Всё выполняется в одной транзакции: при ошибке ничего не меняется.

Дальше владелец/администратор выдаёт доступ мастерам сам: «Мастера → Доступ в панель»
(`POST /api/staff/masters/{id}/access/`). Там создаётся только **новый** пользователь —
существующие аккаунты (в том числе клиентские) не привязываются.

## Миграция 0012_staff_admin

- `AdminProfile.company`: OneToOne → ForeignKey (на Postgres снимается UNIQUE, данные целы);
  новое поле `AdminProfile.role`; всем существующим `AdminProfile` с салоном ставится `role='owner'`.
- `Appointment.status`: новый статус `no_show`; новое поле `Appointment.created_by_staff`.
- Новая модель `MasterDayOff` (выходные мастера по датам).

**Откат 0012 невозможен, если у какого-то салона больше одного AdminProfile**
(company снова станет UNIQUE и Postgres вернёт ошибку). Перед откатом оставьте по одному админу на салон.

### Проверка перед деплоем

```bash
docker compose exec web python manage.py showmigrations bookings   # на проде последняя применённая — 0011
git status bookings/migrations                                     # чисто: никаких миграций, созданных на сервере
```

На проде **не запускать** `makemigrations` — только `migrate` из репозитория.

Посмотреть текущих админов и мастеров с доступом (до и после миграции):

```bash
docker compose exec web python manage.py shell -c "
from bookings.models import AdminProfile, Master
print(list(AdminProfile.objects.filter(company__isnull=False).values_list('user__username', 'company__slug')))
print(list(Master.objects.filter(user__isnull=False).values_list('name', 'user__username', 'company__slug', 'is_active')))
"
```

После миграции у прежних админов должна быть роль `owner`.

## Известные риски и ограничения

- Трафик Vercel → бэк идёт по HTTP, бэк доступен напрямую по IP. Пароли и токены персонала
  передаются без шифрования на этом участке — нужен HTTPS и домен (отдельный этап).
- Лимит перебора паролей (5 неудачных попыток за 15 минут на логин/телефон) хранится в django cache.
  По умолчанию это LocMem: у каждого воркера gunicorn свой счётчик, он сбрасывается при рестарте.
- JWT не отзываются: после смены пароля или снятия доступа уже выданный токен формально действует
  до истечения (access — 12 часов), но доступ проверяется на каждый запрос — отвязанный/скрытый мастер
  и админ без салона получают 403 сразу.
- `AdminProfile.access_token` устарел (вход по токену в URL удалён) и скрыт в админке Django.
