"""Новый салон: проверка адреса (slug) и создание салона с владельцем.

Общее для самостоятельной регистрации (dashboard/signup.py) и ручного подключения
(manage.py create_salon): одни и те же правила адреса и логина.
"""
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from bookings.models import AdminProfile, Company

# Управляющие и невидимые символы (переносы строк, смена направления текста): в названии ломают
# заголовки писем и позволяют подделать вид названия
CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]')


def clean_name(value):
    """Название в одну строку: без управляющих символов, лишние пробелы схлопнуты."""
    return ' '.join(CONTROL_CHARS.sub(' ', str(value or '')).split())


SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{1,28}[a-z0-9])$')
# Поддомены сервиса и служебные адреса — салону их не дать
RESERVED_SLUGS = {
    'www', 'api', 'app', 'admin', 'admin-panel', 'panel', 'demo', 'e2e', 'mail', 'smtp', 'static', 'media',
    'help', 'support', 'status', 'blog', 'timepoint', 'signup', 'login', 'billing', 'pay', 'docs', 'test',
}
SLUG_FORMAT_MESSAGE = ('Адрес: 3–30 символов, латинские буквы в нижнем регистре, цифры и дефис, '
                       'без дефиса в начале и в конце — например nail-studio')

# Транслитерация названия в адрес: кириллица (ru) и румынские буквы
TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i',
    'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
    'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya', 'ă': 'a', 'â': 'a', 'î': 'i', 'ș': 's', 'ş': 's', 'ț': 't', 'ţ': 't',
}


def slugify_name(name):
    """«Студия «Ногти» №1» → «studiya-nogti-1» (подсказка адреса по названию)."""
    text = ''.join(TRANSLIT.get(ch, ch) for ch in (name or '').lower())
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text[:30].rstrip('-')


def slug_error(slug):
    """Почему адрес нельзя взять (текст для владельца) или None — адрес свободен."""
    if not SLUG_RE.match(slug or ''):
        return SLUG_FORMAT_MESSAGE
    if slug in RESERVED_SLUGS:
        return f'Адрес «{slug}» служебный — выберите другой'
    if Company.objects.filter(slug=slug).exists():
        return f'Адрес «{slug}» уже занят — выберите другой'
    return None


def suggest_slug(slug):
    """Свободный адрес рядом с занятым: nail-bar → nail-bar-2, nail-bar-3…"""
    base = (slug or 'salon')[:26].rstrip('-') or 'salon'
    for n in range(2, 50):
        candidate = f'{base}-{n}'
        if slug_error(candidate) is None:
            return candidate
    return None


def login_taken(login):
    """Логин или email уже используется (без учёта регистра)."""
    return User.objects.filter(username__iexact=login).exists() or (
        '@' in login and User.objects.filter(email__iexact=login).exists())


def create_salon(*, name, slug, owner_login, password, email='', address='', phone='', pending=False):
    """Салон и его владелец. pending=True — самостоятельная регистрация: салон скрыт до подтверждения
    email и получает пробный период; ручное подключение — сразу работает."""
    now = timezone.now()
    with transaction.atomic():
        company = Company.objects.create(
            name=name, slug=slug, address=address, phone=phone,
            status=Company.STATUS_PENDING if pending else Company.STATUS_ACTIVE,
            trial_ends_at=now + timedelta(days=settings.TRIAL_DAYS) if pending else None,
            terms_accepted_at=now if pending else None,
            # Самостоятельная регистрация — без штрафов за неявку, пока владелец сам их не включит
            **({'penalty_percent': 0} if pending else {}),
        )
        user = User.objects.create_user(owner_login, email=email, password=password, first_name='Владелец')
        AdminProfile.objects.create(user=user, company=company, role='owner')
    return company, user
