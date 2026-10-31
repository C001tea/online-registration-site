from decimal import Decimal

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from bookings.models import Appointment, Company, Master

METHOD_CHOICES = [
    ('cash', 'Наличные'),
    ('card', 'Карта'),
    ('transfer', 'Перевод'),
]


class Payment(models.Model):
    """Оплата визита (одна на запись). Цена и мастер — на момент оплаты."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='payments')
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name='payment')
    master = models.ForeignKey(Master, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    date = models.DateField()  # дата визита — по ней отчёты и зарплаты
    price = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # price - discount
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        indexes = [models.Index(fields=['company', 'date'])]

    def __str__(self):
        return f'{self.appointment} — {self.amount} ({self.method})'


class CashOperation(models.Model):
    """Приход или расход по кассе вне визитов: аренда, материалы, выплата зарплаты…"""
    KIND_CHOICES = [('income', 'Приход'), ('expense', 'Расход')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='cash_operations')
    date = models.DateField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='cash')
    category = models.CharField(max_length=100)
    comment = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        indexes = [models.Index(fields=['company', 'date'])]

    def __str__(self):
        return f'{self.date} {self.get_kind_display()} {self.amount} — {self.category}'


class SalaryScheme(models.Model):
    """Схема оплаты мастера: процент от оплаченных визитов и/или фикс за рабочий день."""
    master = models.OneToOneField(Master, on_delete=models.CASCADE, related_name='salary_scheme')
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0'),
                                  validators=[MinValueValidator(0), MaxValueValidator(100)])
    fixed_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'),
                                        validators=[MinValueValidator(0)])


class SalaryPayout(models.Model):
    """Выплата зарплаты мастеру за период; проводится расходом по кассе."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='salary_payouts')
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='salary_payouts')
    date_from = models.DateField()
    date_to = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    operation = models.OneToOneField(CashOperation, on_delete=models.CASCADE, related_name='salary_payout')
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')


class Penalty(models.Model):
    """Штраф клиента (чёрный список): не пришёл или отменил запись слишком поздно.

    Клиент — номер телефона в салоне. Оплачивается на месте при следующем визите (доход в кассе),
    может быть прощён. Срока давности нет: клиент остаётся в чёрном списке, пока у него есть
    неотменённые (не прощённые) штрафы.
    """
    KIND_CHOICES = [('no_show', 'Не пришёл'), ('late_cancel', 'Поздняя отмена')]
    STATUS_CHOICES = [('unpaid', 'Не оплачен'), ('paid', 'Оплачен'), ('forgiven', 'Прощён')]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='penalties')
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE, related_name='penalty')
    phone = models.CharField(max_length=20, db_index=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='unpaid')
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=300, blank=True)
    # Приход в кассе при оплате
    operation = models.OneToOneField(CashOperation, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='penalty')

    class Meta:
        indexes = [models.Index(fields=['company', 'phone'])]

    def __str__(self):
        return f'{self.phone} — {self.amount} ({self.status})'
