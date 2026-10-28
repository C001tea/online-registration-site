from datetime import time, timedelta
from decimal import Decimal

from bookings.models import Appointment
from dashboard.tests import StaffTestCase
from .models import CashOperation, Payment, SalaryPayout


class FinanceTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.today  # оплачивают визиты сегодня и в прошлом

    def visit(self, start=time(10), master=None, status='confirmed', day=None):
        return self.appointment(master=master, start=start, end=time(start.hour + 1), status=status, day=day or self.today)

    def pay(self, appointment, method='cash', discount=0):
        return self.api.post(f'/api/staff/appointments/{appointment.id}/checkout/',
                             {'method': method, 'discount': discount}, format='json')

    def period(self, url, date_from=None, date_to=None):
        date_from = date_from or self.today
        return self.api.get(url, {'date_from': date_from.isoformat(), 'date_to': (date_to or date_from).isoformat()})


class CheckoutTests(FinanceTestCase):
    def test_admin_checks_out_visit(self):
        self.auth(self.admin)
        visit = self.visit()
        response = self.pay(visit, method='card', discount='20')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['amount'], '180.00')  # цена услуги 200 − скидка 20
        visit.refresh_from_db()
        self.assertEqual(visit.status, 'completed')
        payment = Payment.objects.get()
        self.assertEqual((payment.method, payment.master, payment.date), ('card', self.master, self.today))
        # В журнале видна оплата
        journal = self.api.get('/api/staff/appointments/', {'date_from': self.today.isoformat()}).data
        self.assertEqual(journal[0]['payment'], {'amount': '180.00', 'discount': '20.00', 'method': 'card'})

    def test_rules(self):
        self.auth(self.admin)
        self.assertEqual(self.pay(self.visit(status='cancelled')).status_code, 400)
        future = self.appointment(day=self.today + timedelta(days=1))
        self.assertEqual(self.pay(future).status_code, 400)
        visit = self.visit(start=time(12))
        self.assertEqual(self.pay(visit, discount='250').status_code, 400)  # скидка больше цены
        self.assertEqual(self.pay(visit, method='bitcoin').status_code, 400)
        self.assertEqual(self.pay(visit).status_code, 201)
        response = self.pay(visit)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error'], 'Визит уже оплачен')

    def test_master_cannot_checkout_and_other_salon_hidden(self):
        visit = self.visit()
        self.auth(self.master_user)
        self.assertEqual(self.pay(visit).status_code, 403)
        self.auth(self.owner_b)
        self.assertEqual(self.pay(visit).status_code, 404)

    def test_paid_visit_is_locked_until_owner_cancels_payment(self):
        self.auth(self.admin)
        visit = self.visit()
        self.pay(visit)
        url = f'/api/staff/appointments/{visit.id}/'
        response = self.api.patch(url, {'status': 'confirmed'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('оплачен', response.data['error'])
        self.assertEqual(self.api.patch(url, {'start_time': '15:00'}, format='json').status_code, 400)
        self.assertEqual(self.api.patch(url, {'notes': 'кофе'}, format='json').status_code, 200)
        # Отменить оплату может только владелец
        self.assertEqual(self.api.delete(f'{url}checkout/').status_code, 403)
        self.auth(self.owner)
        self.assertEqual(self.api.delete(f'{url}checkout/').status_code, 204)
        self.assertEqual(self.api.patch(url, {'status': 'confirmed'}, format='json').status_code, 200)


class ReportTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)
        self.pay(self.visit(start=time(10)), method='cash')
        self.pay(self.visit(start=time(12)), method='card', discount='50')
        self.pay(self.visit(start=time(10), master=self.master2), method='transfer')
        self.visit(start=time(15), status='completed')  # пришёл, но не оплачен
        self.visit(start=time(16), status='no_show')
        self.auth(self.owner)

    def test_summary(self):
        self.api.post('/api/staff/finance/operations/', {
            'date': self.today.isoformat(), 'kind': 'expense', 'amount': '100', 'category': 'Материалы'}, format='json')
        data = self.period('/api/staff/finance/summary/').data
        self.assertEqual(data['revenue'], '550.00')  # 200 + 150 + 200
        self.assertEqual(data['visits'], 3)
        self.assertEqual(data['discounts'], '50.00')
        self.assertEqual(data['by_method'], {'cash': '200.00', 'card': '150.00', 'transfer': '200.00'})
        self.assertEqual(data['expenses'], '100.00')
        self.assertEqual(data['profit'], '450.00')
        self.assertEqual(data['no_shows'], 1)
        self.assertEqual(data['unpaid_visits'], 1)
        self.assertEqual([(m['name'], m['revenue']) for m in data['by_master']], [('Анна a', '350.00'), ('Мария', '200.00')])
        self.assertEqual(data['expense_categories'], [{'category': 'Материалы', 'amount': '100.00'}])

    def test_only_owner_sees_finance(self):
        for user in (self.admin, self.master_user):
            self.auth(user)
            for url in ('/api/staff/finance/summary/', '/api/staff/finance/salaries/', '/api/staff/finance/operations/'):
                self.assertEqual(self.period(url).status_code, 403, (user.username, url))
        self.auth(self.owner_b)
        self.assertEqual(self.period('/api/staff/finance/summary/').data['revenue'], '0.00')

    def test_cash_day_balance(self):
        yesterday = self.today - timedelta(days=1)
        CashOperation.objects.create(company=self.company, date=yesterday, kind='income', amount=Decimal('1000'),
                                     method='cash', category='Внесение в кассу')
        self.api.post('/api/staff/finance/operations/', {
            'date': self.today.isoformat(), 'kind': 'expense', 'amount': '300', 'category': 'Аренда'}, format='json')
        self.api.post('/api/staff/finance/operations/', {
            'date': self.today.isoformat(), 'kind': 'expense', 'amount': '40', 'method': 'card', 'category': 'Реклама'},
            format='json')
        data = self.api.get('/api/staff/finance/cash/', {'date': self.today.isoformat()}).data
        self.assertEqual(data['opening'], '1000.00')
        self.assertEqual(data['cash_in'], '200.00')   # только наличная оплата визита
        self.assertEqual(data['cash_out'], '300.00')  # расход картой не из кассы
        self.assertEqual(data['closing'], '900.00')
        self.assertEqual(len(data['payments']), 3)
        self.assertEqual(len(data['operations']), 2)

    def test_operation_validation_and_delete(self):
        url = '/api/staff/finance/operations/'
        bad = self.api.post(url, {'date': self.today.isoformat(), 'kind': 'expense', 'amount': '0', 'category': 'x'}, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertIn('error', bad.data)
        created = self.api.post(url, {'date': self.today.isoformat(), 'kind': 'income', 'amount': '10', 'category': 'Прочее'},
                                format='json').data
        self.assertEqual(self.api.delete(f'{url}{created["id"]}/').status_code, 204)
        self.auth(self.owner_b)
        other = CashOperation.objects.create(company=self.company, date=self.today, kind='income', amount=1, category='x')
        self.assertEqual(self.api.delete(f'{url}{other.id}/').status_code, 404)


class SalaryTests(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)
        yesterday = self.today - timedelta(days=1)
        self.pay(self.visit(start=time(10), day=yesterday))
        self.pay(self.visit(start=time(12), day=yesterday), discount='100')
        self.pay(self.visit(start=time(10)))
        self.auth(self.owner)
        self.week_from = self.today - timedelta(days=6)

    def salaries(self):
        return {row['master']['name']: row for row in self.period('/api/staff/finance/salaries/', self.week_from, self.today).data}

    def test_percent_and_fixed_per_day(self):
        url = f'/api/staff/finance/salaries/{self.master.id}/scheme/'
        self.assertEqual(self.api.put(url, {'percent': '40', 'fixed_per_day': '100'}, format='json').status_code, 200)
        row = self.salaries()['Анна a']
        self.assertEqual(row['revenue'], '500.00')       # 200 + 100 + 200
        self.assertEqual(row['percent_part'], '200.00')  # 40 %
        self.assertEqual(row['days'], 2)
        self.assertEqual(row['fixed_part'], '200.00')    # 2 дня × 100
        self.assertEqual(row['accrued'], '400.00')
        self.assertEqual(row['due'], '400.00')
        self.assertEqual(self.salaries()['Мария']['accrued'], '0.00')  # схема не задана
        self.assertEqual(self.api.put(url, {'percent': '120', 'fixed_per_day': '0'}, format='json').status_code, 400)

    def test_payout_creates_cash_expense_and_can_be_undone(self):
        self.api.put(f'/api/staff/finance/salaries/{self.master.id}/scheme/', {'percent': '50', 'fixed_per_day': '0'}, format='json')
        response = self.api.post(f'/api/staff/finance/salaries/{self.master.id}/payouts/', {
            'date_from': self.week_from.isoformat(), 'date_to': self.today.isoformat(), 'amount': '250'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        row = self.salaries()['Анна a']
        self.assertEqual((row['paid'], row['due']), ('250.00', '0.00'))
        operation = CashOperation.objects.get()
        self.assertEqual((operation.kind, operation.category, operation.amount), ('expense', 'Зарплата', Decimal('250')))
        summary = self.period('/api/staff/finance/summary/', self.week_from, self.today).data
        self.assertEqual(summary['salaries'], '250.00')
        # Выплату нельзя удалить как обычный расход — только отменить в зарплатах
        self.assertEqual(self.api.delete(f'/api/staff/finance/operations/{operation.id}/').status_code, 400)
        payout = SalaryPayout.objects.get()
        self.assertEqual(self.api.delete(f'/api/staff/finance/payouts/{payout.id}/').status_code, 204)
        self.assertFalse(CashOperation.objects.exists())

    def test_master_sees_only_own_salary(self):
        self.api.put(f'/api/staff/finance/salaries/{self.master.id}/scheme/', {'percent': '30', 'fixed_per_day': '0'}, format='json')
        self.auth(self.master_user)
        data = self.period('/api/staff/finance/my-salary/', self.week_from, self.today).data
        self.assertEqual(data['master']['name'], 'Анна a')
        self.assertEqual(data['accrued'], '150.00')
        self.assertEqual(len(data['visits_list']), 3)
        self.auth(self.admin)  # у админа нет профиля мастера
        self.assertEqual(self.period('/api/staff/finance/my-salary/').status_code, 404)

    def test_hidden_master_with_payments_stays_in_list(self):
        Appointment.objects.update(master=self.master)
        self.master.is_active = False
        self.master.save()
        self.assertIn('Анна a', self.salaries())
