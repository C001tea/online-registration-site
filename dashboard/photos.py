"""Фото салона и мастеров из панели: проверка, уменьшение, замена старого файла."""
import io
import logging
import uuid

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import StaffError

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 40_000_000  # защита от «бомбы распаковки»: 40 Мп хватает любой камере телефона
FORMATS = ('JPEG', 'PNG', 'WEBP')
# Наибольшая сторона после уменьшения
MAX_SIDE = {'main_photo': 1600, 'logo': 600, 'photo': 800}
FORMAT_MESSAGE = 'Загрузите фото в формате JPG, PNG или WebP'


def uploaded_file(request):
    """Файл из POST multipart (поле file); DELETE — None, фото убирается."""
    if request.method == 'DELETE':
        return None
    upload = request.FILES.get('file')
    if upload is None:
        raise StaffError('Выберите файл с фото')
    return upload


def prepare_image(upload, field):
    """Проверяет картинку и пересохраняет её: уменьшенную, без EXIF (там бывают координаты съёмки)."""
    if upload.size > MAX_UPLOAD_BYTES:
        raise StaffError('Фото больше 10 МБ — выберите файл поменьше')
    try:
        image = Image.open(upload)
        if image.format not in FORMATS:
            raise StaffError(FORMAT_MESSAGE)
        if image.width * image.height > MAX_PIXELS:
            raise StaffError('Слишком большое разрешение фото')
        image.load()
    except StaffError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
        raise StaffError(FORMAT_MESSAGE)

    image = ImageOps.exif_transpose(image)
    image.thumbnail((MAX_SIDE[field], MAX_SIDE[field]))
    # Прозрачность сохраняем только у логотипа (PNG), остальное — JPEG
    keep_alpha = field == 'logo' and (image.mode in ('RGBA', 'LA') or 'transparency' in image.info)
    buffer = io.BytesIO()
    if keep_alpha:
        image.convert('RGBA').save(buffer, 'PNG', optimize=True)
        ext = 'png'
    else:
        image.convert('RGB').save(buffer, 'JPEG', quality=85, optimize=True, progressive=True)
        ext = 'jpg'
    return ContentFile(buffer.getvalue(), name=f'{uuid.uuid4().hex[:12]}.{ext}')


def delete_file(storage, name):
    try:
        storage.delete(name)
    except Exception:  # файл в облаке не удалился — не страшно, запись уже без него
        logger.warning('Не удалось удалить старое фото %s', name, exc_info=True)


def replace_photo(instance, field, upload):
    """Сохраняет новое фото (upload=None — удаляет) и после коммита удаляет старый файл."""
    file_field = getattr(instance, field)
    old_name, storage = file_field.name, file_field.storage
    if upload is None:
        setattr(instance, field, None)
        instance.save(update_fields=[field])
    else:
        content = prepare_image(upload, field)
        try:
            file_field.save(content.name, content, save=False)
        except Exception:
            logger.exception('Не удалось сохранить фото в хранилище')
            raise StaffError('Не удалось сохранить фото, попробуйте позже', status_code=503)
        instance.save(update_fields=[field])
    if old_name and old_name != getattr(instance, field).name:
        transaction.on_commit(lambda: delete_file(storage, old_name))
    return instance
