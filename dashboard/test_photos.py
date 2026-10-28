import io

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from bookings.models import Company, Master
from .tests import StaffTestCase

MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


def image_file(name='photo.jpg', size=(3000, 2000), fmt='JPEG', mode='RGB', exif=None):
    buffer = io.BytesIO()
    image = Image.new(mode, size, 'red' if mode == 'RGB' else (255, 0, 0, 0))
    kwargs = {'exif': exif} if exif is not None else {}
    image.save(buffer, fmt, **kwargs)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f'image/{fmt.lower()}')


def stored_image(field):
    with field.storage.open(field.name) as f:
        image = Image.open(f)
        image.load()
    return image


@override_settings(STORAGES=MEMORY_STORAGES, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PhotoUploadTests(StaffTestCase):
    def setUp(self):
        super().setUp()
        self.auth(self.admin)

    def upload(self, url, file):
        return self.api.post(url, {'file': file}, format='multipart')

    def test_main_photo_is_resized_and_shown_on_site(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # повёрнуто на 90° — после сохранения портрет
        response = self.upload('/api/staff/company/main-photo/', image_file(exif=exif))
        self.assertEqual(response.status_code, 200, response.data)
        self.company.refresh_from_db()
        image = stored_image(self.company.main_photo)
        self.assertEqual((image.format, image.size), ('JPEG', (1067, 1600)))
        self.assertFalse(image.getexif())  # метаданные (в т.ч. геолокация) удалены
        self.assertEqual(response.data['main_photo'], self.company.main_photo.url)
        public = self.api.get('/api/companies/a/info/').data
        self.assertTrue(public['main_photo'].endswith(self.company.main_photo.url))

    def test_replace_and_delete_remove_old_file(self):
        url = '/api/staff/company/logo/'
        self.upload(url, image_file('logo.png', size=(200, 200), fmt='PNG', mode='RGBA'))
        self.company.refresh_from_db()
        first = self.company.logo.name
        self.assertTrue(first.endswith('.png'))  # прозрачность логотипа сохранилась
        with self.captureOnCommitCallbacks(execute=True):
            self.upload(url, image_file('logo2.jpg', size=(200, 200)))
        self.assertFalse(default_storage.exists(first))
        self.company.refresh_from_db()
        second = self.company.logo.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.delete(url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['logo'])
        self.assertFalse(default_storage.exists(second))
        self.assertFalse(Company.objects.get(pk=self.company.pk).logo)

    def test_master_photo(self):
        url = f'/api/staff/masters/{self.master2.id}/photo/'
        response = self.upload(url, image_file(size=(400, 300)))
        self.assertEqual(response.status_code, 200, response.data)
        self.master2.refresh_from_db()
        self.assertEqual(stored_image(self.master2.photo).size, (400, 300))  # маленькое не увеличиваем
        self.assertEqual(response.data['photo'], self.master2.photo.url)
        public = self.api.get('/api/companies/a/masters/').data
        self.assertIn(self.master2.photo.url, [m['photo'] for m in public if m['id'] == self.master2.id][0])
        self.assertEqual(self.api.delete(url).data['photo'], None)

    def test_bad_files(self):
        url = '/api/staff/company/main-photo/'
        not_image = SimpleUploadedFile('x.jpg', b'not an image', content_type='image/jpeg')
        self.assertEqual(self.upload(url, not_image).data['error'], 'Загрузите фото в формате JPG, PNG или WebP')
        gif = image_file('x.gif', size=(10, 10), fmt='GIF', mode='RGB')
        self.assertEqual(self.upload(url, gif).status_code, 400)
        self.assertEqual(self.api.post(url, {}, format='multipart').data['error'], 'Выберите файл с фото')
        huge = SimpleUploadedFile('big.jpg', b'0' * (10 * 1024 * 1024 + 1), content_type='image/jpeg')
        self.assertIn('10 МБ', self.upload(url, huge).data['error'])
        self.assertEqual(self.api.post('/api/staff/company/other/', {}, format='multipart').status_code, 404)
        self.company.refresh_from_db()
        self.assertFalse(self.company.main_photo)

    def test_permissions(self):
        self.auth(self.master_user)
        self.assertEqual(self.upload('/api/staff/company/logo/', image_file()).status_code, 403)
        self.assertEqual(self.upload(f'/api/staff/masters/{self.master.id}/photo/', image_file()).status_code, 403)
        self.auth(self.owner_b)
        self.assertEqual(self.upload(f'/api/staff/masters/{self.master.id}/photo/', image_file()).status_code, 404)
        self.upload('/api/staff/company/logo/', image_file(size=(50, 50)))
        self.assertFalse(Company.objects.get(pk=self.company.pk).logo)  # чужой салон не тронут
        self.assertFalse(Master.objects.get(pk=self.master.pk).photo)
