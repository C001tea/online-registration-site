"""Письма клиенту без Telegram и выбор времени напоминания."""
from datetime import time, timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Appointment, Company, Master, MasterSchedule, Service
from . import email, services
from .models import AppointmentTelegram, OutgoingMessage


def salon():
    company = Company.objects.create(name='Салон', slug='salon', phone='+37322000000')
    service = Service.objects.create(company=company, name='Маникюр', duration=60, price=300)
    master = Master.objects.create(company=company, name='Анна')
    master.services.add(service)
    for day in range(7):
        MasterSchedule.objects.create(master=master, day_of_week=day, start_time=time(8), end_time=time(20))
    return company, service, master


@override_settings(EMAIL_CONFIGURED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class EmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company, self.service, self.master = salon()

    def book(self, days=5, **extra):
        day = timezone.localdate() + timedelta(days=days)
        payload = {'master': self.master.id, 'service': self.service.id, 'client_name': 'Ирина',
                   'client_phone': '+37369123456', 'client_email': 'irina@example.com',
                   'date': day.isoformat(), 'start_time': '10:00', 'consent': True, **extra}
        response = self.api.post('/api/companies/salon/appointments/', payload, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return Appointment.objects.get(pk=response.data['id']) if 'id' in response.data else Appointment.objects.latest('id')

    def test_confirmation_sent_once_with_manage_link(self):
        appointment = self.book()
        self.assertEqual(email.process(), 1)
        self.assertEqual(email.process(), 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Салон', mail.outbox[0].subject)
        self.assertIn(f'/manage/{appointment.manage_token}', mail.outbox[0].body)

    def test_reminder_at_chosen_time(self):
        appointment = self.book(reminder_minutes=120)
        email.process()  # подтверждение
        start = timezone.make_aware(timezone.datetime.combine(appointment.date, appointment.start_time))
        self.assertEqual(email.process(now=start - timedelta(hours=3)), 0)
        self.assertEqual(email.process(now=start - timedelta(minutes=110)), 1)
        self.assertIn('Напоминание', mail.outbox[-1].subject)
        self.assertEqual(email.process(now=start - timedelta(minutes=100)), 0)

    def test_no_reminder_when_declined(self):
        appointment = self.book(reminder_minutes=0)
        email.process()
        start = timezone.make_aware(timezone.datetime.combine(appointment.date, appointment.start_time))
        self.assertEqual(email.process(now=start - timedelta(minutes=30)), 0)

    def test_bad_reminder_choice_refused(self):
        day = timezone.localdate() + timedelta(days=5)
        response = self.api.post('/api/companies/salon/appointments/', {
            'master': self.master.id, 'service': self.service.id, 'client_name': 'И', 'client_phone': '+37369123456',
            'date': day.isoformat(), 'start_time': '10:00', 'consent': True, 'reminder_minutes': 7}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_staff_and_old_appointments_get_no_confirmation(self):
        day = timezone.localdate() + timedelta(days=2)
        Appointment.objects.create(company=self.company, master=self.master, service=self.service,
                                   client_name='А', client_phone='+37369000000', client_email='a@example.com',
                                   date=day, start_time=time(9), end_time=time(10), created_by_staff=True)
        email.process()
        self.assertEqual(len(mail.outbox), 0)


class TelegramCustomReminderTests(TestCase):
    def setUp(self):
        self.company, self.service, self.master = salon()

    @override_settings(TELEGRAM_BOT_TOKEN='x')
    def test_custom_lead_single_reminder(self):
        day = timezone.localdate() + timedelta(days=4)
        appointment = Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='А',
            client_phone='+37369000000', date=day, start_time=time(10), end_time=time(11), status='confirmed',
            reminder_minutes=2880)
        AppointmentTelegram.objects.create(appointment=appointment, chat_id=123)
        start = timezone.make_aware(timezone.datetime.combine(day, time(10)))
        self.assertEqual(services.queue_reminders(now=start - timedelta(hours=47)), 1)
        self.assertEqual(services.queue_reminders(now=start - timedelta(hours=1)), 0)
        self.assertEqual(OutgoingMessage.objects.filter(chat_id=123).count(), 1)
