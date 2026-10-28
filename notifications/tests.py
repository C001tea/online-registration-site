from datetime import datetime, time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import AdminProfile, Appointment, Company, Master, MasterSchedule, Service
from dashboard.tokens import get_staff_tokens
from . import services, telegram
from .models import AppointmentTelegram, ClientTelegram, OutgoingMessage, StaffLinkToken, StaffTelegram

BOT = dict(TELEGRAM_BOT_TOKEN='123:test', TELEGRAM_BOT_USERNAME='salon_test_bot')
CLIENT_CHAT = 5001
OWNER_CHAT = 7001


class FakeTelegram:
    """Подменяет telegram.call: запоминает вызовы, отвечает как Bot API."""

    def __init__(self):
        self.calls = []
        self.fail_with = None

    def __call__(self, method, **params):
        if self.fail_with:
            raise self.fail_with
        self.calls.append((method, params))
        if method == 'getMe':
            return {'username': 'salon_test_bot'}
        return {'message_id': len(self.calls)}

    def methods(self):
        return [method for method, _ in self.calls]

    def last(self, method):
        return [params for m, params in self.calls if m == method][-1]


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'], **BOT)
class TelegramTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        self.company = Company.objects.create(name='JcNails', slug='jc', address='str. Academiei 17')
        self.service = Service.objects.create(company=self.company, name='Маникюр', duration=60, price=300)
        self.master = Master.objects.create(company=self.company, name='Анна')
        self.master.services.add(self.service)
        for day in range(7):
            MasterSchedule.objects.create(master=self.master, day_of_week=day, start_time=time(9), end_time=time(18))
        self.owner = User.objects.create_user('owner', password='Str0ng-pass-123')
        AdminProfile.objects.create(user=self.owner, company=self.company, role='owner')
        StaffTelegram.objects.create(user=self.owner, chat_id=OWNER_CHAT)
        self.day = timezone.localdate() + timedelta(days=7)
        self.fake = FakeTelegram()
        patcher = mock.patch.object(telegram, 'call', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def book(self, **overrides):
        payload = {'master': self.master.id, 'service': self.service.id, 'client_name': 'Ирина',
                   'client_phone': '+37369123456', 'date': self.day.isoformat(), 'start_time': '10:00'}
        payload.update(overrides)
        return self.api.post(f'/api/companies/{self.company.slug}/appointments/', payload, format='json')

    def webhook(self, update, secret=None):
        return self.api.post('/api/telegram/webhook/', update, format='json',
                             HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN=secret or telegram.webhook_secret())

    def start(self, payload, chat_id=CLIENT_CHAT):
        return self.webhook({'update_id': 1, 'message': {
            'message_id': 1, 'chat': {'id': chat_id, 'type': 'private'}, 'text': f'/start {payload}'}})

    def press(self, data, chat_id=CLIENT_CHAT):
        return self.webhook({'update_id': 2, 'callback_query': {
            'id': 'cb1', 'data': data, 'message': {'message_id': 10, 'chat': {'id': chat_id, 'type': 'private'}}}})

    def linked_appointment(self):
        self.assertEqual(self.book().status_code, 201)
        link = AppointmentTelegram.objects.get()
        self.start(f'a_{link.token}')
        link.refresh_from_db()
        return link

    def queued(self, chat_id):
        return list(OutgoingMessage.objects.filter(chat_id=chat_id).values_list('text', flat=True))


class BookingFlowTests(TelegramTestCase):
    def test_booking_returns_bot_link_and_notifies_salon(self):
        response = self.book(notes='Хочу френч')
        self.assertEqual(response.status_code, 201, response.data)
        link = AppointmentTelegram.objects.get()
        self.assertEqual(response.data['telegram_url'], f'https://t.me/salon_test_bot?start=a_{link.token}')
        [text] = self.queued(OWNER_CHAT)
        self.assertIn('Новая запись', text)
        self.assertIn('Ирина', text)
        self.assertIn('Хочу френч', text)
        # Клиент ещё не подключил Telegram
        self.assertEqual(OutgoingMessage.objects.count(), 1)

    def test_master_of_appointment_is_notified(self):
        master_user = User.objects.create_user('anna')
        self.master.user = master_user
        self.master.save()
        StaffTelegram.objects.create(user=master_user, chat_id=8001)
        other = User.objects.create_user('other')
        StaffTelegram.objects.create(user=other, chat_id=9001)  # не сотрудник этого салона
        self.book()
        self.assertEqual(len(self.queued(8001)), 1)
        self.assertEqual(self.queued(9001), [])

    def test_start_links_appointment_and_shows_buttons(self):
        link = self.linked_appointment()
        self.assertEqual(link.chat_id, CLIENT_CHAT)
        sent = self.fake.last('sendMessage')
        self.assertEqual(sent['chat_id'], CLIENT_CHAT)
        self.assertIn('JcNails', sent['text'])
        self.assertIn('Ждём вашего подтверждения', sent['text'])
        buttons = [b['callback_data'] for b in sent['reply_markup']['inline_keyboard'][0]]
        self.assertEqual(buttons, [f'ok:{link.id}', f'cx:{link.id}'])

    def test_link_cannot_be_taken_by_another_chat(self):
        link = self.linked_appointment()
        self.start(f'a_{link.token}', chat_id=6666)
        link.refresh_from_db()
        self.assertEqual(link.chat_id, CLIENT_CHAT)
        self.assertIn('уже подключена', self.fake.last('sendMessage')['text'])

    def test_confirm(self):
        link = self.linked_appointment()
        self.press(f'ok:{link.id}')
        self.assertEqual(Appointment.objects.get().status, 'confirmed')
        edited = self.fake.last('editMessageText')
        self.assertIn('Запись подтверждена', edited['text'])
        self.assertEqual(self.fake.last('answerCallbackQuery')['text'], 'Спасибо, ждём вас!')

    def test_buttons_from_other_chat_are_ignored(self):
        link = self.linked_appointment()
        self.press(f'ok:{link.id}', chat_id=6666)
        self.press(f'cy:{link.id}', chat_id=6666)
        self.assertEqual(Appointment.objects.get().status, 'pending')

    def test_cancel_asks_then_cancels_and_notifies_salon(self):
        link = self.linked_appointment()
        self.press(f'cx:{link.id}')
        self.assertEqual(Appointment.objects.get().status, 'pending')
        self.assertIn('Точно отменить', self.fake.last('editMessageText')['text'])
        self.press(f'cy:{link.id}')
        self.assertEqual(Appointment.objects.get().status, 'cancelled')
        self.assertIn('Клиент отменил запись', self.queued(OWNER_CHAT)[-1])
        # Отменённую уже не подтвердить
        self.press(f'ok:{link.id}')
        self.assertEqual(Appointment.objects.get().status, 'cancelled')
        self.assertEqual(self.fake.last('answerCallbackQuery')['text'], 'Эту запись уже нельзя изменить')

    def test_past_appointment_cannot_be_changed(self):
        link = self.linked_appointment()
        Appointment.objects.update(date=timezone.localdate() - timedelta(days=1))
        self.press(f'cy:{link.id}')
        self.assertEqual(Appointment.objects.get().status, 'pending')

    def test_logged_in_client_gets_message_without_link(self):
        user = User.objects.create_user('client')
        ClientTelegram.objects.create(user=user, chat_id=CLIENT_CHAT)
        self.api.force_authenticate(user)
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(AppointmentTelegram.objects.get().chat_id, CLIENT_CHAT)
        self.assertIn('Ждём вашего подтверждения', self.queued(CLIENT_CHAT)[0])

    def test_start_remembers_chat_of_client_account(self):
        user = User.objects.create_user('client')
        self.api.force_authenticate(user)
        self.linked_appointment()
        self.assertEqual(ClientTelegram.objects.get(user=user).chat_id, CLIENT_CHAT)

    def test_client_cancel_on_site_notifies_salon(self):
        user = User.objects.create_user('client')
        self.api.force_authenticate(user)
        self.book()
        appointment = Appointment.objects.get()
        response = self.api.post(f'/api/appointments/{appointment.id}/cancel/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('Клиент отменил запись', self.queued(OWNER_CHAT)[-1])

    @override_settings(TELEGRAM_BOT_USERNAME='')
    def test_bot_username_from_get_me(self):
        response = self.book()
        self.assertTrue(response.data['telegram_url'].startswith('https://t.me/salon_test_bot?start=a_'))
        self.book(start_time='12:00')
        self.assertEqual(self.fake.methods().count('getMe'), 1)  # кешируется

    @override_settings(TELEGRAM_BOT_TOKEN='')
    def test_without_bot_nothing_is_queued(self):
        response = self.book()
        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data['telegram_url'])
        self.assertEqual(OutgoingMessage.objects.count(), 0)


class WebhookTests(TelegramTestCase):
    def test_wrong_secret_is_rejected(self):
        response = self.webhook({'update_id': 1}, secret='wrong')
        self.assertEqual(response.status_code, 403)
        response = self.api.post('/api/telegram/webhook/', {'update_id': 1}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_unknown_token_and_plain_start(self):
        self.start('a_nope')
        self.assertIn('не найдена', self.fake.last('sendMessage')['text'])
        self.webhook({'update_id': 3, 'message': {'chat': {'id': 1, 'type': 'private'}, 'text': '/start'}})
        self.assertIn('Здравствуйте', self.fake.last('sendMessage')['text'])

    def test_group_chats_are_ignored(self):
        self.webhook({'update_id': 4, 'message': {'chat': {'id': -1, 'type': 'group'}, 'text': '/start'}})
        self.assertEqual(self.fake.calls, [])

    def test_broken_update_does_not_fail(self):
        response = self.webhook({'update_id': 5, 'callback_query': {'id': 'x', 'data': 'ok:abc'}})
        self.assertEqual(response.status_code, 200)


class StaffLinkTests(TelegramTestCase):
    def auth(self, user):
        self.api.credentials(HTTP_AUTHORIZATION='Bearer ' + get_staff_tokens(user)['access'])

    def test_connect_status_and_disconnect(self):
        admin = User.objects.create_user('admin', password='Str0ng-pass-123')
        AdminProfile.objects.create(user=admin, company=self.company, role='admin')
        self.auth(admin)
        self.assertEqual(self.api.get('/api/staff/telegram/').data, {'available': True, 'connected': False})
        url = self.api.post('/api/staff/telegram/').data['url']
        token = StaffLinkToken.objects.get(user=admin).token
        self.assertEqual(url, f'https://t.me/salon_test_bot?start=s_{token}')

        self.start(f's_{token}', chat_id=4242)
        self.assertEqual(StaffTelegram.objects.get(user=admin).chat_id, 4242)
        self.assertIn('JcNails', self.fake.last('sendMessage')['text'])
        self.assertFalse(StaffLinkToken.objects.exists())
        self.assertTrue(self.api.get('/api/staff/telegram/').data['connected'])

        self.assertEqual(self.api.delete('/api/staff/telegram/').status_code, 204)
        self.assertFalse(StaffTelegram.objects.filter(user=admin).exists())

    def test_expired_link(self):
        self.auth(self.owner)
        self.api.post('/api/staff/telegram/')
        StaffLinkToken.objects.update(created_at=timezone.now() - timedelta(minutes=20))
        token = StaffLinkToken.objects.get().token
        self.start(f's_{token}', chat_id=4242)
        self.assertEqual(StaffTelegram.objects.get(user=self.owner).chat_id, OWNER_CHAT)
        self.assertIn('устарела', self.fake.last('sendMessage')['text'])

    def test_not_staff_cannot_get_link(self):
        user = User.objects.create_user('nobody', password='Str0ng-pass-123')
        self.auth(user)
        self.assertEqual(self.api.post('/api/staff/telegram/').status_code, 403)

    def test_salon_cancel_and_move_reach_client(self):
        link = self.linked_appointment()
        appointment = link.appointment
        AppointmentTelegram.objects.filter(pk=link.pk).update(reminder_24h_sent=True)
        self.auth(self.owner)
        url = f'/api/staff/appointments/{appointment.id}/'
        self.assertEqual(self.api.patch(url, {'start_time': '14:00'}, format='json').status_code, 200)
        self.assertIn('Запись перенесена', self.queued(CLIENT_CHAT)[-1])
        link.refresh_from_db()
        self.assertFalse(link.reminder_24h_sent)
        self.assertEqual(self.api.patch(url, {'status': 'cancelled'}, format='json').status_code, 200)
        self.assertIn('Салон отменил', self.queued(CLIENT_CHAT)[-1])


class ReminderTests(TelegramTestCase):
    def make(self, start_in, booked_before):
        now = timezone.now()
        start = timezone.localtime(now + start_in)
        appointment = Appointment.objects.create(
            company=self.company, master=self.master, service=self.service, client_name='Ирина',
            client_phone='+37369123456', date=start.date(), start_time=start.time().replace(microsecond=0),
            end_time=time(23, 59), status='confirmed')
        Appointment.objects.filter(pk=appointment.pk).update(created_at=start - booked_before)
        return AppointmentTelegram.objects.create(appointment=appointment, chat_id=CLIENT_CHAT)

    def test_24h_reminder_once(self):
        self.make(timedelta(hours=23), booked_before=timedelta(days=3))
        self.assertEqual(services.queue_reminders(), 1)
        self.assertEqual(services.queue_reminders(), 0)
        self.assertIn('завтра', self.queued(CLIENT_CHAT)[0])

    def test_1h_reminder(self):
        self.make(timedelta(minutes=50), booked_before=timedelta(days=3))
        services.queue_reminders()
        self.assertIn('Через час', self.queued(CLIENT_CHAT)[0])

    def test_no_reminders_for_late_bookings_or_cancelled(self):
        self.make(timedelta(hours=20), booked_before=timedelta(hours=3))  # записались за 3 часа до суток
        self.make(timedelta(minutes=40), booked_before=timedelta(hours=1))  # записались за час
        cancelled = self.make(timedelta(hours=23), booked_before=timedelta(days=3))
        Appointment.objects.filter(pk=cancelled.appointment_id).update(status='cancelled')
        self.assertEqual(services.queue_reminders(), 0)

    def test_nothing_for_unlinked(self):
        link = self.make(timedelta(hours=23), booked_before=timedelta(days=3))
        AppointmentTelegram.objects.filter(pk=link.pk).update(chat_id=None)
        self.assertEqual(services.queue_reminders(), 0)


class QueueTests(TelegramTestCase):
    def test_send_and_retry(self):
        services.enqueue(CLIENT_CHAT, 'привет', [[('Да', 'ok:1')]])
        self.assertEqual(services.send_queued(), 1)
        message = OutgoingMessage.objects.get()
        self.assertIsNotNone(message.sent_at)
        sent = self.fake.last('sendMessage')
        self.assertEqual(sent['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'ok:1')

        services.enqueue(CLIENT_CHAT, 'второе')
        self.fake.fail_with = telegram.TelegramError('Too Many Requests', retry_after=3)
        self.assertEqual(services.send_queued(), 0)
        retry = OutgoingMessage.objects.get(text='второе')
        self.assertEqual(retry.attempts, 1)
        self.assertGreater(retry.next_attempt_at, timezone.now())
        # До next_attempt_at повторно не шлём
        self.fake.fail_with = None
        self.assertEqual(services.send_queued(), 0)
        OutgoingMessage.objects.filter(pk=retry.pk).update(next_attempt_at=timezone.now())
        self.assertEqual(services.send_queued(), 1)

    def test_blocked_bot_is_not_retried(self):
        services.enqueue(CLIENT_CHAT, 'привет')
        self.fake.fail_with = telegram.TelegramError('Forbidden: bot was blocked by the user', permanent=True)
        services.send_queued()
        OutgoingMessage.objects.update(next_attempt_at=None)
        self.fake.fail_with = None
        self.assertEqual(services.send_queued(), 0)

    def test_cleanup(self):
        services.enqueue(CLIENT_CHAT, 'старое')
        OutgoingMessage.objects.update(sent_at=timezone.now() - timedelta(days=31))
        services.cleanup()
        self.assertFalse(OutgoingMessage.objects.exists())


@override_settings(**BOT, API_DOMAIN='api.example.com')
class SetupCommandTests(TelegramTestCase):
    def test_sets_webhook_with_secret(self):
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        call_command('telegram_setup', stdout=out)
        params = self.fake.last('setWebhook')
        self.assertEqual(params['url'], 'https://api.example.com/api/telegram/webhook/')
        self.assertEqual(params['secret_token'], telegram.webhook_secret())
        self.assertIn('@salon_test_bot', out.getvalue())
