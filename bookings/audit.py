"""История изменений записи: кто, когда и что сделал (карточка записи → «История изменений»)."""
from .models import AppointmentLog

# Поля, изменения которых пишем в историю (значения — строки для показа)
TRACKED = ('status', 'master', 'service', 'date', 'start_time', 'end_time', 'price', 'discount',
           'buffer_minutes', 'client_name', 'client_phone', 'client_email', 'notes', 'booker_name', 'booker_phone')


def value_text(value):
    if value is None:
        return ''
    if hasattr(value, 'strftime') and hasattr(value, 'hour') and not hasattr(value, 'year'):
        return value.strftime('%H:%M')
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if hasattr(value, 'name'):
        return value.name
    return str(value)


def snapshot(appointment):
    return {field: value_text(getattr(appointment, field)) for field in TRACKED}


def diff(before, appointment):
    after = snapshot(appointment)
    return {field: [before[field], after[field]] for field in TRACKED if before[field] != after[field]}


def log(appointment, action, user=None, actor='staff', details=None):
    """action: created / updated / paid / payment_cancelled / reminded / repeated / service_added / cancelled_by_client
    / rescheduled_by_client."""
    if user is not None and not getattr(user, 'is_authenticated', False):
        user = None
    AppointmentLog.objects.create(appointment=appointment, action=action, user=user, actor=actor,
                                  details=details or {})


def row(entry):
    user = entry.user
    name = (user.get_full_name() or user.username) if user else ''
    return {
        'id': entry.id, 'at': entry.created_at.isoformat(), 'action': entry.action, 'actor': entry.actor,
        'user': name, 'details': entry.details,
    }
