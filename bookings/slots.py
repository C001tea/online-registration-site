from collections import defaultdict
from datetime import datetime, time, timedelta
from django.utils import timezone
from .models import Appointment
from .schedule import day_schedule, days_schedule

SLOT_STEP = timedelta(minutes=30)
# Предстоящие записи, которые ещё можно подтвердить или отменить
ACTIVE_STATUSES = ['pending', 'confirmed']
# Записи, которые занимают время мастера. completed — «клиент пришёл»: визит может идти
# прямо сейчас. cancelled и no_show время освобождают.
BUSY_STATUSES = ['pending', 'confirmed', 'completed']


def local_now():
    return timezone.localtime().replace(tzinfo=None)


def free_starts(date, schedule, busy, duration_minutes, step=SLOT_STEP, now=None):
    """Свободное время начала (datetime.time) в рабочем дне schedule.

    busy — [(time начала, time конца)] занятых записей; обед из schedule добавляется сам;
    now — naive datetime: время до него (включительно) не предлагается.
    """
    if not schedule.is_working:
        return []
    intervals = [(datetime.combine(date, start), datetime.combine(date, end))
                 for start, end in list(busy) + schedule.breaks]
    duration = timedelta(minutes=duration_minutes)
    current = datetime.combine(date, schedule.start)
    end = datetime.combine(date, schedule.end)
    result = []
    while current + duration <= end:
        slot_end = current + duration
        is_past = now is not None and current <= now
        is_busy = any(b_start < slot_end and b_end > current for b_start, b_end in intervals)
        if not is_past and not is_busy:
            result.append(current.time())
        current += step
    return result


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
    busy = [(s, busy_end(e, b)) for s, e, b in appointments.values_list('start_time', 'end_time', 'buffer_minutes')]
    now = local_now() if date == today and not include_past else None
    return free_starts(date, schedule, busy, duration_minutes, step, now)


def busy_end(end, buffer_minutes):
    """Конец занятости мастера: конец записи плюс технический перерыв (не позже 23:59)."""
    if not buffer_minutes:
        return end
    minutes = min(end.hour * 60 + end.minute + buffer_minutes, 23 * 60 + 59)
    return time(minutes // 60, minutes % 60)


def busy_by_master_day(masters, date_from, date_to):
    """{(master_id, дата): [(начало, конец)]} — занятые записи одним запросом."""
    result = defaultdict(list)
    rows = Appointment.objects.filter(
        master__in=masters, date__gte=date_from, date__lte=date_to, status__in=BUSY_STATUSES,
    ).values_list('master_id', 'date', 'start_time', 'end_time', 'buffer_minutes')
    for master_id, day, start, end, buffer in rows:
        result[(master_id, day)].append((start, busy_end(end, buffer)))
    return result


def minutes_between(date, start, end):
    return max(0, (datetime.combine(date, end) - datetime.combine(date, start)).total_seconds() / 60)


def overlap_minutes(date, a_start, a_end, b_start, b_end):
    start = max(datetime.combine(date, a_start), datetime.combine(date, b_start))
    end = min(datetime.combine(date, a_end), datetime.combine(date, b_end))
    return max(0, (end - start).total_seconds() / 60)


def work_and_busy_minutes(date, schedule, busy):
    """Рабочие минуты дня (без обеда) и сколько из них занято записями."""
    work = minutes_between(date, schedule.start, schedule.end)
    for b_start, b_end in schedule.breaks:
        work -= overlap_minutes(date, schedule.start, schedule.end, b_start, b_end)
    taken = sum(overlap_minutes(date, schedule.start, schedule.end, start, end) for start, end in busy)
    return max(work, 0), min(taken, max(work, 0))


def month_overview(masters, date_from, date_to, duration_minutes=None, step=SLOT_STEP):
    """Загруженность дней для календаря с кружками.

    [{date, working, load (0..1 — доля занятого рабочего времени мастеров), free (число
    свободных времён начала для услуги длительностью duration_minutes; без неё — окна по 30 минут)}]
    Прошедшие дни: free = 0. Запросы: 3 на мастера + 1 на записи.
    """
    masters = list(masters)
    today = timezone.localdate()
    now = local_now()
    schedules = {m.id: days_schedule(m, date_from, date_to) for m in masters}
    busy = busy_by_master_day(masters, date_from, date_to)
    def duration_for(master):
        if callable(duration_minutes):
            return duration_minutes(master)
        return duration_minutes or 30

    durations = {m.id: duration_for(m) for m in masters}
    days = []
    day = date_from
    while day <= date_to:
        work_total = busy_total = 0
        starts = set()
        working = False
        for master in masters:
            schedule = schedules[master.id][day]
            if not schedule.is_working:
                continue
            working = True
            master_busy = busy.get((master.id, day), [])
            work, taken = work_and_busy_minutes(day, schedule, master_busy)
            work_total += work
            busy_total += taken
            if day >= today:
                starts.update(free_starts(day, schedule, master_busy, durations[master.id], step,
                                          now if day == today else None))
        days.append({
            'date': day.isoformat(),
            'working': working,
            'load': round(busy_total / work_total, 2) if work_total else 0,
            'free': len(starts),
        })
        day += timedelta(days=1)
    return days


def nearest_slots(master, duration_minutes, days=14, limit=4, step=SLOT_STEP):
    """Ближайший день с свободным временем у мастера и первые limit времён: (дата, [time]) или None."""
    today = timezone.localdate()
    date_to = today + timedelta(days=days - 1)
    schedules = days_schedule(master, today, date_to)
    busy = busy_by_master_day([master], today, date_to)
    now = local_now()
    day = today
    while day <= date_to:
        slots = free_starts(day, schedules[day], busy.get((master.id, day), []), duration_minutes, step,
                            now if day == today else None)
        if slots:
            return day, slots[:limit]
        day += timedelta(days=1)
    return None


def parse_month(value):
    """'YYYY-MM' → (первый, последний день месяца) или None."""
    try:
        first = datetime.strptime(value or '', '%Y-%m').date()
    except ValueError:
        return None
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, next_month - timedelta(days=1)
