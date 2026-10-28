"""Расчёты финансов салона: оплата визита, отчёт за период, касса за день, зарплаты."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from bookings.models import Appointment
from dashboard.errors import StaffError
from .models import METHOD_CHOICES, CashOperation, Payment, SalaryPayout, SalaryScheme

ZERO = Decimal('0')
CENT = Decimal('0.01')
METHODS = [key for key, _ in METHOD_CHOICES]
SALARY_CATEGORY = 'Зарплата'
# Подсказки в форме расхода/прихода (можно вписать свою категорию)
EXPENSE_CATEGORIES = ['Аренда', 'Материалы', 'Коммунальные услуги', 'Реклама', 'Оборудование', 'Налоги', 'Прочее']
INCOME_CATEGORIES = ['Продажа товаров', 'Внесение в кассу', 'Прочее']


def money(value):
    return (value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP)


def total(queryset, field='amount'):
    return money(queryset.aggregate(s=Sum(field))['s'])


# --- Оплата визита ---

def checkout(appointment, *, method, discount, user):
    """Оплата визита: сумма = цена услуги − скидка, визит отмечается «Пришёл»."""
    if method not in METHODS:
        raise StaffError('Способ оплаты: наличные, карта или перевод')
    if appointment.status == 'cancelled':
        raise StaffError('Отменённую запись нельзя оплатить')
    if appointment.date > timezone.localdate():
        raise StaffError('Будущий визит нельзя оплатить')
    if hasattr(appointment, 'payment'):
        raise StaffError('Визит уже оплачен')
    price = money(appointment.service.price if appointment.service else ZERO)
    discount = money(discount)
    if discount < 0 or discount > price:
        raise StaffError('Скидка — от 0 до цены услуги')
    with transaction.atomic():
        payment = Payment.objects.create(
            company=appointment.company, appointment=appointment, master=appointment.master,
            date=appointment.date, price=price, discount=discount, amount=price - discount,
            method=method, created_by=user,
        )
        if appointment.status != 'completed':
            appointment.status = 'completed'
            appointment.save(update_fields=['status'])
    return payment


# --- Отчёт за период ---

def summary(company, date_from, date_to):
    payments = Payment.objects.filter(company=company, date__gte=date_from, date__lte=date_to)
    operations = CashOperation.objects.filter(company=company, date__gte=date_from, date__lte=date_to)
    revenue = total(payments)
    incomes = total(operations.filter(kind='income'))
    expenses_qs = operations.filter(kind='expense')
    expenses = total(expenses_qs)
    salaries = total(expenses_qs.filter(salary_payout__isnull=False))

    by_master = [
        {'id': row['master'], 'name': row['master__name'] or 'Без мастера',
         'visits': row['visits'], 'revenue': str(money(row['revenue']))}
        for row in payments.values('master', 'master__name')
        .annotate(visits=Count('id'), revenue=Sum('amount')).order_by('-revenue')
    ]
    by_day = [
        {'date': row['date'].isoformat(), 'visits': row['visits'], 'revenue': str(money(row['revenue']))}
        for row in payments.values('date').annotate(visits=Count('id'), revenue=Sum('amount')).order_by('date')
    ]
    expense_categories = [
        {'category': row['category'], 'amount': str(money(row['s']))}
        for row in expenses_qs.values('category').annotate(s=Sum('amount')).order_by('-s')
    ]
    completed = Appointment.objects.filter(company=company, date__gte=date_from, date__lte=date_to)
    return {
        'date_from': date_from.isoformat(),
        'date_to': date_to.isoformat(),
        'revenue': str(revenue),
        'visits': payments.count(),
        'average_check': str(money(revenue / payments.count())) if payments.exists() else '0.00',
        'discounts': str(total(payments, 'discount')),
        'by_method': {method: str(total(payments.filter(method=method))) for method in METHODS},
        'other_income': str(incomes),
        'expenses': str(expenses),
        'salaries': str(salaries),
        'profit': str(revenue + incomes - expenses),
        'no_shows': completed.filter(status='no_show').count(),
        'unpaid_visits': completed.filter(status='completed', payment__isnull=True).count(),
        'by_master': by_master,
        'by_day': by_day,
        'expense_categories': expense_categories,
    }


# --- Касса за день ---

def cash_balance_before(company, day):
    """Наличные в кассе на начало дня: все наличные оплаты и приходы минус расходы до этой даты."""
    payments = total(Payment.objects.filter(company=company, method='cash', date__lt=day))
    ops = CashOperation.objects.filter(company=company, method='cash', date__lt=day)
    return payments + total(ops.filter(kind='income')) - total(ops.filter(kind='expense'))


def cash_day(company, day):
    payments = Payment.objects.filter(company=company, date=day)
    ops = CashOperation.objects.filter(company=company, date=day)
    opening = cash_balance_before(company, day)
    cash_in = total(payments.filter(method='cash')) + total(ops.filter(kind='income', method='cash'))
    cash_out = total(ops.filter(kind='expense', method='cash'))
    return {
        'date': day.isoformat(),
        'opening': str(opening),
        'cash_in': str(cash_in),
        'cash_out': str(cash_out),
        'closing': str(opening + cash_in - cash_out),
        'by_method': {method: str(total(payments.filter(method=method))) for method in METHODS},
        'payments': [payment_row(p) for p in payments.select_related('appointment', 'master')
                     .order_by('appointment__start_time', 'id')],
        'operations': [operation_row(o) for o in ops.select_related('salary_payout').order_by('created_at')],
    }


def payment_row(p):
    a = p.appointment
    return {
        'id': p.id, 'appointment': a.id, 'time': a.start_time.strftime('%H:%M'), 'client_name': a.client_name,
        'master': p.master.name if p.master else None, 'price': str(p.price), 'discount': str(p.discount),
        'amount': str(p.amount), 'method': p.method,
    }


def operation_row(o):
    return {
        'id': o.id, 'date': o.date.isoformat(), 'kind': o.kind, 'amount': str(o.amount), 'method': o.method,
        'category': o.category, 'comment': o.comment,
        'salary_payout': getattr(o, 'salary_payout', None) is not None,
    }


# --- Зарплаты ---

def scheme_of(master):
    scheme = SalaryScheme.objects.filter(master=master).first()
    return scheme or SalaryScheme(master=master)


def salary_row(master, date_from, date_to):
    """Начислено за период: % от оплаченных визитов мастера + фикс × дни с оплаченными визитами."""
    scheme = scheme_of(master)
    payments = Payment.objects.filter(master=master, date__gte=date_from, date__lte=date_to)
    revenue = total(payments)
    days = payments.values('date').distinct().count()
    percent_part = money(revenue * scheme.percent / 100)
    fixed_part = money(scheme.fixed_per_day * days)
    accrued = percent_part + fixed_part
    payouts = SalaryPayout.objects.filter(master=master, date_from__gte=date_from, date_to__lte=date_to)
    paid = total(payouts)
    return {
        'master': {'id': master.id, 'name': master.name, 'is_active': master.is_active},
        'scheme': {'percent': str(scheme.percent), 'fixed_per_day': str(scheme.fixed_per_day)},
        'visits': payments.count(),
        'days': days,
        'revenue': str(revenue),
        'percent_part': str(percent_part),
        'fixed_part': str(fixed_part),
        'accrued': str(accrued),
        'paid': str(paid),
        'due': str(accrued - paid),
        'payouts': [
            {'id': p.id, 'date_from': p.date_from.isoformat(), 'date_to': p.date_to.isoformat(),
             'amount': str(p.amount), 'created_at': p.created_at.isoformat()}
            for p in payouts.order_by('created_at')
        ],
    }


def salary_visits(master, date_from, date_to):
    payments = (Payment.objects.filter(master=master, date__gte=date_from, date__lte=date_to)
                .select_related('appointment', 'appointment__service').order_by('date', 'appointment__start_time'))
    return [
        {'date': p.date.isoformat(), 'time': p.appointment.start_time.strftime('%H:%M'),
         'service': p.appointment.service.name if p.appointment.service else None, 'amount': str(p.amount)}
        for p in payments
    ]


def pay_salary(master, *, date_from, date_to, amount, method, user):
    with transaction.atomic():
        operation = CashOperation.objects.create(
            company=master.company, date=timezone.localdate(), kind='expense', amount=amount, method=method,
            category=SALARY_CATEGORY, comment=f'{master.name}, {date_from:%d.%m}–{date_to:%d.%m}', created_by=user,
        )
        return SalaryPayout.objects.create(
            company=master.company, master=master, date_from=date_from, date_to=date_to,
            amount=amount, operation=operation, created_by=user,
        )
