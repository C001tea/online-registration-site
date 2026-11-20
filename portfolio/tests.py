"""Портфолио: фото и детали работы в карточке, «в прошлый раз», галерея мастера, сайт записи, права."""
from datetime import time, timedelta

from django.test import override_settings

from bookings.models import Appointment
from dashboard.test_photos import MEMORY_STORAGES, image_file
from dashboard.tests import StaffTestCase
from feedback.models import VisitReview
from .models import WorkPhoto

PHONE = '+37369000051'


@override_settings(STORAGES=MEMORY_STORAGES, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PortfolioTestCase(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.company.portfolio_enabled = True
        self.company.save()

    def visit(self, days_ago=0, master=None, start=time(10), **extra):
        return self.appointment(day=self.today - timedelta(days=days_ago), status='completed', client_phone=PHONE,
                                master=master, start=start, end=time(start.hour + 1), **extra)

    def upload(self, url, **data):
        return self.api.post(url, {'file': image_file(), **data}, format='multipart')


class VisitWorkTests(PortfolioTestCase):
    def test_photos_notes_and_previous_visit(self):
        self.auth(self.master_user)
        old = self.visit(days_ago=21)
        response = self.upload(f'/api/staff/works/visit/{old.id}/photos/')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data['public'])
        photo = WorkPhoto.objects.get()
        self.assertEqual((photo.master, photo.service, photo.appointment), (self.master, self.service, old))
        self.assertLessEqual(max(photo.image.width, photo.image.height), 1200)
        response = self.api.patch(f'/api/staff/works/visit/{old.id}/', {'notes': ' гель Kodi 45, миндаль '},
                                  format='json')
        self.assertEqual(response.data['notes'], 'гель Kodi 45, миндаль')

        new = self.visit(days_ago=0)
        data = self.api.get(f'/api/staff/works/visit/{new.id}/').data
        self.assertEqual((data['notes'], data['photos']), ('', []))
        self.assertEqual(data['previous']['id'], old.id)
        self.assertEqual(data['previous']['notes'], 'гель Kodi 45, миндаль')
        self.assertEqual(len(data['previous']['photos']), 1)

    def test_limit_five_photos_per_visit(self):
        self.auth(self.admin)
        visit = self.visit()
        for _ in range(5):
            self.assertEqual(self.upload(f'/api/staff/works/visit/{visit.id}/photos/').status_code, 201)
        self.assertEqual(self.upload(f'/api/staff/works/visit/{visit.id}/photos/').status_code, 400)

    def test_disabled_blocks_changes_but_get_works(self):
        self.company.portfolio_enabled = False
        self.company.save()
        self.auth(self.admin)
        visit = self.visit()
        self.assertFalse(self.api.get(f'/api/staff/works/visit/{visit.id}/').data['enabled'])
        self.assertEqual(self.upload(f'/api/staff/works/visit/{visit.id}/photos/').status_code, 403)
        self.assertEqual(self.api.patch(f'/api/staff/works/visit/{visit.id}/', {'notes': 'x'}, format='json')
                         .status_code, 403)

    def test_master_only_own_visits_and_other_salon(self):
        other = self.visit(master=self.master2)
        self.auth(self.master_user)
        self.assertEqual(self.api.get(f'/api/staff/works/visit/{other.id}/').status_code, 404)
        self.auth(self.owner_b)
        self.assertEqual(self.api.get(f'/api/staff/works/visit/{other.id}/').status_code, 404)


class GalleryTests(PortfolioTestCase):
    def test_old_work_upload_hide_and_public_site(self):
        self.auth(self.master_user)
        response = self.upload(f'/api/staff/works/master/{self.master.id}/', service=self.service.id)
        self.assertEqual(response.status_code, 201, response.data)
        photo_id = response.data['id']
        # Чужая галерея — нельзя
        self.assertEqual(self.upload(f'/api/staff/works/master/{self.master2.id}/').status_code, 403)
        # Скрыть может только владелец или админ
        self.assertEqual(self.api.patch(f'/api/staff/works/{photo_id}/', {'hidden': True}, format='json')
                         .status_code, 403)

        url = f'/api/companies/{self.company.slug}/masters/{self.master.id}/works/'
        rows = self.client.get(url).json()
        self.assertEqual([r['id'] for r in rows], [photo_id])
        self.assertEqual(rows[0]['service']['name'], self.service.name)

        self.auth(self.owner)
        self.assertTrue(self.api.patch(f'/api/staff/works/{photo_id}/', {'hidden': True}, format='json').data['hidden'])
        self.assertEqual(self.client.get(url).json(), [])
        self.api.patch(f'/api/staff/works/{photo_id}/', {'hidden': False}, format='json')
        self.auth(self.master_user)
        self.api.patch(f'/api/staff/works/{photo_id}/', {'public': False}, format='json')
        self.assertEqual(self.client.get(url).json(), [])

        self.company.portfolio_enabled = False
        self.company.save()
        self.api.patch(f'/api/staff/works/{photo_id}/', {'public': True}, format='json')
        self.assertEqual(self.client.get(url).json(), [])  # выключено — на сайте ничего

    def test_delete_photo(self):
        self.auth(self.master_user)
        photo_id = self.upload(f'/api/staff/works/master/{self.master.id}/').data['id']
        self.assertEqual(self.api.delete(f'/api/staff/works/{photo_id}/').status_code, 204)
        self.assertFalse(WorkPhoto.objects.exists())


class PrivacyAndReviewsTests(PortfolioTestCase):
    def test_forget_client_removes_works_and_review_text(self):
        visit = self.visit(work_notes='гель')
        self.auth(self.admin)
        self.upload(f'/api/staff/works/visit/{visit.id}/photos/')
        VisitReview.objects.create(appointment=visit, company=self.company, master=self.master, phone=PHONE,
                                   channel='telegram', rating=5, comment='Супер', publish=True)
        self.auth(self.owner)
        response = self.api.post('/api/staff/clients/forget/', {'phone': PHONE}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(WorkPhoto.objects.exists())
        visit.refresh_from_db()
        self.assertEqual(visit.work_notes, '')
        review = VisitReview.objects.get()
        self.assertEqual((review.rating, review.comment, review.publish, review.phone), (5, '', False, ''))

    def test_public_reviews_include_published_visit_reviews(self):
        shown = self.visit(client_name='Анна Чобану')
        hidden = self.visit(days_ago=1, client_name='Елена Русу')
        VisitReview.objects.create(appointment=shown, company=self.company, master=self.master, phone=PHONE,
                                   channel='telegram', rating=5, comment='Очень аккуратно', publish=True,
                                   rated_at=shown.created_at)
        VisitReview.objects.create(appointment=hidden, company=self.company, master=self.master, phone=PHONE,
                                   channel='telegram', rating=2, comment='Долго', publish=False,
                                   rated_at=hidden.created_at)
        data = self.client.get(f'/api/companies/{self.company.slug}/master/{self.master.id}/reviews/').json()
        self.assertEqual((data['avg_rating'], data['reviews_count']), (3.5, 2))
        self.assertEqual([(r['client_name'], r['comment']) for r in data['reviews']], [('Анна', 'Очень аккуратно')])
        self.assertNotIn('Елена', str(data))
        self.assertTrue(Appointment.objects.filter(pk=hidden.pk).exists())
