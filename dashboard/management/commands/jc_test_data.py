"""Тестовые данные в настоящем салоне (по умолчанию slug jcnails): посмотреть, залить, стереть.

  inspect — что лежит в салоне (только названия и количества, без имён и телефонов клиентов);
  seed    — наполнить салон как живой: скрыть перечисленных мастеров (--hide), добавить тестовых
            мастеров с фото и описанием, услуги по категориям, логотип и фото салона (если их нет),
            записи с прошлой недели до двух недель вперёд, оплаты, кассу и зарплаты. Повторный запуск
            только дополняет: дни, где у мастера уже есть записи, не трогает;
  cleanup — стереть всё тестовое: тестовых мастеров и записи к ним, тестовых клиентов, тестовые
            операции кассы. С --all — вообще все записи, оплаты, кассу и заметки салона (только до
            запуска: настоящие данные тоже исчезнут), --unhide возвращает скрытых мастеров.

Тестовое помечено: мастера — «[тест]» в описании, клиенты — телефоны +3736999xxxx, операции кассы —
«[тест]» в комментарии, услуги и категории — по названиям из TEST_SERVICES (стираются, только если
на них не осталось записей), картинки — «jc-test» в имени файла. Записи, которые вы сами сделали на сайте, помечены не будут: их стирает
cleanup --all.
"""
import io
import random
from datetime import time, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from bookings.models import AdminProfile, Appointment, Company, Master, MasterSchedule, Service, ServiceCategory
from dashboard.models import ClientNote
from finance.models import CashOperation, Payment, Penalty, SalaryPayout, SalaryScheme
from .seed_demo import Command as Demo

TAG = '[тест]'
PHONE_PREFIX = '+3736999'
IMAGE_MARK = 'jc-test'
DAYS_AHEAD = 14
# (категория, услуга, минут, цена)
TEST_SERVICES = [
    ('Маникюр', 'Маникюр классический', 60, 250),
    ('Маникюр', 'Маникюр + гель-лак', 90, 400),
    ('Маникюр', 'Снятие + маникюр + гель-лак', 120, 450),
    ('Маникюр', 'Мужской маникюр', 45, 220),
    ('Маникюр', 'Детский маникюр', 30, 150),
    ('Педикюр', 'Педикюр классический', 90, 400),
    ('Педикюр', 'Педикюр + гель-лак', 120, 550),
    ('Педикюр', 'Smart-педикюр', 75, 450),
    ('Наращивание', 'Наращивание ногтей', 150, 650),
    ('Наращивание', 'Коррекция наращивания', 120, 500),
    ('Дизайн', 'Френч', 30, 100),
    ('Дизайн', 'Дизайн (2 ногтя)', 15, 50),
    ('Дизайн', 'Укрепление гелем', 30, 120),
    ('Брови и ресницы', 'Коррекция и окрашивание бровей', 45, 250),
    ('Брови и ресницы', 'Ламинирование ресниц', 60, 400),
    ('Брови и ресницы', 'Наращивание ресниц 2D', 150, 550),
]
SALON_DESCRIPTION = ('Студия маникюра и педикюра в центре Кишинёва: стерильные инструменты, '
                     'проверенные материалы и уютная атмосфера. Записывайтесь онлайн — подтвердим в Telegram.')
# имя (и старое имя), рабочие дни (0 — пн), часы, категории услуг, схема зарплаты (%, фикс/день),
# описание, цвет фото
TEST_MASTERS = [
    ('Алина', 'Алина (тест)', [0, 1, 2, 3, 4], (9, 18), ['Маникюр', 'Дизайн'], (40, 0),
     'Топ-мастер маникюра, 7 лет опыта. Идеальное покрытие под кутикулу и сложный дизайн.', (236, 72, 153)),
    ('Диана', 'Диана (тест)', [1, 2, 3, 4, 5], (10, 19), ['Маникюр', 'Наращивание', 'Дизайн'], (35, 100),
     'Мастер наращивания: миндаль, балерина, квадрат. Коррекция без потери длины.', (168, 85, 247)),
    ('Виктория', '', [0, 2, 4, 5, 6], (10, 20), ['Педикюр', 'Маникюр'], (45, 0),
     'Аппаратный и smart-педикюр, работа с проблемной стопой. 5 лет опыта.', (20, 184, 166)),
    ('Кристина', '', [0, 1, 3, 4, 5], (9, 17), ['Маникюр', 'Наращивание', 'Дизайн'], (30, 150),
     'Нюд, френч и минимализм. Аккуратная форма и стойкое покрытие до 4 недель.', (245, 158, 11)),
    ('Марина', '', [1, 2, 3, 5, 6], (11, 20), ['Брови и ресницы'], (40, 0),
     'Бровист и лэшмейкер: архитектура бровей, ламинирование, наращивание ресниц.', (59, 130, 246)),
]
FIRST_NAMES = ['Ирина', 'Ольга', 'Елена', 'Наталья', 'Светлана', 'Татьяна', 'Анастасия', 'Дарья', 'Кристина',
               'Алина', 'Юлия', 'Марина', 'Ана', 'Корина', 'Даниэла', 'Надежда', 'Людмила', 'Валерия']


def test_clients():
    return [(f'{name} Т.', f'{PHONE_PREFIX}{i:04d}') for i, name in enumerate(FIRST_NAMES, start=1)]


class Command(BaseCommand):
    help = 'Тестовые данные в настоящем салоне: inspect / seed / cleanup'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['inspect', 'seed', 'cleanup'])
        parser.add_argument('--slug', default='jcnails')
        parser.add_argument('--hide', nargs='*', default=[], help='имена мастеров, которых скрыть (seed)')
        parser.add_argument('--all', action='store_true', help='cleanup: стереть вообще все записи и кассу салона')
        parser.add_argument('--unhide', action='store_true', help='cleanup: вернуть скрытых мастеров')
        parser.add_argument('--yes', action='store_true', help='подтверждение для seed и cleanup')

    def handle(self, *args, **options):
        company = Company.objects.filter(slug=options['slug']).first()
        if not company:
            raise CommandError(f'Салон {options["slug"]} не найден')
        getattr(self, options['action'])(company, options)

    # --- Что в салоне ---

    def inspect(self, company, options):
        out = self.stdout.write
        out(f'Салон: {company.name} (slug {company.slug})')
        for master in Master.objects.filter(company=company).order_by('id').annotate(n=Count('appointments')):
            out(f'  мастер #{master.id} «{master.name}»: {"активен" if master.is_active else "СКРЫТ"}, '
                f'записей {master.n}, услуг {master.services.count()}, вход в панель: {"есть" if master.user_id else "нет"}')
        for service in Service.objects.filter(company=company).order_by('id'):
            out(f'  услуга #{service.id} «{service.name}»: {service.duration} мин, {service.price} L, '
                f'{"активна" if service.is_active else "СКРЫТА"}')
        appointments = Appointment.objects.filter(company=company)
        by_status = dict(appointments.values_list('status').annotate(n=Count('id')))
        out(f'Записи: всего {appointments.count()} {by_status}; тестовых (+3736999…) '
            f'{appointments.filter(client_phone__startswith=PHONE_PREFIX).count()}')
        out(f'Оплат: {Payment.objects.filter(company=company).count()}, '
            f'касса: {CashOperation.objects.filter(company=company).count()}, '
            f'выплат: {SalaryPayout.objects.filter(company=company).count()}, '
            f'заметок о клиентах: {ClientNote.objects.filter(company=company).count()}')
        out('Входы в панель (роль): ' + ', '.join(
            f'{p.get_role_display() if hasattr(p, "get_role_display") else p.role}'
            for p in AdminProfile.objects.filter(company=company)))

    # --- Залить ---

    def seed(self, company, options):
        if not options['yes']:
            raise CommandError('Добавьте --yes: команда меняет данные салона')
        now = timezone.localtime()
        today = now.date()
        monday = today - timedelta(days=today.weekday())
        rng = random.Random(2026)
        demo = Demo()
        owner = AdminProfile.objects.filter(company=company, role='owner').first()
        admin = AdminProfile.objects.filter(company=company, role='admin').first()
        owner_user, admin_user = (owner.user if owner else None), (admin.user if admin else owner.user if owner else None)

        with transaction.atomic():
            hidden = []
            for name in options['hide']:
                for master in Master.objects.filter(company=company, name__iexact=name, is_active=True):
                    master.is_active = False
                    master.save(update_fields=['is_active'])
                    hidden.append(master.name)

            by_category, new_services = self.seed_services(company)
            pictures = self.seed_salon(company)
            masters, new_masters = [], 0
            for spec in TEST_MASTERS:
                master, created = self.seed_master(company, spec, by_category)
                new_masters += created
                masters.append(master)

            clients = test_clients()
            appointments = []
            first_day = monday - timedelta(days=7)  # прошлая неделя и две недели вперёд
            for offset in range((today - first_day).days + DAYS_AHEAD + 1):
                day = first_day + timedelta(days=offset)
                for master, days, start, end, master_services in masters:
                    if day.weekday() not in days or master.appointments.filter(date=day).exists():
                        continue
                    appointments += demo.fill_day(rng, company, master, master_services, day, start, end,
                                                  clients, now, admin_user)
            payments, revenue = demo.pay_visits(rng, appointments, admin_user, owner_user)
            demo.penalize(appointments, admin_user)
            operations = 0
            if not CashOperation.objects.filter(company=company, comment__startswith=TAG).exists():
                before = CashOperation.objects.filter(company=company).aggregate(m=Max('id'))['m'] or 0
                operations = demo.cash_operations(rng, company, owner_user, monday, today, masters)
                for op in CashOperation.objects.filter(company=company, id__gt=before):
                    op.comment = f'{TAG} {op.comment}'.strip()
                    op.save(update_fields=['comment'])

        self.stdout.write(
            f'Скрыто мастеров: {len(hidden)} ({", ".join(hidden) or "—"}). Тестовых мастеров: {len(masters)} '
            f'(новых {new_masters}), новых услуг: {new_services}, картинки салона: {pictures or "уже были"}. '
            f'Добавлено {len(appointments)} записей (по {today + timedelta(days=DAYS_AHEAD):%d.%m}), '
            f'{payments} оплат на {revenue} L, {operations} операций по кассе.')

    def seed_services(self, company):
        """Категории и услуги из TEST_SERVICES (существующие с тем же названием не дублируются)."""
        by_category, created = {}, 0
        for category_name, name, duration, price in TEST_SERVICES:
            category = ServiceCategory.objects.filter(company=company, name=category_name).first() \
                or ServiceCategory.objects.create(company=company, name=category_name)
            service = Service.objects.filter(company=company, name=name).first()
            if not service:
                service = Service.objects.create(company=company, category=category, name=name,
                                                 duration=duration, price=price)
                created += 1
            by_category.setdefault(category_name, []).append(service)
        return by_category, created

    def seed_salon(self, company):
        """Логотип, фото и описание салона — только если их ещё нет."""
        done = []
        if not company.logo:
            company.logo.save(f'{IMAGE_MARK}-logo.png', ContentFile(salon_logo(company.name)), save=False)
            done.append('логотип')
        if not company.main_photo:
            company.main_photo.save(f'{IMAGE_MARK}-salon.jpg', ContentFile(salon_photo(company.name)), save=False)
            done.append('фото')
        if not company.description:
            company.description = SALON_DESCRIPTION
            done.append('описание')
        company.save()
        return ', '.join(done)

    def seed_master(self, company, spec, by_category):
        """Тестовый мастер: создать или дополнить (фото, описание, услуги) уже залитого."""
        name, old_name, days, (start, end), categories, (percent, fixed), about, color = spec
        services = [service for category in categories for service in by_category[category]]
        master = Master.objects.filter(company=company, description__startswith=TAG,
                                       name__in=[n for n in (name, old_name) if n]).first()
        created = master is None
        if created:
            master = Master.objects.create(company=company, name=name)
            for day in range(7):
                MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(start),
                                              end_time=time(end), is_working=day in days,
                                              break_start=time(13) if day in days else None,
                                              break_end=time(14) if day in days else None)
            SalaryScheme.objects.create(master=master, percent=Decimal(percent), fixed_per_day=Decimal(fixed))
        master.name = name
        master.description = f'{TAG} {about}'
        if not master.photo:
            master.photo.save(f'{IMAGE_MARK}-master-{master.id}.jpg', ContentFile(master_photo(color)), save=False)
        master.save()
        master.services.add(*services)
        return (master, days, start, end, services), created

    # --- Стереть ---

    def cleanup(self, company, options):
        test_masters = Master.objects.filter(company=company, description__startswith=TAG)
        test_appointments = Appointment.objects.filter(company=company, client_phone__startswith=PHONE_PREFIX)
        test_ops = CashOperation.objects.filter(company=company, comment__startswith=TAG)
        test_services = Service.objects.filter(company=company, name__in=[n for _, n, *_ in TEST_SERVICES])
        plan = {
            'тестовых мастеров': test_masters.count(),
            'тестовых записей': test_appointments.count(),
            'тестовых операций кассы': test_ops.count(),
            'тестовых услуг (если на них не осталось записей)': test_services.count(),
        }
        if options['all']:
            plan.update({
                'ВСЕХ записей салона': Appointment.objects.filter(company=company).count(),
                'ВСЕХ оплат': Payment.objects.filter(company=company).count(),
                'ВСЕХ операций кассы': CashOperation.objects.filter(company=company).count(),
                'штрафов (чёрный список)': Penalty.objects.filter(company=company).count(),
                'заметок о клиентах': ClientNote.objects.filter(company=company).count(),
            })
        self.stdout.write('Будет удалено: ' + ', '.join(f'{k} {v}' for k, v in plan.items()))
        if not options['yes']:
            self.stdout.write('Пробный запуск. Для удаления добавьте --yes')
            return
        with transaction.atomic():
            # Сначала кассовые операции (зарплатные выплаты уйдут каскадом), потом записи и мастера
            test_ops.delete()
            SalaryPayout.objects.filter(master__in=test_masters).delete()
            # Оплаченные штрафы тестовых клиентов — приходы в кассе
            CashOperation.objects.filter(penalty__appointment__in=test_appointments).delete()
            test_appointments.delete()
            if options['all']:
                CashOperation.objects.filter(company=company).delete()  # выплаты каскадом
                Appointment.objects.filter(company=company).delete()  # оплаты и штрафы каскадом
                ClientNote.objects.filter(company=company).delete()
            for master in test_masters:
                if master.photo and IMAGE_MARK in master.photo.name:
                    master.photo.delete(save=False)
            test_masters.delete()
            # Тестовые услуги — только без записей (на них мог записаться живой клиент)
            test_services.filter(appointments__isnull=True).delete()
            ServiceCategory.objects.filter(company=company, name__in=[c for c, *_ in TEST_SERVICES],
                                           categories__isnull=True).delete()
            for field in (company.logo, company.main_photo):
                if field and IMAGE_MARK in field.name:
                    field.delete(save=False)
            if company.description == SALON_DESCRIPTION:
                company.description = ''
            company.save()
            if options['unhide']:
                Master.objects.filter(company=company, is_active=False).update(is_active=True)
        self.stdout.write('Готово.')


# --- Картинки (рисуются на месте, без скачивания) ---

def _jpeg(image, fmt='JPEG'):
    buffer = io.BytesIO()
    image.save(buffer, fmt, quality=88) if fmt == 'JPEG' else image.save(buffer, fmt)
    return buffer.getvalue()


def _gradient(size, top, bottom):
    from PIL import Image
    width, height = size
    image = Image.new('RGB', size, top)
    pixels = image.load()
    for y in range(height):
        k = y / max(height - 1, 1)
        row = tuple(int(a + (b - a) * k) for a, b in zip(top, bottom))
        for x in range(width):
            pixels[x, y] = row
    return image


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except (TypeError, OSError):
        return ImageFont.load_default()


def master_photo(color):
    """Портрет-заглушка: силуэт на мягком градиенте цвета мастера."""
    from PIL import ImageDraw
    light = tuple(min(255, c + (255 - c) * 3 // 4) for c in color)
    image = _gradient((400, 400), light, tuple(min(255, c + (255 - c) // 3) for c in color))
    draw = ImageDraw.Draw(image)
    dark = tuple(c * 3 // 5 for c in color)
    hair = tuple(c * 2 // 5 for c in color)
    draw.ellipse((118, 70, 282, 250), fill=hair)  # волосы
    draw.rectangle((118, 160, 282, 300), fill=hair)
    draw.rectangle((180, 200, 220, 290), fill=(240, 205, 186))  # шея
    draw.ellipse((140, 95, 260, 225), fill=(246, 214, 196))  # лицо
    draw.ellipse((70, 280, 330, 520), fill=dark)  # плечи
    return _jpeg(image)


def salon_logo(name):
    from PIL import ImageDraw
    image = _gradient((400, 400), (236, 72, 153), (190, 24, 93))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 40, 360, 360), outline=(255, 255, 255), width=10)
    words = name.replace('-', ' ').split()
    initials = (''.join(w[0] for w in words[:2]) if len(words) > 1 else name[:2]).upper() or 'JC'
    if not initials.isascii():
        initials = 'JC'
    font = _font(150)
    box = draw.textbbox((0, 0), initials, font=font)
    draw.text(((400 - (box[2] - box[0])) / 2 - box[0], (400 - (box[3] - box[1])) / 2 - box[1]),
              initials, font=font, fill=(255, 255, 255))
    return _jpeg(image, 'PNG')


def salon_photo(name):
    """Интерьер-иллюстрация: стена, столы мастеров, лампы, растение и вывеска."""
    from PIL import ImageDraw
    width, height = 1280, 720
    image = _gradient((width, height), (253, 242, 248), (251, 207, 232))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 520, width, height), fill=(222, 196, 180))  # пол
    draw.rectangle((0, 505, width, 520), fill=(200, 170, 155))
    for x in (180, 560, 940):  # шнуры ламп — за вывеской
        draw.line((x + 80, 0, x + 80, 250), fill=(120, 90, 80), width=3)
    sign = name if name.isascii() else 'JC Nails'
    font = _font(84)
    box = draw.textbbox((0, 0), sign, font=font)
    draw.rounded_rectangle((width / 2 - (box[2] - box[0]) / 2 - 40, 70, width / 2 + (box[2] - box[0]) / 2 + 40, 200),
                           radius=30, fill=(255, 255, 255))
    draw.text((width / 2 - (box[2] - box[0]) / 2 - box[0], 135 - (box[3] - box[1]) / 2 - box[1]), sign,
              font=font, fill=(219, 39, 119))
    for x in (180, 560, 940):  # столы, лампы, кресла
        draw.pieslice((x + 30, 230, x + 130, 330), 180, 360, fill=(250, 204, 21))
        draw.rectangle((x, 400, x + 160, 420), fill=(255, 255, 255))
        draw.rectangle((x + 10, 420, x + 25, 560), fill=(230, 230, 230))
        draw.rectangle((x + 135, 420, x + 150, 560), fill=(230, 230, 230))
        draw.rounded_rectangle((x + 40, 450, x + 120, 600), radius=20, fill=(236, 72, 153))
        for i, c in enumerate([(244, 114, 182), (167, 139, 250), (248, 113, 113)]):
            draw.rounded_rectangle((x + 30 + i * 30, 375, x + 48 + i * 30, 400), radius=4, fill=c)
    draw.rounded_rectangle((1150, 440, 1230, 560), radius=10, fill=(255, 255, 255))  # растение
    for dx, dy in ((-40, -60), (0, -90), (40, -60), (-20, -30), (25, -35)):
        draw.ellipse((1190 + dx - 35, 440 + dy - 35, 1190 + dx + 35, 440 + dy + 35), fill=(74, 160, 110))
    return _jpeg(image)
