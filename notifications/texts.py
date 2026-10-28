"""Тексты сообщений бота (HTML-разметка Telegram).

Клиенту — на языке записи (appointment.lang: ru/ro, перевод в bookings/i18n.py), салону — по-русски.
"""
from html import escape

from bookings.i18n import MONTHS, WEEKDAYS, tr

STATUS_LINES = {
    'pending': '⏳ Ждём вашего подтверждения',
    'confirmed': '✅ Запись подтверждена',
    'cancelled': '❌ Запись отменена',
    'completed': 'Визит состоялся',
    'no_show': 'Визит пропущен',
}


def lang_of(appointment):
    return getattr(appointment, 'lang', None) or 'ru'


def when(appointment, lang='ru'):
    d = appointment.date
    return f'{WEEKDAYS[lang][d.weekday()]}, {d.day} {MONTHS[lang][d.month - 1]}, {appointment.start_time:%H:%M}'


def details(appointment, lang='ru'):
    """Салон, услуга, мастер, время и адрес."""
    company = appointment.company
    lines = [f'💅 <b>{escape(company.name)}</b>']
    if appointment.service:
        lines.append(tr('Услуга: {name}', lang, name=escape(appointment.service.name)))
    if appointment.master:
        lines.append(tr('Мастер: {name}', lang, name=escape(appointment.master.name)))
    lines.append(f'📅 {when(appointment, lang)}')
    if company.address:
        lines.append(f'📍 {escape(company.address)}')
    return '\n'.join(lines)


def status_line(appointment):
    return tr(STATUS_LINES.get(appointment.status, ''), lang_of(appointment))


def client_card(appointment):
    return f'{details(appointment, lang_of(appointment))}\n\n{status_line(appointment)}'


def client_message(title, appointment):
    lang = lang_of(appointment)
    return f'{tr(title, lang)}\n{details(appointment, lang)}'


def staff_new(appointment):
    note = f'\nКомментарий: {escape(appointment.notes)}' if appointment.notes else ''
    lang = '\nЯзык клиента: румынский' if lang_of(appointment) == 'ro' else ''
    return (f'🆕 <b>Новая запись</b>\n{details(appointment)}\n'
            f'Клиент: {escape(appointment.client_name)}, {escape(appointment.client_phone)}{note}{lang}')


def staff_cancelled(appointment):
    return (f'❌ <b>Клиент отменил запись</b>\n{details(appointment)}\n'
            f'Клиент: {escape(appointment.client_name)}, {escape(appointment.client_phone)}')


def reminder_24h(appointment):
    return client_message('🔔 <b>Напоминаем о записи завтра</b>', appointment)


def reminder_1h(appointment):
    return client_message('⏰ <b>Через час ждём вас</b>', appointment)


def salon_cancelled(appointment):
    return client_message('❌ <b>Салон отменил вашу запись</b>', appointment)


def salon_moved(appointment):
    return f'{client_message("🔄 <b>Запись перенесена</b>", appointment)}\n\n{status_line(appointment)}'


# Язык неизвестен (бот открыли без ссылки с сайта) — на двух языках
GREETING = ('Здравствуйте! Здесь будут подтверждения и напоминания о ваших записях.\n'
            'Чтобы подключить запись, нажмите «Подтвердить в Telegram» после записи на сайте.\n\n'
            'Bună ziua! Aici veți primi confirmări și mementouri despre programările dvs.\n'
            'Pentru a conecta o programare, apăsați «Confirmă în Telegram» după programarea pe site.')
LINK_USED = 'Эта запись уже подключена к другому Telegram.'
LINK_NOT_FOUND = 'Ссылка устарела или не найдена. Откройте её заново с сайта.'
STAFF_LINK_EXPIRED = 'Ссылка для подключения устарела. Получите новую в панели салона.'
CONFIRM_CANCEL = '\n\n<b>Точно отменить запись?</b>'
TOO_LATE = 'Эту запись уже нельзя изменить'


def staff_linked(company_name):
    return (f'✅ Готово! Уведомления о записях салона <b>{escape(company_name)}</b> '
            f'будут приходить сюда.')
