"""Тексты сообщений бота (HTML-разметка Telegram)."""
from html import escape

WEEKDAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря']

STATUS_LINES = {
    'pending': '⏳ Ждём вашего подтверждения',
    'confirmed': '✅ Запись подтверждена',
    'cancelled': '❌ Запись отменена',
    'completed': 'Визит состоялся',
    'no_show': 'Визит пропущен',
}


def when(appointment):
    d = appointment.date
    return f'{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}, {appointment.start_time:%H:%M}'


def details(appointment):
    """Салон, услуга, мастер, время и адрес."""
    company = appointment.company
    lines = [f'💅 <b>{escape(company.name)}</b>']
    if appointment.service:
        lines.append(f'Услуга: {escape(appointment.service.name)}')
    if appointment.master:
        lines.append(f'Мастер: {escape(appointment.master.name)}')
    lines.append(f'📅 {when(appointment)}')
    if company.address:
        lines.append(f'📍 {escape(company.address)}')
    return '\n'.join(lines)


def client_card(appointment):
    return f'{details(appointment)}\n\n{STATUS_LINES.get(appointment.status, "")}'


def staff_new(appointment):
    note = f'\nКомментарий: {escape(appointment.notes)}' if appointment.notes else ''
    return (f'🆕 <b>Новая запись</b>\n{details(appointment)}\n'
            f'Клиент: {escape(appointment.client_name)}, {escape(appointment.client_phone)}{note}')


def staff_cancelled(appointment):
    return (f'❌ <b>Клиент отменил запись</b>\n{details(appointment)}\n'
            f'Клиент: {escape(appointment.client_name)}, {escape(appointment.client_phone)}')


def reminder_24h(appointment):
    return f'🔔 <b>Напоминаем о записи завтра</b>\n{details(appointment)}'


def reminder_1h(appointment):
    return f'⏰ <b>Через час ждём вас</b>\n{details(appointment)}'


def salon_cancelled(appointment):
    return f'❌ <b>Салон отменил вашу запись</b>\n{details(appointment)}'


def salon_moved(appointment):
    return f'🔄 <b>Запись перенесена</b>\n{details(appointment)}\n\n{STATUS_LINES.get(appointment.status, "")}'


GREETING = ('Здравствуйте! Здесь будут подтверждения и напоминания о ваших записях.\n'
            'Чтобы подключить запись, нажмите «Подтвердить в Telegram» после записи на сайте.')
LINK_USED = 'Эта запись уже подключена к другому Telegram.'
LINK_NOT_FOUND = 'Ссылка устарела или не найдена. Откройте её заново с сайта.'
STAFF_LINK_EXPIRED = 'Ссылка для подключения устарела. Получите новую в панели салона.'
CONFIRM_CANCEL = '\n\n<b>Точно отменить запись?</b>'
TOO_LATE = 'Эту запись уже нельзя изменить'


def staff_linked(company_name):
    return (f'✅ Готово! Уведомления о записях салона <b>{escape(company_name)}</b> '
            f'будут приходить сюда.')
