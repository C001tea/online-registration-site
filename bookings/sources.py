"""Откуда пришла онлайн-запись: метка ?from= в ссылке салона или ссылка мастера."""

SITE = 'site'
ADMIN = 'admin'
# Известные метки; всё остальное — other (чтобы в статистике не копился мусор)
SOURCES = ('site', 'stories', 'instagram', 'telegram', 'facebook', 'tiktok', 'google', 'whatsapp', 'viber',
           'qr', 'master', 'other')


def clean_source(value):
    value = str(value or '').strip().lower()
    if not value:
        return SITE
    return value if value in SOURCES else 'other'


def source_of(appointment):
    """Источник записи; у старых записей поля нет — по тому, кто создал."""
    if appointment.source:
        return appointment.source
    return ADMIN if appointment.created_by_staff else SITE
