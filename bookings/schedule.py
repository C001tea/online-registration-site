"""Рабочий день мастера на дату: часы и обед.

Порядок: выходной по дате (MasterDayOff) → рабочий день по дате (MasterWorkDay) →
недельный график (MasterSchedule). Обед — время, когда клиентам не предлагаются слоты.
"""
from dataclasses import dataclass
from datetime import time, timedelta
from typing import Optional

from .models import MasterDayOff, MasterSchedule, MasterWorkDay


@dataclass(frozen=True)
class DaySchedule:
    source: str  # template | custom | off | template_off
    start: Optional[time] = None
    end: Optional[time] = None
    break_start: Optional[time] = None
    break_end: Optional[time] = None

    @property
    def is_working(self):
        return self.start is not None

    @property
    def breaks(self):
        return [(self.break_start, self.break_end)] if self.break_start and self.break_end else []


def from_row(source, row):
    return DaySchedule(source, row.start_time, row.end_time, row.break_start, row.break_end)


def day_schedule(master, date):
    if MasterDayOff.objects.filter(master=master, date=date).exists():
        return DaySchedule('off')
    custom = MasterWorkDay.objects.filter(master=master, date=date).first()
    if custom:
        return from_row('custom', custom)
    template = MasterSchedule.objects.filter(master=master, day_of_week=date.weekday(), is_working=True).first()
    return from_row('template', template) if template else DaySchedule('template_off')


def days_schedule(master, date_from, date_to):
    """{дата: DaySchedule} за период — тремя запросами, для календаря на месяц."""
    days_off = set(MasterDayOff.objects.filter(master=master, date__gte=date_from, date__lte=date_to)
                   .values_list('date', flat=True))
    custom = {row.date: row for row in MasterWorkDay.objects.filter(master=master, date__gte=date_from, date__lte=date_to)}
    template = {row.day_of_week: row for row in MasterSchedule.objects.filter(master=master, is_working=True)}
    result = {}
    day = date_from
    while day <= date_to:
        if day in days_off:
            result[day] = DaySchedule('off')
        elif day in custom:
            result[day] = from_row('custom', custom[day])
        elif day.weekday() in template:
            result[day] = from_row('template', template[day.weekday()])
        else:
            result[day] = DaySchedule('template_off')
        day += timedelta(days=1)
    return result
