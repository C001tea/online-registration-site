"""Соло-мастер: аккаунт из одного мастера, который сам себе владелец.

Технически это салон (Company.kind = 'solo'), где владелец — он же единственный мастер
(Master.user = владелец). После регистрации у мастера сразу есть услуги по специализации
и график, чтобы страница записи заработала без долгой настройки.
"""
from datetime import time

from bookings.models import Master, MasterSchedule, Service, ServiceCategory

# Шаблоны услуг: (название, минуты, цена в леях) — мастер потом меняет под себя
TEMPLATES = {
    'manicure': ('Маникюр', [
        ('Маникюр без покрытия', 60, 200),
        ('Маникюр с покрытием гель-лак', 90, 350),
        ('Наращивание ногтей', 150, 550),
        ('Педикюр с покрытием', 90, 400),
        ('Снятие покрытия', 20, 80),
    ]),
    'brows': ('Брови', [
        ('Коррекция бровей', 30, 150),
        ('Окрашивание бровей', 30, 150),
        ('Ламинирование бровей', 60, 350),
        ('Коррекция и окрашивание', 45, 250),
    ]),
    'lashes': ('Ресницы', [
        ('Наращивание ресниц, классика', 120, 450),
        ('Наращивание ресниц, 2D', 150, 550),
        ('Ламинирование ресниц', 60, 350),
        ('Снятие ресниц', 20, 80),
    ]),
    'massage': ('Массаж', [
        ('Классический массаж, 60 минут', 60, 400),
        ('Массаж спины, 30 минут', 30, 250),
        ('Антицеллюлитный массаж', 60, 450),
        ('Расслабляющий массаж', 90, 550),
    ]),
    'hair': ('Волосы', [
        ('Женская стрижка', 60, 300),
        ('Мужская стрижка', 40, 200),
        ('Окрашивание волос', 150, 700),
        ('Укладка', 45, 250),
    ]),
    'other': ('Услуги', [
        ('Консультация', 30, 100),
        ('Услуга', 60, 300),
    ]),
}
SPECIALIZATIONS = tuple(TEMPLATES)

# График по умолчанию: пн–сб 10:00–19:00, воскресенье — выходной
WORK_START, WORK_END = time(10), time(19)
WORK_DAYS = range(6)


def setup_solo(company, user, name, specialization):
    """Профиль мастера, услуги по шаблону и график для нового соло-аккаунта."""
    user.first_name = name[:150]
    user.save(update_fields=['first_name'])
    master = Master.objects.create(company=company, name=name, user=user)
    title, services = TEMPLATES.get(specialization) or TEMPLATES['other']
    category = ServiceCategory.objects.create(company=company, name=title)
    created = [Service.objects.create(company=company, category=category, name=service_name, duration=minutes,
                                      price=price)
               for service_name, minutes, price in services]
    master.services.set(created)
    MasterSchedule.objects.bulk_create([
        MasterSchedule(master=master, day_of_week=day, start_time=WORK_START, end_time=WORK_END,
                       is_working=day in WORK_DAYS)
        for day in range(7)
    ])
    # Всё, что нужно одному мастеру, сразу включено (у салонов — выключено по умолчанию)
    company.rhythm_enabled = True
    company.reviews_enabled = True
    company.portfolio_enabled = True
    company.promo_enabled = True
    company.save(update_fields=['rhythm_enabled', 'reviews_enabled', 'portfolio_enabled', 'promo_enabled'])
    return master
