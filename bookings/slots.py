from datetime import datetime, timedelta
from django.utils import timezone
from .models import Appointment, MasterSchedule

SLOT_STEP = timedelta(minutes=30)
ACTIVE_STATUSES = ['pending', 'confirmed']


def get_free_slots(master, date, duration_minutes):
    """Свободное время начала записи (datetime.time) у мастера на дату.

    Учитывает график мастера, уже занятые записи и то, что прошедшее
    время сегодняшнего дня (по TIME_ZONE) записать нельзя.
    """
    today = timezone.localdate()
    if date < today:
        return []

    schedule = MasterSchedule.objects.filter(
        master=master, day_of_week=date.weekday(), is_working=True
    ).first()
    if not schedule:
        return []

    busy = [
        (datetime.combine(date, start), datetime.combine(date, end))
        for start, end in Appointment.objects.filter(
            master=master, date=date, status__in=ACTIVE_STATUSES
        ).values_list('start_time', 'end_time')
    ]
    now = timezone.localtime().replace(tzinfo=None) if date == today else None

    duration = timedelta(minutes=duration_minutes)
    current = datetime.combine(date, schedule.start_time)
    end = datetime.combine(date, schedule.end_time)

    free_slots = []
    while current + duration <= end:
        slot_end = current + duration
        is_past = now is not None and current <= now
        is_busy = any(busy_start < slot_end and busy_end > current for busy_start, busy_end in busy)
        if not is_past and not is_busy:
            free_slots.append(current.time())
        current += SLOT_STEP

    return free_slots
