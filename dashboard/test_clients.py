from datetime import time, timedelta
from decimal import Decimal

from django.test import override_settings

from finance.models import Payment
from .models import ClientNote
from .tests import StaffTestCase

IRINA = '+37369000001'
OLGA = '+37369000002'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ClientBaseTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        past = self.today - timedelta(days=10)
        week_ago = self.today - timedelta(days=7)
        # Ирина: 2 визита (оплачены 200 и 150), неявка, отмена, будущая запись; сменила имя
        for day, status, name in [(past, 'completed', 'Ира'), (week_ago, 'completed', 'Ирина'),
                                  (week_ago, 'no_show', 'Ирина'), (week_ago, 'cancelled', 'Ирина'),
                                  (self.day, 'confirmed', 'Ирина Петрова')]:
            start = time(10 + len(name) % 5)
            self.appointment(day=day, status=status, client_name=name, start=start, end=time(start.hour + 1))
        for amount, a in zip((200, 150), self.company.appointments.filter(status='completed').order_by('date')):
            Payment.objects.create(company=self.company, appointment=a, master=a.master, date=a.date,
                                   price=Decimal(200), amount=Decimal(amount), discount=Decimal(200 - amount), method='cash')
        # Ольга: одна запись у другого мастера
        self.appointment(master=self.master2, day=past, status='completed', client_name='Ольга', client_phone=OLGA)
        # Клиент другого салона не виден
        self.appointment(master=self.master_b, company=self.company_b, service=self.service_b, client_name='Чужой',
                         client_phone='+37369000009')
        self.auth(self.admin)

    def clients(self, **params):
        response = self.api.get('/api/staff/clients/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_list_with_stats(self):
        data = self.clients()
        self.assertEqual(data['count'], 2)
        irina, olga = data['results']  # по последней активности: у Ирины будущая запись
        self.assertEqual(irina['phone'], IRINA)
        self.assertEqual(irina['name'], 'Ирина Петрова')  # имя из последней записи
        self.assertEqual((irina['total'], irina['visits'], irina['no_shows'], irina['cancelled']), (5, 2, 1, 1))
        self.assertEqual((irina['paid'], irina['average_check']), ('350.00', '175.00'))
        self.assertEqual(irina['last_visit'], (self.today - timedelta(days=7)).isoformat())
        self.assertEqual(irina['next_visit'], self.day.isoformat())
        self.assertEqual((olga['paid'], olga['average_check'], olga['next_visit']), ('0.00', None, None))

    def test_search_and_sort(self):
        self.assertEqual([c['phone'] for c in self.clients(q='Ольга')['results']], [OLGA])
        self.assertEqual([c['phone'] for c in self.clients(q='069000002')['results']], [OLGA])
        # Поиск по старому имени находит клиента, статистика — по всем его записям
        found = self.clients(q='Ира')['results']
        self.assertEqual([(c['phone'], c['total']) for c in found], [(IRINA, 5)])
        self.assertEqual(self.clients(q='Чужой')['count'], 0)
        self.assertEqual([c['phone'] for c in self.clients(sort='paid')['results']], [IRINA, OLGA])
        self.assertEqual([c['name'] for c in self.clients(sort='name')['results']], ['Ирина Петрова', 'Ольга'])
        self.assertEqual(self.clients(page=2)['results'], [])

    def test_card_and_note(self):
        response = self.api.get('/api/staff/clients/card/', {'phone': '069000001'})
        self.assertEqual(response.status_code, 200, response.data)
        card = response.data
        self.assertEqual(card['phone'], IRINA)
        self.assertEqual(len(card['appointments']), 5)
        self.assertEqual(card['appointments'][0]['date'], self.day.isoformat())  # новые сверху
        self.assertEqual(card['top_master'], 'Анна a')
        self.assertEqual(card['top_service'], 'Маникюр')
        self.assertEqual(sorted(a['paid'] for a in card['appointments'] if a['paid']), ['150.00', '200.00'])
        self.assertEqual(card['note'], '')

        url = '/api/staff/clients/note/'
        response = self.api.put(url, {'phone': IRINA, 'text': '  Аллергия на гель X  '}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.api.put(url, {'phone': IRINA, 'text': 'Любит кофе'}, format='json').data['note'], 'Любит кофе')
        self.assertEqual(ClientNote.objects.get().text, 'Любит кофе')
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': IRINA}).data['note'], 'Любит кофе')
        # Заметку на номер, которого нет в записях салона, не создать
        self.assertEqual(self.api.put(url, {'phone': '+37369000009', 'text': 'x'}, format='json').status_code, 404)

    def test_access(self):
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': '+37369000009'}).status_code, 404)
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': 'abc'}).status_code, 400)
        self.auth(self.master_user)
        for url in ('/api/staff/clients/', '/api/staff/clients/card/'):
            self.assertEqual(self.api.get(url, {'phone': IRINA}).status_code, 403)
        self.auth(self.owner_b)
        self.assertEqual([c['name'] for c in self.clients()['results']], ['Чужой'])
        self.assertEqual(self.api.get('/api/staff/clients/card/', {'phone': IRINA}).status_code, 404)
