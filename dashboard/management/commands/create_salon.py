"""Подключить новый салон: салон + владелец с доступом в панель.

Данные — JSON в stdin (так имя салона и пароль не попадают в командную строку и логи):
  {"name": "...", "slug": "...", "owner": "логин", "password": "...", "address": "...", "phone": "..."}
Без --yes только проверяет данные и показывает, что будет создано.
Дальше владелец сам заполняет услуги, мастеров и графики в панели (Помощь → «С чего начать»).
Workflow: Actions → Create salon.
"""
import json
import re
import sys

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bookings.models import AdminProfile, Company

SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])$')
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.@+-]{3,150}$')
# Поддомены сервиса и служебные адреса — салону их не дать
RESERVED_SLUGS = {'www', 'api', 'app', 'admin', 'admin-panel', 'demo', 'e2e', 'mail', 'static', 'media',
                  'help', 'support', 'status', 'blog', 'timepoint'}
SITE_URL = 'https://nail-salon-frontend.vercel.app'


def clean(data):
    """Проверить и нормализовать данные салона; ошибки — списком понятных фраз."""
    if not isinstance(data, dict):
        raise CommandError('Ожидается JSON-объект')
    name = str(data.get('name') or '').strip()
    slug = str(data.get('slug') or '').strip().lower()
    owner = str(data.get('owner') or '').strip()
    password = str(data.get('password') or '')
    address = str(data.get('address') or '').strip()
    phone = str(data.get('phone') or '').strip()

    errors = []
    if not 1 <= len(name) <= 200:
        errors.append('Название салона: от 1 до 200 символов')
    if not SLUG_RE.match(slug):
        errors.append('Адрес салона (slug): 3–30 символов, латиница в нижнем регистре, цифры и дефис, '
                      'без дефиса в начале и конце — например jcnails или nail-studio')
    elif slug in RESERVED_SLUGS:
        errors.append(f'Адрес «{slug}» служебный — выберите другой')
    elif Company.objects.filter(slug=slug).exists():
        errors.append(f'Салон с адресом «{slug}» уже есть')
    if not USERNAME_RE.match(owner):
        errors.append('Логин владельца: 3–150 символов, латиница, цифры и _ . @ + -')
    elif User.objects.filter(username__iexact=owner).exists():
        errors.append(f'Логин «{owner}» уже занят')
    if len(address) > 300:
        errors.append('Адрес: не длиннее 300 символов')
    if len(phone) > 20:
        errors.append('Телефон: не длиннее 20 символов')
    if not password:
        errors.append('Нет пароля владельца')
    else:
        try:
            validate_password(password, User(username=owner))
        except ValidationError as exc:
            errors.append('Пароль владельца не подходит: ' + ' '.join(exc.messages))
    if errors:
        raise CommandError('\n'.join(errors))
    return {'name': name, 'slug': slug, 'owner': owner, 'password': password, 'address': address, 'phone': phone}


class Command(BaseCommand):
    help = 'Подключить новый салон и его владельца (данные — JSON в stdin)'

    def add_arguments(self, parser):
        parser.add_argument('--yes', action='store_true', help='создать; без флага — только проверка')

    def handle(self, *args, **options):
        try:
            data = clean(json.loads(sys.stdin.read() or '{}'))
        except json.JSONDecodeError:
            raise CommandError('В stdin не JSON')

        self.stdout.write(f'Салон «{data["name"]}», адрес {data["slug"]}, владелец {data["owner"]}')
        if not options['yes']:
            self.stdout.write('Проверка пройдена. Пробный запуск — для создания добавьте --yes')
            return

        with transaction.atomic():
            company = Company.objects.create(name=data['name'], slug=data['slug'],
                                             address=data['address'], phone=data['phone'])
            user = User.objects.create_user(data['owner'], password=data['password'], first_name='Владелец')
            AdminProfile.objects.create(user=user, company=company, role='owner')

        self.stdout.write(self.style.SUCCESS('Готово.'))
        self.stdout.write(f'Сайт записи: {SITE_URL}/?salon={data["slug"]}  (или https://{data["slug"]}.timepoint.tech '
                          f'после добавления домена в Vercel)')
        self.stdout.write(f'Панель: {SITE_URL}/admin-panel/login — логин {data["owner"]}')
        self.stdout.write('Дальше владелец: Настройки → Услуги → Мастера → Графики (Помощь → «С чего начать»)')
