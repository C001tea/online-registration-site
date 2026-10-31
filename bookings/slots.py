from datetime import datetime, timedelta
from django.utils import timezone
from .models import Appointment
from .schedule import day_schedule

SLOT_STEP = timedelta(minutes=30)
# Предстоящие записи, которые ещё можно подтвердить или отменить
ACTIVE_STATUSES = ['pending', 'confirmed']
# Записи, которые занимают время мастера. completed — «клиент пришёл»: визит может идти
# прямо сейчас. cancelled и no_show время освобождают.
BUSY_STATUSES = ['pending', 'confirmed', 'completed']


def get_free_slots(master, date, duration_minutes, exclude_id=None, step=SLOT_STEP, include_past=False):
    """Свободное время начала записи (datetime.time) у мастера на дату.

    Учитывает рабочий день мастера (bookings/schedule.py: выходные и график по датам,
    недельный график, обед), уже занятые записи и то, что прошедшее время сегодняшнего
    дня (по TIME_ZONE) записать нельзя.

    exclude_id — запись, которую не считать занятой (перенос этой же записи);
    include_past=True — не отсекать прошедшие дату и время.
    """
    today = timezone.localdate()
    if date < today and not include_past:
        return []

    schedule = day_schedule(master, date)
    if not schedule.is_working:
        return []

    appointments = Appointment.objects.filter(master=master, date=date, status__in=BUSY_STATUSES)
    if exclude_id is not None:
        appointments = appointments.exclude(pk=exclude_id)
    busy = [
        (datetime.combine(date, start), datetime.combine(date, end))
        for start, end in appointments.values_list('start_time', 'end_time')
    ]
    # Обед — как занятое время: запись не может начаться в обед или заходить на него
    busy += [(datetime.combine(date, start), datetime.combine(date, end)) for start, end in schedule.breaks]
    now = timezone.localtime().replace(tzinfo=None) if date == today and not include_past else None

    duration = timedelta(minutes=duration_minutes)
    current = datetime.combine(date, schedule.start)
    end = datetime.combine(date, schedule.end)

    free_slots = []
    while current + duration <= end:
        slot_end = current + duration
        is_past = now is not None and current <= now
        is_busy = any(busy_start < slot_end and busy_end > current for busy_start, busy_end in busy)
        if not is_past and not is_busy:
            free_slots.append(current.time())
        current += step

    return free_slots
