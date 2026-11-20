"""Отзывы после визита: когда спрашиваем, оценка в Telegram и по ссылке из письма, тревога владельцу, раздел панели."""
from datetime import datetime, time, timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from bookings.models import Review
from dashboard.tests import StaffTestCase
from notifications import services, telegram
from notifications.models import AppointmentTelegram, OutgoingMessage, StaffTelegram
from retention.models import ClientOptOut
from . import service
from .models import VisitReview

BOT = dict(TELEGRAM_BOT_TOKEN='123:test', TELEGRAM_BOT_USERNAME='salon_test_bot',
           SALON_URL_TEMPLATE='https://{slug}.timepoint.tech')
CHAT = 777001
OWNER_CHAT = 777100
PHONE = '+37369000031'


@override_settings(EMAIL_CONFIGURED=True, **BOT)
class FeedbackTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.company.reviews_enabled = True
        self.company.review_google_url = 'https://g.page/r/test/review'
        self.company.review_instagram = '@salon_a'
        self.company.save()
        cache.clear()
        self.now = timezone.make_aware(datetime.combine(self.today, time(15, 0)))
        StaffTelegram.objects.create(user=self.owner, chat_id=OWNER_CHAT)

    def done_visit(self, start=time(11, 0), end=time(12, 0), day=None, phone=PHONE, chat=CHAT, **extra):
        appointment = self.appointment(day=day or self.today, status='completed', client_phone=phone,
                                       client_name='Мария Ионеску', start=start, end=end, **extra)
        if chat:
            AppointmentTelegram.objects.create(appointment=appointment, chat_id=chat)
        return appointment

    def callback(self, data, chat=CHAT):
        calls = []
        with mock.patch.object(telegram, 'call', lambda method, **params: calls.append((method, params))):
            services.handle_callback({'id': '1', 'data': data, 'message': {'chat': {'id': chat}, 'message_id': 5}})
        return calls

    def owner_alerts(self):
        return list(OutgoingMessage.objects.filter(chat_id=OWNER_CHAT).values_list('text', flat=True))


class RequestTests(FeedbackTestCase):
    def test_asks_two_hours_after_visit_once(self):
        appointment = self.done_visit(end=time(12, 30))
        self.assertEqual(service.send_requests(self.now.replace(hour=14)), 0)  # прошло 1,5 часа
        self.assertEqual(service.send_requests(self.now), 1)
        review = VisitReview.objects.get()
        self.assertEqual((review.appointment, review.channel, review.chat_id, review.rating),
                         (appointment, 'telegram', CHAT, None))
        message = OutgoingMessage.objects.get(chat_id=CHAT)
        self.assertIn('Здравствуйте, Мария!', message.text)
        self.assertEqual([b[1] for b in message.buttons[0]], [f'rv{n}:{review.id}' for n in range(1, 6)])
        self.assertEqual(service.send_requests(self.now), 0)

    def test_not_at_night_not_when_disabled_not_for_other_statuses(self):
        self.done_visit()
        self.appointment(day=self.today, status='confirmed', client_phone='+37369000032')
        self.assertEqual(service.send_requests(self.now.replace(hour=21)), 0)
        self.company.reviews_enabled = False
        self.company.save()
        self.assertEqual(service.send_requests(self.now), 0)
        self.company.reviews_enabled = True
        self.company.save()
        self.assertEqual(service.send_requests(self.now), 1)

    def test_once_a_month_per_client_and_opt_out(self):
        self.done_visit(day=self.today - timedelta(days=1))
        self.assertEqual(service.send_requests(self.now), 1)
        self.done_visit(phone=PHONE, chat=None)
        self.assertEqual(service.send_requests(self.now), 0)  # уже спрашивали в этом месяце
        self.done_visit(phone='+37369000033', chat=CHAT + 3)
        ClientOptOut.objects.create(company=self.company, phone='+37369000033', kind='all')
        self.assertEqual(service.send_requests(self.now), 0)

    def test_old_visits_are_not_asked(self):
        self.done_visit(day=self.today - timedelta(days=3))
        self.assertEqual(service.send_requests(self.now), 0)

    def test_email_when_no_telegram(self):
        self.done_visit(chat=None, client_email='maria@example.com')
        self.assertEqual(service.send_requests(self.now), 1)
        review = VisitReview.objects.get()
        self.assertEqual(review.channel, 'email')
        self.assertIn(f'https://a.timepoint.tech/api/review/{review.token}/?r=5', mail.outbox[0].body)

    @override_settings(EMAIL_CONFIGURED=False)
    def test_no_channel_nothing_sent(self):
        self.done_visit(chat=None, client_email='maria@example.com')
        self.assertEqual(service.send_requests(self.now), 0)
        self.assertFalse(VisitReview.objects.exists())


class TelegramAnswerTests(FeedbackTestCase):
    def setUp(self):
        super().setUp()
        self.done_visit()
        service.send_requests(self.now)
        self.review = VisitReview.objects.get()

    def test_high_rating_then_publish_and_comment(self):
        calls = self.callback(f'rv5:{self.review.id}')
        self.review.refresh_from_db()
        self.assertEqual((self.review.rating, self.review.awaiting_comment), (5, True))
        edit = next(params for method, params in calls if method == 'editMessageText')
        buttons = [b for row in edit['reply_markup']['inline_keyboard'] for b in row]
        self.assertIn({'text': 'Отзыв в Google', 'url': 'https://g.page/r/test/review'}, buttons)
        self.assertIn({'text': 'Instagram', 'url': 'https://instagram.com/salon_a'}, buttons)
        self.assertEqual(self.owner_alerts(), [])
        self.callback(f'rvp:{self.review.id}')
        self.review.refresh_from_db()
        self.assertTrue(self.review.publish)
        services.handle_update({'message': {'chat': {'id': CHAT, 'type': 'private'}, 'text': 'Всё супер, спасибо!'}})
        self.review.refresh_from_db()
        self.assertEqual((self.review.comment, self.review.awaiting_comment), ('Всё супер, спасибо!', False))

    def test_low_rating_alerts_owner_and_comment_follows(self):
        calls = self.callback(f'rv2:{self.review.id}')
        edit = next(params for method, params in calls if method == 'editMessageText')
        self.assertIn('Что пошло не так', edit['text'])
        buttons = [b for row in edit['reply_markup']['inline_keyboard'] for b in row]
        # Google — всем (правило Google), но без Instagram и без «показать на сайте»
        self.assertEqual(buttons, [{'text': 'Отзыв в Google', 'url': 'https://g.page/r/test/review'}])
        alerts = self.owner_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn('Низкая оценка', alerts[0])
        self.assertIn(PHONE, alerts[0])
        services.handle_update({'message': {'chat': {'id': CHAT, 'type': 'private'}, 'text': 'Долго ждала <мастера>'}})
        alerts = self.owner_alerts()
        self.assertEqual(len(alerts), 2)
        self.assertIn('Долго ждала &lt;мастера&gt;', alerts[1])

    def test_other_chat_cannot_rate(self):
        self.callback(f'rv1:{self.review.id}', chat=CHAT + 50)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.rating)

    def test_text_without_pending_review_gets_greeting(self):
        with mock.patch.object(services, 'reply') as reply:
            services.handle_update({'message': {'chat': {'id': CHAT, 'type': 'private'}, 'text': 'Привет'}})
        reply.assert_called_once()


class EmailPageTests(FeedbackTestCase):
    def setUp(self):
        super().setUp()
        self.done_visit(chat=None, client_email='maria@example.com')
        service.send_requests(self.now)
        self.review = VisitReview.objects.get()
        self.url = f'/api/review/{self.review.token}/'

    def test_get_shows_form_without_saving_then_post_saves(self):
        page = self.client.get(self.url, {'r': '4'})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'value="4" checked')
        self.review.refresh_from_db()
        self.assertIsNone(self.review.rating)
        page = self.client.post(self.url, {'r': '4', 'comment': 'Отлично', 'publish': '1'})
        self.assertContains(page, 'Отзыв в Google')
        self.review.refresh_from_db()
        self.assertEqual((self.review.rating, self.review.comment, self.review.publish), (4, 'Отлично', True))

    def test_low_rating_by_email_alerts(self):
        self.client.post(self.url, {'r': '1', 'comment': 'Плохо'})
        self.assertEqual(len(self.owner_alerts()), 1)

    def test_bad_token(self):
        self.assertEqual(self.client.get('/api/review/nope/').status_code, 404)


class PanelAndPublicTests(FeedbackTestCase):
    def rated(self, rating, phone, master=None, publish=False):
        appointment = self.done_visit(phone=phone, chat=None, master=master or self.master)
        return VisitReview.objects.create(appointment=appointment, company=self.company, master=appointment.master,
                                          phone=phone, channel='telegram', rating=rating, rated_at=self.now,
                                          publish=publish)

    def test_panel_summary_and_low_filter(self):
        self.rated(5, '+37369000041')
        self.rated(2, '+37369000042')
        self.rated(4, '+37369000043', master=self.master2)
        self.auth(self.owner)
        data = self.api.get('/api/staff/reviews/').data
        self.assertEqual(data['summary'], {'average': 3.7, 'count': 3, 'requested': 3, 'low': 1})
        self.assertEqual({m['id']: m['average'] for m in data['masters']}, {self.master.id: 3.5, self.master2.id: 4.0})
        low = self.api.get('/api/staff/reviews/', {'low': '1'}).data['results']
        self.assertEqual([r['rating'] for r in low], [2])

    def test_public_master_rating_counts_all_ratings(self):
        self.rated(5, '+37369000041')
        self.rated(2, '+37369000042')
        Review.objects.create(master=self.master, client=self.owner, rating=5)
        response = self.client.get(f'/api/companies/{self.company.slug}/masters/')
        row = next(m for m in response.json() if m['id'] == self.master.id)
        self.assertEqual((row['avg_rating'], row['reviews_count']), (4.0, 3))

    def test_settings_validation(self):
        self.auth(self.owner)
        bad = self.api.patch('/api/staff/company/', {'review_google_url': 'http://x'}, format='json')
        self.assertEqual(bad.status_code, 400)
        ok = self.api.patch('/api/staff/company/', {'review_instagram': 'salon.b', 'reviews_enabled': False},
                            format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.company.refresh_from_db()
        self.assertEqual((self.company.review_instagram, self.company.reviews_enabled), ('@salon.b', False))
