"""Аудит финансов: касса по дню оплаты, «Внесение в кассу» не прибыль, зарплата за пересекающийся период,
оплата визита «Не пришёл» снимает штраф, повторная оплата не падает."""
from datetime import time, timedelta
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError

from bookings.models import Appointment
from .models import CashOperation, Payment, Penalty
from .tests import FinanceTestCase


class FinanceAuditTests(FinanceTestCase):
    def test_late_payment_goes_to_cash_of_payment_day(self):
        """Вчерашний визит оплатили сегодня: деньги в кассе сегодня, вчерашняя касса не меняется."""
        self.auth(self.owner)
        yesterday = self.today - timedelta(days=1)
        visit = self.visit(day=yesterday)
        self.assertEqual(self.pay(visit).status_code, 201)
        today = self.api.get('/api/staff/finance/cash/', {'date': self.today.isoformat()}).data
        before = self.api.get('/api/staff/finance/cash/', {'date': yesterday.isoformat()}).data
        self.assertEqual(today['cash_in'], '200.00')
        self.assertEqual(today['payments'][0]['visit_date'], yesterday.isoformat())
        self.assertEqual(before['cash_in'], '0.00')
        self.assertEqual(before['payments'], [])
        # Выручка в отчёте и зарплата — по дате визита
        summary = self.period('/api/staff/finance/summary/', yesterday).data
        self.assertEqual(summary['revenue'], '200.00')

    def test_cash_deposit_is_not_profit(self):
        self.auth(self.owner)
        for kind, category in (('income', 'Внесение в кассу'), ('expense', 'Изъятие из кассы'),
                               ('income', 'Продажа товаров')):
            self.api.post('/api/staff/finance/operations/', {
                'date': self.today.isoformat(), 'kind': kind, 'amount': '100', 'category': category}, format='json')
        summary = self.period('/api/staff/finance/summary/').data
        self.assertEqual(summary['other_income'], '100.00')  # только продажа товаров
        self.assertEqual(summary['expenses'], '0.00')
        self.assertEqual(summary['profit'], '100.00')
        # В кассе внесение и изъятие видны
        cash = self.api.get('/api/staff/finance/cash/', {'date': self.today.isoformat()}).data
        self.assertEqual((cash['cash_in'], cash['cash_out']), ('200.00', '100.00'))

    def test_overlapping_payout_is_shown(self):
        """Выплата за 1–15 видна и при просмотре 10–20 — чтобы не заплатить дважды."""
        self.auth(self.owner)
        start = self.today - timedelta(days=20)
        self.api.post(f'/api/staff/finance/salaries/{self.master.id}/payouts/', {
            'date_from': start.isoformat(), 'date_to': (start + timedelta(days=14)).isoformat(), 'amount': '500'},
            format='json')
        rows = self.period('/api/staff/finance/salaries/', start + timedelta(days=9), start + timedelta(days=19)).data
        row = next(r for r in rows if r['master']['id'] == self.master.id)
        self.assertEqual(row['paid'], '0.00')
        self.assertEqual(len(row['overlapping_payouts']), 1)
        self.assertEqual(row['overlapping_payouts'][0]['amount'], '500.00')

    def test_paying_no_show_removes_unpaid_penalty(self):
        self.auth(self.admin)
        visit = self.visit()
        Penalty.objects.create(company=self.company, appointment=visit, phone=visit.client_phone, kind='no_show',
                               amount=Decimal('100'))
        Appointment.objects.filter(pk=visit.pk).update(status='no_show')
        self.assertEqual(self.pay(visit).status_code, 201)
        self.assertFalse(Penalty.objects.filter(appointment=visit).exists())

    def test_double_checkout_returns_error_not_500(self):
        self.auth(self.admin)
        visit = self.visit()
        self.assertEqual(self.pay(visit).status_code, 201)
        self.assertEqual(self.pay(visit).status_code, 400)
        # Гонка: проверка прошла, а запись оплаты уже создана другим запросом
        Payment.objects.all().delete()
        Appointment.objects.filter(pk=visit.pk).update(status='confirmed')
        with mock.patch('finance.services.Payment.objects.create', side_effect=IntegrityError):
            response = self.pay(visit)
        self.assertEqual(response.status_code, 400)
        self.assertIn('уже оплачен', response.data['error'])

    def test_penalty_paid_at_checkout_is_cash_of_payment_day(self):
        self.auth(self.admin)
        old = self.visit(day=self.today - timedelta(days=10))
        Penalty.objects.create(company=self.company, appointment=old, phone=old.client_phone, kind='no_show',
                               amount=Decimal('50'))
        yesterday_visit = self.visit(day=self.today - timedelta(days=1))
        response = self.api.post(f'/api/staff/appointments/{yesterday_visit.id}/checkout/',
                                 {'method': 'cash', 'pay_penalties': True}, format='json')
        self.assertEqual(response.data['penalties_paid'], '50.00')
        self.assertEqual(CashOperation.objects.get(category='Штраф за неявку').date, self.today)
