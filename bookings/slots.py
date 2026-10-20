from datetime import datetime, timedelta
from django.utils import timezone
from .models import Appointment, MasterSchedule, MasterDayOff

SLOT_STEP = timedelta(minutes=30)
# Предстоящие записи, которые ещё можно подтвердить или отменить
ACTIVE_STATUSES = ['pending', 'confirmed']
# Записи, которые занимают время мастера. completed — «клиент пришёл»: визит может идти
# прямо сейчас. cancelled и no_show время освобождают.
BUSY_STATUSES = ['pending', 'confirmed', 'completed']


def get_free_slots(master, date, duration_minutes, exclude_id=None, step=SLOT_STEP, include_past=False):
    """Свободное время начала записи (datetime.time) у мастера на дату.

    Учитывает график мастера, выходные по датам (MasterDayOff), уже занятые записи
    и то, что прошедшее время сегодняшнего дня (по TIME_ZONE) записать нельзя.

    exclude_id — запись, которую не считать занятой (перенос этой же записи);
    include_past=True — не отсекать прошедшие дату и время.
    """
    today = timezone.localdate()
    if date < today and not include_past:
        return []

    schedule = MasterSchedule.objects.filter(
        master=master, day_of_week=date.weekday(), is_working=True
    ).first()
    if not schedule:
        return []

    if MasterDayOff.objects.filter(master=master, date=date).exists():
        return []

    appointments = Appointment.objects.filter(master=master, date=date, status__in=BUSY_STATUSES)
    if exclude_id is not None:
        appointments = appointments.exclude(pk=exclude_id)
    busy = [
        (datetime.combine(date, start), datetime.combine(date, end))
        for start, end in appointments.values_list('start_time', 'end_time')
    ]
    now = timezone.localtime().replace(tzinfo=None) if date == today and not include_past else None

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
        current += step

    return free_slots
