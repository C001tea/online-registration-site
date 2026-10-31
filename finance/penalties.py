"""Чёрный список: штрафы за неявку и позднюю отмену.

Правила салона (Company): penalty_percent — штраф в % от цены услуги (0 — штрафы выключены),
late_cancel_hours — отмена клиентом позже этого срока до визита тоже штрафуется (0 — нет).
Штраф оплачивается на месте при следующем визите (приход в кассе) или прощается. В чёрном
списке клиент остаётся навсегда, пока у него есть не прощённые штрафы.
"""
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from .models import CashOperation, Penalty

CENT = Decimal('0.01')
CATEGORY = {'no_show': 'Штраф за неявку', 'late_cancel': 'Штраф за позднюю отмену'}
ACTIVE = ('unpaid', 'paid')  # прощённые штрафы из чёрного списка выводят


def starts_at(appointment):
    return timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))


def amount_for(appointment):
    percent = appointment.company.penalty_percent
    if not percent or appointment.service is None:
        return Decimal('0')
    # Decimal(str(...)): у только что созданной услуги цена может быть ещё int, а не Decimal
    return (Decimal(str(appointment.service.price)) * percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def is_late_cancel(appointment, now=None):
    """Отмена сейчас — позже, чем за late_cancel_hours до начала (в т.ч. после начала)."""
    hours = appointment.company.late_cancel_hours
    if not hours or not appointment.company.penalty_percent:
        return False
    return starts_at(appointment) - (now or timezone.now()) < timedelta(hours=hours)


def late_cancel_amount(appointment, now=None):
    """Сколько составит штраф, если клиент отменит сейчас (None — штрафа не будет)."""
    if appointment.status not in ('pending', 'confirmed') or not is_late_cancel(appointment, now):
        return None
    amount = amount_for(appointment)
    return amount if amount > 0 else None


def create(appointment, kind, user=None):
    """Штраф по записи (один на запись). Нулевой (нет цены или штрафы выключены) не создаётся."""
    amount = amount_for(appointment)
    if amount <= 0 or Penalty.objects.filter(appointment=appointment).exists():
        return None
    return Penalty.objects.create(
        company=appointment.company, appointment=appointment, phone=appointment.client_phone,
        kind=kind, amount=amount, created_by=user if user and user.is_authenticated else None,
    )


def remove_unpaid(appointment):
    """Статус записи исправили — неоплаченный штраф по ней снимается. Оплаченный не трогаем."""
    Penalty.objects.filter(appointment=appointment, status='unpaid').delete()


def paid_penalty(appointment):
    return Penalty.objects.filter(appointment=appointment, status='paid').first()


def blacklist(company, phones):
    """{телефон: {'debt': Decimal, 'count': штрафов в чёрном списке, 'unpaid': неоплаченных}}."""
    phones = [p for p in set(phones) if p]
    if not phones:
        return {}
    rows = (Penalty.objects.filter(company=company, phone__in=phones, status__in=ACTIVE)
            .values('phone').annotate(count=Count('id'), unpaid=Count('id', filter=Q(status='unpaid')),
                                      debt=Sum('amount', filter=Q(status='unpaid'))))
    return {r['phone']: {'debt': (r['debt'] or Decimal('0')).quantize(CENT), 'count': r['count'],
                         'unpaid': r['unpaid']} for r in rows}


def blacklist_data(info):
    """Для ответов API: {'debt': '100.00', 'count': 2} или None (не в чёрном списке)."""
    if not info:
        return None
    return {'debt': str(info['debt']), 'count': info['count']}


def unpaid(company, phone):
    return Penalty.objects.filter(company=company, phone=phone, status='unpaid').select_related('appointment')


def penalty_row(p):
    a = p.appointment
    return {
        'id': p.id, 'kind': p.kind, 'status': p.status, 'amount': str(p.amount),
        'date': a.date.isoformat(), 'time': a.start_time.strftime('%H:%M'),
        'service': a.service.name if a.service else None, 'master': a.master.name if a.master else None,
        'created_at': p.created_at.isoformat(), 'resolved_at': p.resolved_at.isoformat() if p.resolved_at else None,
        'note': p.note,
    }


def pay(penalties, *, method, user, day=None):
    """Оплата штрафов на месте: каждый — приход в кассе. Возвращает сумму."""
    total = Decimal('0')
    now = timezone.now()
    with transaction.atomic():
        for penalty in penalties:
            penalty = Penalty.objects.select_for_update().get(pk=penalty.pk)
            if penalty.status != 'unpaid':
                continue
            a = penalty.appointment
            penalty.operation = CashOperation.objects.create(
                company=penalty.company, date=day or timezone.localdate(), kind='income', amount=penalty.amount,
                method=method, category=CATEGORY[penalty.kind],
                comment=f'{a.client_name}, запись {a.date:%d.%m} {a.start_time:%H:%M}', created_by=user,
            )
            penalty.status, penalty.resolved_at, penalty.resolved_by = 'paid', now, user
            penalty.save(update_fields=['operation', 'status', 'resolved_at', 'resolved_by'])
            total += penalty.amount
    return total


def cancel_payment(penalty):
    """Отменить оплату штрафа (ошибка кассира): приход удаляется, штраф снова не оплачен."""
    with transaction.atomic():
        if penalty.operation_id:
            CashOperation.objects.filter(pk=penalty.operation_id).delete()
        penalty.operation, penalty.status, penalty.resolved_at, penalty.resolved_by = None, 'unpaid', None, None
        penalty.save(update_fields=['operation', 'status', 'resolved_at', 'resolved_by'])


def forgive(penalty, *, user, note=''):
    penalty.status, penalty.resolved_at, penalty.resolved_by = 'forgiven', timezone.now(), user
    penalty.note = note[:300]
    penalty.save(update_fields=['status', 'resolved_at', 'resolved_by', 'note'])
