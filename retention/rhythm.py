"""Ритм клиента: через сколько клиент обычно возвращается на услугу и кому пора записаться снова.

Клиент — телефон в салоне. Ритм считается отдельно по группе услуг (категория; услуга без категории —
сама по себе): маникюр и стрижка живут своими сроками.

- Интервал — медиана промежутков между визитами клиента в группе (нужно хотя бы два визита).
  Один пропущенный месяц медиану не сбивает.
- Один визит — «обычно повторяют через» у услуги (Service.repeat_days), иначе типичный интервал
  этой группы по всем клиентам салона.
- Срок = последний визит + интервал. Стадии:
  soon — срок в ближайшие SOON_AHEAD дней или недавно прошёл;
  leaving — прошло больше LEAVING интервалов с визита: клиент «уходит», его ещё можно вернуть;
  lost — больше LOST интервалов.
- Не показываем: клиентов с будущей записью в этой группе, чёрный список, удалённые данные.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.utils import timezone

from bookings.i18n import tr
from bookings.models import Appointment
from bookings.slots import ACTIVE_STATUSES
from finance import penalties
from notifications.models import AppointmentTelegram, ClientTelegram
from .models import ClientOptOut, RhythmMessage

MIN_INTERVAL = 7
MAX_INTERVAL = 180
# Два визита ближе этого — один визит, разнесённый на дни (доделка, коррекция), не промежуток ритма
MIN_GAP = 3
HISTORY_DAYS = 730
SOON_AHEAD = 7
LEAVING = 1.5
LOST = 3
# Типичный интервал группы по салону — если промежутков хотя бы столько
SALON_DEFAULT_MIN_SAMPLES = 3
# Визит засчитывается напоминанию, если запись сделана в течение стольких дней после него
ATTRIBUTION_DAYS = 14
STAGES = ('soon', 'leaving', 'lost')


def group_of(service_id, category_id):
    return f'c{category_id}' if category_id else f's{service_id}'


def most_common(values):
    """Самое частое значение; при равенстве — последнее по порядку (values — от старых к новым)."""
    counts = Counter(values)
    best = max(counts.values())
    return next(v for v in reversed(values) if counts[v] == best)


def gaps_of(dates):
    days = sorted(set(dates))
    return [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days >= MIN_GAP]


def clamp(days):
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(round(days))))


def money(value):
    return str(Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def salon_url(company):
    parts = urlsplit(settings.SALON_URL_TEMPLATE.format(slug=company.slug))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))


@dataclass
class ClientRhythm:
    phone: str
    group: str
    category: str
    service: str
    service_id: int
    master_id: int | None
    master: str | None
    last_visit: date
    interval: int
    own_interval: bool
    visits: int
    stage: str = ''
    name: str = ''
    lang: str = 'ru'
    average_check: Decimal | None = None
    usual_weekday: int = 0
    usual_time: str = ''
    telegram_chat: int | None = None
    email: str = ''
    opted_out: bool = False
    messages: list = field(default_factory=list)

    @property
    def due(self):
        return self.last_visit + timedelta(days=self.interval)

    @property
    def channel(self):
        if self.telegram_chat:
            return 'telegram'
        return 'email' if self.email else None

    def to_dict(self, company, today):
        last = self.messages[-1] if self.messages else None
        return {
            'phone': self.phone,
            'name': self.name,
            'group': self.group,
            'category': self.category,
            'service': self.service,
            'master': {'id': self.master_id, 'name': self.master} if self.master_id else None,
            'last_visit': self.last_visit.isoformat(),
            'interval': self.interval,
            'own_interval': self.own_interval,
            'due': self.due.isoformat(),
            'days_left': (self.due - today).days,
            'stage': self.stage,
            'visits': self.visits,
            'average_check': money(self.average_check) if self.average_check is not None else None,
            'channel': self.channel,
            'opted_out': self.opted_out,
            'message': {'channel': last.channel, 'step': last.step,
                        'sent_at': last.sent_at.isoformat()} if last else None,
            'text': invite_text(self, company, today),
        }


def stage_of(elapsed, interval, days_left):
    if days_left > SOON_AHEAD:
        return ''
    if elapsed < LEAVING * interval:
        return 'soon'
    if elapsed < LOST * interval:
        return 'leaving'
    return 'lost'


def client_rhythms(company, master=None, today=None):
    """Ритмы клиентов салона (или одного мастера), у которых срок близко или прошёл."""
    today = today or timezone.localdate()
    own = {'master': master} if master is not None else {}
    visits = list(
        Appointment.objects.filter(company=company, status='completed', date__lte=today,
                                   date__gte=today - timedelta(days=HISTORY_DAYS), **own)
        .exclude(client_phone='').exclude(service__isnull=True)
        .order_by('date', 'start_time', 'id')
        .values('client_phone', 'client_name', 'client_email', 'lang', 'date', 'start_time', 'master_id',
                'master__name', 'service_id', 'service__name', 'service__category_id', 'service__category__name',
                'service__repeat_days', 'price', 'payment__amount'))
    by_client = defaultdict(list)
    for row in visits:
        by_client[(row['client_phone'], group_of(row['service_id'], row['service__category_id']))].append(row)

    # Типичный интервал группы по всему салону (медиана промежутков у клиентов с повторными визитами)
    group_gaps = defaultdict(list)
    for (_, group), rows in by_client.items():
        group_gaps[group] += gaps_of([r['date'] for r in rows])
    salon_default = {g: median(v) for g, v in group_gaps.items() if len(v) >= SALON_DEFAULT_MIN_SAMPLES}

    future = set(
        (row['client_phone'], group_of(row['service_id'], row['service__category_id']))
        for row in Appointment.objects.filter(company=company, status__in=ACTIVE_STATUSES, date__gte=today)
        .exclude(service__isnull=True).values('client_phone', 'service_id', 'service__category_id'))

    result = []
    for (phone, group), rows in by_client.items():
        if (phone, group) in future:
            continue
        last = rows[-1]
        gaps = gaps_of([r['date'] for r in rows])
        own_interval = bool(gaps)
        interval = median(gaps) if gaps else (last['service__repeat_days'] or salon_default.get(group))
        if not interval:
            continue
        interval = clamp(interval)
        elapsed = (today - last['date']).days
        stage = stage_of(elapsed, interval, interval - elapsed)
        if not stage:
            continue
        masters = [r['master_id'] for r in rows if r['master_id']]
        master_id = most_common(masters) if masters else None
        master_name = next((r['master__name'] for r in reversed(rows) if r['master_id'] == master_id), None)
        checks = [r['payment__amount'] if r['payment__amount'] is not None else r['price'] for r in rows]
        checks = [c for c in checks if c is not None]
        result.append(ClientRhythm(
            phone=phone, group=group, category=last['service__category__name'] or last['service__name'],
            service=last['service__name'], service_id=last['service_id'], master_id=master_id, master=master_name,
            last_visit=last['date'], interval=interval, own_interval=own_interval,
            visits=len({r['date'] for r in rows}), stage=stage, lang=last['lang'] or 'ru',
            average_check=sum(checks) / len(checks) if checks else None,
            usual_weekday=most_common([r['date'].weekday() for r in rows]),
            usual_time=most_common([r['start_time'].strftime('%H:%M') for r in rows]),
            email=next((r['client_email'] for r in reversed(rows) if r['client_email']), ''),
        ))
    return enrich(company, result)


def enrich(company, items):
    """Имя, чаты Telegram, отписки и отметки «позвали» — одним запросом на всё; чёрный список убираем."""
    phones = {item.phone for item in items}
    if not phones:
        return []
    black = penalties.blacklist(company, phones)
    items = [item for item in items if item.phone not in black]
    names = {}
    for row in (Appointment.objects.filter(company=company, client_phone__in=phones)
                .order_by('date', 'start_time', 'id').values('client_phone', 'client_name')):
        names[row['client_phone']] = row['client_name']
    chats = dict(AppointmentTelegram.objects.filter(
        appointment__company=company, appointment__client_phone__in=phones, chat_id__isnull=False,
    ).order_by('appointment__date', 'id').values_list('appointment__client_phone', 'chat_id'))
    client_users = dict(Appointment.objects.filter(company=company, client_phone__in=phones, client__isnull=False)
                        .values_list('client_phone', 'client_id'))
    user_chats = dict(ClientTelegram.objects.filter(user_id__in=client_users.values()).values_list('user_id', 'chat_id'))
    opted_out = set(ClientOptOut.objects.filter(company=company, phone__in=phones, kind__in=('rhythm', 'all'))
                    .values_list('phone', flat=True))
    messages = defaultdict(list)
    for message in RhythmMessage.objects.filter(company=company, phone__in=phones).order_by('sent_at', 'id'):
        messages[(message.phone, message.group, message.cycle)].append(message)
    for item in items:
        item.name = names.get(item.phone, '')
        item.telegram_chat = chats.get(item.phone) or user_chats.get(client_users.get(item.phone))
        item.opted_out = item.phone in opted_out
        item.messages = messages.get((item.phone, item.group, item.last_visit), [])
    return items


# --- Текст «позвать» для WhatsApp / Viber (сотрудник отправляет со своего телефона) ---

def plural_ru(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def plural_ro(n, one, many):
    if n == 1:
        return one
    return f'de {many}' if n % 100 >= 20 or n % 100 == 0 else many


def ago_text(days, lang):
    if days < 14:
        n, ru, ro = days, ('день', 'дня', 'дней'), ('zi', 'zile')
    elif days < 60:
        n, ru, ro = round(days / 7), ('неделя', 'недели', 'недель'), ('săptămână', 'săptămâni')
    else:
        n, ru, ro = round(days / 30), ('месяц', 'месяца', 'месяцев'), ('lună', 'luni')
    if lang == 'ro':
        return f'{n} {plural_ro(n, *ro)}'
    return f'{n} {plural_ru(n, *ru)}'


def first_name(name):
    parts = (name or '').split()
    return parts[0] if parts else ''


def invite_text(item, company, today):
    lang = item.lang if item.lang in ('ru', 'ro') else 'ru'
    name = first_name(item.name)
    hello = tr('Здравствуйте, {name}!', lang, name=name) if name else tr('Здравствуйте!', lang)
    url = salon_url(company)
    if item.stage == 'lost':
        body = tr('Давно вас не видели — будем рады видеть вас снова. Выбрать время: {url}', lang, url=url)
    else:
        visit = f'{item.service}, {item.master}' if item.master else item.service
        body = tr('Прошло уже {ago} с вашего визита ({visit}). Записать вас снова? Выбрать время: {url}', lang,
                  ago=ago_text((today - item.last_visit).days, lang), visit=visit, url=url)
    return f'{hello} {body}\n{company.name}'


# --- Сколько клиентов вернулось после напоминаний ---

def returned_stats(company, master=None, since=None):
    """Клиенты, записавшиеся в течение ATTRIBUTION_DAYS после того, как их позвали (с даты since)."""
    messages = RhythmMessage.objects.filter(company=company, sent_at__date__gte=since)
    if master is not None:
        messages = messages.filter(phone__in=Appointment.objects.filter(company=company, master=master)
                                   .values('client_phone'))
    if not messages.exists():
        return {'invited': 0, 'returned': 0, 'revenue': money(0)}
    by_phone = defaultdict(list)
    for message in messages:
        by_phone[message.phone].append(message)
    own = {'master': master} if master is not None else {}
    bookings = (Appointment.objects.filter(company=company, client_phone__in=by_phone.keys(), **own)
                .exclude(status='cancelled').exclude(service__isnull=True)
                .values('id', 'client_phone', 'created_at', 'service_id', 'service__category_id', 'price',
                        'payment__amount'))
    returned, revenue = set(), Decimal('0')
    for row in bookings:
        group = group_of(row['service_id'], row['service__category_id'])
        for message in by_phone[row['client_phone']]:
            if message.group == group and message.sent_at <= row['created_at'] <= message.sent_at + timedelta(days=ATTRIBUTION_DAYS):
                returned.add(row['client_phone'])
                amount = row['payment__amount'] if row['payment__amount'] is not None else row['price']
                revenue += amount or 0
                break
    invited = len({(m.phone, m.group, m.cycle) for m in messages})
    return {'invited': invited, 'returned': len(returned), 'revenue': money(revenue)}
