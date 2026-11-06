"""Визит из нескольких услуг подряд, в том числе у разных мастеров.

Визит — несколько записей Appointment с общим Appointment.visit (uuid): услуги идут одна за другой без
перерыва, каждая — в колонке своего мастера в журнале. Клиент выбирает услуги (и, если хочет, мастеров),
сайт предлагает время начала, при котором вся цепочка помещается.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from .models import Appointment, Master, Service
from .schedule import day_schedule
from .slots import BUSY_STATUSES, SLOT_STEP, local_now
from .terms import terms

MAX_VISIT_ITEMS = 5


class Chain:
    """Подбор времени для цепочки услуг на одну дату.

    items — [(кандидаты-мастера, услуга)]: один мастер, если клиент его выбрал, иначе все мастера услуги.
    exclude_ids — записи, которые не считать занятыми (перенос этого же визита).
    """

    def __init__(self, items, date, exclude_ids=(), now=None):
        self.items = items
        self.date = date
        self.exclude_ids = set(exclude_ids)
        today = timezone.localdate()
        self.now = now if now is not None else (local_now() if date == today else None)
        self.closed = date < today
        self._schedules = {}
        self._busy = {}

    def schedule(self, master):
        if master.id not in self._schedules:
            self._schedules[master.id] = day_schedule(master, self.date)
        return self._schedules[master.id]

    def busy(self, master):
        if master.id not in self._busy:
            rows = Appointment.objects.filter(master=master, date=self.date, status__in=BUSY_STATUSES)
            if self.exclude_ids:
                rows = rows.exclude(pk__in=self.exclude_ids)
            self._busy[master.id] = [
                (datetime.combine(self.date, s), datetime.combine(self.date, e))
                for s, e in rows.values_list('start_time', 'end_time')
            ]
        return self._busy[master.id]

    def fits(self, master, start, minutes, taken):
        schedule = self.schedule(master)
        if not schedule.is_working:
            return False
        end = start + timedelta(minutes=minutes)
        if start < datetime.combine(self.date, schedule.start) or end > datetime.combine(self.date, schedule.end):
            return False
        breaks = [(datetime.combine(self.date, s), datetime.combine(self.date, e)) for s, e in schedule.breaks]
        intervals = self.busy(master) + breaks + taken.get(master.id, [])
        return not any(b_start < end and b_end > start for b_start, b_end in intervals)

    def at(self, start):
        """Части визита, если он помещается с началом start (datetime), иначе None."""
        if self.closed or (self.now is not None and start <= self.now):
            return None
        parts, taken, current = [], {}, start
        for item in self.items:
            candidates, service = item[0], item[1]
            for master in candidates:
                minutes, price = terms(master, service)
                if len(item) > 2:
                    minutes, price = item[2], item[3]
                if self.fits(master, current, minutes, taken):
                    end = current + timedelta(minutes=minutes)
                    parts.append({'master': master, 'service': service, 'start': current, 'end': end,
                                  'price': price})
                    taken.setdefault(master.id, []).append((current, end))
                    current = end
                    break
            else:
                return None
        return parts

    def starts(self):
        """Возможное время начала: шаг 30 минут от начала рабочего дня мастеров первой услуги."""
        times = set()
        for master in self.items[0][0]:
            schedule = self.schedule(master)
            if not schedule.is_working:
                continue
            current = datetime.combine(self.date, schedule.start)
            end = datetime.combine(self.date, schedule.end)
            while current < end:
                times.add(current)
                current += SLOT_STEP
        return sorted(times)

    def options(self):
        result = []
        for start in self.starts():
            parts = self.at(start)
            if parts:
                result.append(parts)
        return result


def parse_items(company, raw):
    """[(кандидаты, услуга)] из [{'service': id, 'master': id | None}] или ValueError с текстом ошибки."""
    if not isinstance(raw, list) or not raw:
        raise ValueError('Выберите услуги')
    if len(raw) > MAX_VISIT_ITEMS:
        raise ValueError(f'В одном визите не больше {MAX_VISIT_ITEMS} услуг')
    items, chosen = [], []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Выберите услуги')
        service_id, master_id = item.get('service'), item.get('master')
        if not str(service_id).isdecimal() or (master_id not in (None, '', 0) and not str(master_id).isdecimal()):
            raise ValueError('Услуга не найдена.')
        service = Service.objects.filter(company=company, id=service_id, is_active=True).first()
        if not service:
            raise ValueError('Услуга не найдена.')
        masters = Master.objects.filter(company=company, is_active=True, services=service).order_by('id')
        if master_id:
            masters = masters.filter(id=master_id)
            if not masters:
                raise ValueError('Мастер не оказывает эту услугу.')
        # «Любой мастер»: сначала те, кого клиент уже выбрал в этом визите — услуги у одного мастера подряд
        candidates = sorted(masters, key=lambda m: (m.id not in chosen, m.id))
        if not candidates:
            raise ValueError('На эту услугу нет мастеров.')
        if master_id:
            chosen.append(candidates[0].id)
        items.append((candidates, service))
    return items


def parse_query_items(value):
    """?items=12,15:3 — услуга 12 у любого мастера, услуга 15 у мастера 3."""
    result = []
    for chunk in (value or '').split(','):
        if not chunk:
            continue
        service, _, master = chunk.partition(':')
        result.append({'service': service, 'master': master or None})
    return result


def part_data(part):
    master, service = part['master'], part['service']
    return {
        'master': master.id, 'master_name': master.name,
        'service': service.id, 'service_name': service.name,
        'start_time': part['start'].strftime('%H:%M'), 'end_time': part['end'].strftime('%H:%M'),
        'duration': int((part['end'] - part['start']).total_seconds() // 60),
        'price': str(part['price']),
    }


def appointment_part(appointment):
    """Часть визита из сохранённой записи (для ответа и страницы управления)."""
    from .terms import price_of
    return part_data({
        'master': appointment.master, 'service': appointment.service,
        'start': datetime.combine(appointment.date, appointment.start_time),
        'end': datetime.combine(appointment.date, appointment.end_time),
        'price': price_of(appointment),
    })


def option_data(parts):
    return {
        'start_time': parts[0]['start'].strftime('%H:%M'),
        'end_time': parts[-1]['end'].strftime('%H:%M'),
        'parts': [part_data(p) for p in parts],
    }


def visit_parts(appointment):
    """Все записи визита по порядку (одна запись — сама по себе визит)."""
    if not appointment.visit:
        return [appointment]
    return list(Appointment.objects.filter(visit=appointment.visit, company=appointment.company)
                .select_related('company', 'master', 'service').order_by('start_time', 'id'))


def chain_for_reschedule(parts, date):
    """Цепочка тех же мастеров и услуг на другую дату; сам визит время не занимает."""
    from .manage_views import visit_minutes
    from .terms import price_of
    items = [([p.master], p.service, visit_minutes(p), price_of(p)) for p in parts]
    return Chain(items, date, exclude_ids=[p.id for p in parts])
