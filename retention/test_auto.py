"""Автонапоминания «пора записаться»: кому, когда, сколько раз, отписка в Telegram и по ссылке из письма."""
from datetime import datetime, time, timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from notifications import services, telegram
from notifications.models import AppointmentTelegram, OutgoingMessage
from . import auto
from .models import ClientOptOut, RhythmMessage
from .tests import OTHER, PHONE, RhythmTestCase

BOT = dict(TELEGRAM_BOT_TOKEN='123:test', TELEGRAM_BOT_USERNAME='salon_test_bot',
           SALON_URL_TEMPLATE='https://{slug}.timepoint.tech')
CHAT = 555001


@override_settings(EMAIL_CONFIGURED=True, **BOT)
class RhythmAutoTests(RhythmTestCase):
    def setUp(self):
        super().setUp()
        self.company.rhythm_auto = True
        self.company.save()
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(self.today, time(12, 0)))

    def due_client(self, days_ago=21, phone=PHONE, chat=CHAT, **extra):
        """Два визита с промежутком 21 день; последний — days_ago дней назад."""
        self.visit(days_ago + 21, phone=phone, **extra)
        last = self.visit(days_ago, phone=phone, **extra)
        if chat:
            AppointmentTelegram.objects.create(appointment=last, chat_id=chat)
        return last

    def test_due_today_gets_one_telegram_message(self):
        self.due_client()
        self.assertEqual(auto.process_company(self.company, self.now), 1)
        message = RhythmMessage.objects.get()
        self.assertEqual((message.channel, message.step, message.chat_id), ('telegram', 1, CHAT))
        outgoing = OutgoingMessage.objects.get()
        self.assertEqual(outgoing.chat_id, CHAT)
        self.assertIn('Здравствуйте, Ирина!', outgoing.text)
        self.assertEqual(outgoing.buttons, [[['Выбрать время', 'https://a.timepoint.tech/']],
                                            [['Не напоминать', f'roff:{message.id}']]])
        markup = telegram.keyboard(outgoing.buttons)['inline_keyboard']
        self.assertEqual(markup[0][0], {'text': 'Выбрать время', 'url': 'https://a.timepoint.tech/'})
        # Повторно в тот же день — ничего
        self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_long_overdue_and_not_yet_due_get_nothing(self):
        self.due_client(days_ago=27, phone=PHONE)  # срок прошёл 6 дней назад — только вручную
        self.due_client(days_ago=15, phone=OTHER, chat=CHAT + 1)  # до срока 6 дней
        self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_lost_and_opted_out_get_nothing(self):
        self.due_client(days_ago=21)
        ClientOptOut.objects.create(company=self.company, phone=PHONE, kind='rhythm')
        self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_second_reminder_after_a_week_then_stop(self):
        last = self.due_client(days_ago=29)  # срок прошёл 8 дней назад
        RhythmMessage.objects.create(company=self.company, phone=PHONE, group=f'c{self.service.category_id}',
                                     cycle=last.date, step=1, channel='telegram', chat_id=CHAT,
                                     sent_at=self.now - timedelta(days=8))
        self.assertEqual(auto.process_company(self.company, self.now), 1)
        self.assertEqual(RhythmMessage.objects.order_by('-id').first().step, 2)
        # Через неделю — третьего нет
        self.assertEqual(auto.process_company(self.company, self.now + timedelta(days=8)), 0)

    def test_no_bot_reminder_after_staff_wrote_personally(self):
        last = self.due_client(days_ago=29)
        RhythmMessage.objects.create(company=self.company, phone=PHONE, group=f'c{self.service.category_id}',
                                     cycle=last.date, step=1, channel='whatsapp',
                                     sent_at=self.now - timedelta(days=8))
        self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_email_without_telegram_and_opt_out_link(self):
        self.due_client(chat=None, client_email='irina@example.com')
        self.assertEqual(auto.process_company(self.company, self.now), 1)
        self.assertEqual(RhythmMessage.objects.get().channel, 'email')
        self.assertEqual(len(mail.outbox), 1)
        letter = mail.outbox[0]
        self.assertEqual(letter.to, ['irina@example.com'])
        self.assertIn('Пора записаться', letter.subject)
        url = letter.extra_headers['List-Unsubscribe'].strip('<>')
        self.assertIn(url, letter.body)
        path = url.replace('https://a.timepoint.tech', '')
        # Открыть ссылку (почтовый сканер) — ещё не отписка
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Не присылать')
        self.assertFalse(ClientOptOut.objects.exists())
        self.assertEqual(self.client.post(path).status_code, 200)
        self.assertTrue(ClientOptOut.objects.filter(company=self.company, phone=PHONE, kind='rhythm').exists())
        self.assertEqual(self.client.get('/api/rhythm/off/bad-token/').status_code, 404)

    @override_settings(EMAIL_CONFIGURED=False, TELEGRAM_BOT_TOKEN='')
    def test_no_channel_no_message(self):
        self.due_client(client_email='irina@example.com')
        self.assertEqual(auto.process_company(self.company, self.now), 0)
        self.assertFalse(RhythmMessage.objects.exists())

    def test_one_message_per_client_across_groups_and_daily_limit(self):
        self.due_client(phone=PHONE)
        self.due_client(phone=OTHER, chat=CHAT + 1)
        with mock.patch.object(auto, 'DAILY_LIMIT', 1):
            self.assertEqual(auto.process_company(self.company, self.now), 1)
            self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_run_respects_switches_hours_and_hourly_guard(self):
        self.due_client()
        night = timezone.make_aware(datetime.combine(self.today, time(21, 30)))
        self.assertEqual(auto.run(night), 0)
        self.company.rhythm_enabled = False
        self.company.save()
        self.assertEqual(auto.run(self.now), 0)
        self.company.rhythm_enabled = True
        self.company.save()
        cache.clear()
        self.assertEqual(auto.run(self.now), 1)
        RhythmMessage.objects.all().delete()
        self.assertEqual(auto.run(self.now + timedelta(minutes=5)), 0)  # в этот час уже проверяли

    def test_off_by_default(self):
        self.company.rhythm_auto = False
        self.company.save()
        self.due_client()
        self.assertEqual(auto.run(self.now), 0)

    def test_telegram_button_opts_out_only_same_chat(self):
        self.due_client()
        auto.process_company(self.company, self.now)
        message = RhythmMessage.objects.get()
        calls = []
        with mock.patch.object(telegram, 'call', lambda method, **params: calls.append((method, params))):
            services.handle_callback({'id': '1', 'data': f'roff:{message.id}',
                                      'message': {'chat': {'id': CHAT + 9}, 'message_id': 7}})
            self.assertFalse(ClientOptOut.objects.exists())
            services.handle_callback({'id': '2', 'data': f'roff:{message.id}',
                                      'message': {'chat': {'id': CHAT}, 'message_id': 7}})
        self.assertTrue(ClientOptOut.objects.filter(phone=PHONE, kind='rhythm').exists())
        methods = [method for method, _ in calls]
        self.assertIn('editMessageReplyMarkup', methods)
        answer = [params for method, params in calls if method == 'answerCallbackQuery'][-1]
        self.assertEqual(answer.get('text'), 'Хорошо, больше не будем напоминать')
        # Отписанному больше не пишем
        RhythmMessage.objects.all().delete()
        self.assertEqual(auto.process_company(self.company, self.now), 0)

    def test_owner_toggles_auto(self):
        self.auth(self.owner)
        response = self.api.patch('/api/staff/company/', {'rhythm_auto': False}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.company.refresh_from_db()
        self.assertFalse(self.company.rhythm_auto)
