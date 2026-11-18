"""Мастера работают сами (Company.masters_independent): оплата, выручка, клиенты и записи мастера."""
from datetime import time, timedelta
from unittest import mock

from django.utils import timezone

from .models import Payment
from .tests import FinanceTestCase


class IndependentTestCase(FinanceTestCase):
    def setUp(self):
        super().setUp()
        self.company.masters_independent = True
        self.company.save()

    def unpay(self, appointment):
        return self.api.delete(f'/api/staff/appointments/{appointment.id}/checkout/')


class MasterCheckoutTests(IndependentTestCase):
    def test_master_checks_out_own_visit(self):
        self.auth(self.master_user)
        visit = self.visit()
        response = self.pay(visit, method='card')
        self.assertEqual(response.status_code, 201, response.data)
        payment = Payment.objects.get()
        self.assertEqual((payment.method, payment.master, payment.created_by), ('card', self.master, self.master_user))

    def test_master_cannot_touch_other_masters_visit(self):
        other = self.visit(master=self.master2)
        self.auth(self.master_user)
        self.assertEqual(self.pay(other).status_code, 404)

    def test_mode_off_master_cannot_checkout(self):
        self.company.masters_independent = False
        self.company.save()
        self.auth(self.master_user)
        self.assertEqual(self.pay(self.visit()).status_code, 403)

    def test_master_cancels_todays_payment_only(self):
        self.auth(self.master_user)
        visit = self.visit()
        self.pay(visit)
        self.assertEqual(self.unpay(visit).status_code, 204)
        self.assertFalse(Payment.objects.exists())
        # Оплата, принятая вчера, — только владелец
        self.pay(visit)
        Payment.objects.update(created_at=timezone.now() - timedelta(days=1))
        response = self.unpay(visit)
        self.assertEqual(response.status_code, 403)
        self.assertIn('владелец', response.data['error'])
        self.auth(self.owner)
        self.assertEqual(self.unpay(visit).status_code, 204)

    def test_admin_still_cannot_cancel_payment(self):
        self.auth(self.admin)
        visit = self.visit()
        self.pay(visit)
        self.assertEqual(self.unpay(visit).status_code, 403)


class MyRevenueTests(IndependentTestCase):
    URL = '/api/staff/finance/my-revenue/'

    def test_revenue_by_method_and_unpaid(self):
        self.auth(self.master_user)
        self.pay(self.visit(start=time(10)), method='cash')
        self.pay(self.visit(start=time(12)), method='card', discount='50')
        unpaid = self.visit(start=time(14))
        self.pay(self.visit(master=self.master2, start=time(10)))  # чужой визит не считается
        data = self.period(self.URL).data
        self.assertEqual(data['revenue'], '350.00')
        self.assertEqual(data['visits'], 2)
        self.assertEqual(data['by_method']['cash'], '200.00')
        self.assertEqual(data['by_method']['card'], '150.00')
        self.assertEqual([row['appointment'] for row in data['unpaid']], [unpaid.id])
        self.assertTrue(all(row['can_cancel'] for row in data['payments']))

    def test_mode_off_forbidden_for_master(self):
        self.company.masters_independent = False
        self.company.save()
        self.auth(self.master_user)
        self.assertEqual(self.period(self.URL).status_code, 403)

    def test_owner_without_master_profile(self):
        self.auth(self.owner)
        self.assertEqual(self.period(self.URL).status_code, 404)


class MasterClientsTests(IndependentTestCase):
    def visit(self, master=None, start=time(10), **extra):
        return self.appointment(master=master, start=start, end=time(start.hour + 1), day=self.today, **extra)

    def test_master_sees_only_own_clients(self):
        self.visit(client_phone='+37369000001', client_name='Ирина')
        self.visit(master=self.master2, start=time(12), client_phone='+37369000002', client_name='Ольга')
        self.auth(self.master_user)
        data = self.api.get('/api/staff/clients/').data
        self.assertEqual([row['phone'] for row in data['results']], ['+37369000001'])
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': '+37369000002'}).status_code, 404)
        card = self.api.get('/api/staff/clients/card/', {'phone': '+37369000001'})
        self.assertEqual(card.status_code, 200)
        response = self.api.put('/api/staff/clients/note/', {'phone': '+37369000001', 'text': 'любит френч'},
                                format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.api.put('/api/staff/clients/note/', {'phone': '+37369000002', 'text': 'x'},
                                      format='json').status_code, 404)

    def test_shared_client_shows_only_own_visits(self):
        self.visit(client_phone='+37369000001')
        self.visit(master=self.master2, start=time(12), client_phone='+37369000001')
        self.auth(self.master_user)
        card = self.api.get('/api/staff/clients/card/', {'phone': '+37369000001'}).data
        self.assertEqual(card['total'], 1)
        self.assertEqual(len(card['appointments']), 1)

    def test_mode_off_clients_forbidden(self):
        self.company.masters_independent = False
        self.company.save()
        self.auth(self.master_user)
        self.assertEqual(self.api.get('/api/staff/clients/').status_code, 403)


class MasterAppointmentTests(IndependentTestCase):
    def test_master_reschedules_own_visit_but_not_to_other_master(self):
        visit = self.appointment()
        self.auth(self.master_user)
        url = f'/api/staff/appointments/{visit.id}/'
        with mock.patch('notifications.services.appointment_changed_by_staff'):
            response = self.api.patch(url, {'start_time': '12:00'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        response = self.api.patch(url, {'master': self.master2.id}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_mode_off_master_cannot_reschedule(self):
        self.company.masters_independent = False
        self.company.save()
        visit = self.appointment()
        self.auth(self.master_user)
        response = self.api.patch(f'/api/staff/appointments/{visit.id}/', {'start_time': '12:00'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_me_and_company_flag(self):
        self.auth(self.master_user)
        self.assertTrue(self.api.get('/api/staff/me/').data['company']['masters_independent'])
        self.assertEqual(self.api.patch('/api/staff/company/', {'masters_independent': False},
                                        format='json').status_code, 403)
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'masters_independent': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['masters_independent'])

    def test_master_edits_own_schedule(self):
        self.master.refresh_from_db()
        self.assertTrue(self.master.can_edit_own_schedule)
