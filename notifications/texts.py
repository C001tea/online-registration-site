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


def money(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.') + ' L'


# Рамка блока чёрного списка — чтобы он не сливался с текстом записи
DIVIDER = '━━━━━━━━━━━━━━━━'


def penalty_reason(penalty, lang='ru', who='client'):
    """«28 октября, 14:00 — не пришёл(-ли) на «Маникюр» (200 L)» — за что штраф."""
    a = penalty.appointment
    when_text = f'{a.date.day} {MONTHS[lang][a.date.month - 1]}, {a.start_time:%H:%M}'
    service = f'«{escape(a.service.name)}» ({money(a.service.price)})' if a.service else ''
    if penalty.kind == 'no_show':
        what = tr('не пришёл на {service}, не предупредив' if who == 'staff' else 'вы не пришли на {service}',
                  lang, service=service or tr('запись', lang))
    else:
        what = tr('отменил {service} меньше чем за {hours} ч до визита' if who == 'staff'
                  else 'вы отменили {service} меньше чем за {hours} ч до визита',
                  lang, service=service or tr('запись', lang), hours=a.company.late_cancel_hours)
    return f'• {when_text} — {what}'


def staff_blacklist(penalties):
    """Блок для салона: почему клиент в чёрном списке и что сделать при визите."""
    if not penalties:
        return ''
    company = penalties[0].company
    debt = sum(p.amount for p in penalties if p.status == 'unpaid')
    lines = ['', '', DIVIDER, '⛔ <b>КЛИЕНТ В ЧЁРНОМ СПИСКЕ</b>', '', '<b>Почему:</b>']
    for p in penalties:
        state = f'штраф {money(p.amount)} — <b>не оплачен</b>' if p.status == 'unpaid' else f'штраф {money(p.amount)} оплачен'
        lines.append(f'{penalty_reason(p, who="staff")}\n   {state}')
    lines.append('')
    if debt:
        lines += [
            f'💰 <b>При визите получить: {money(debt)}</b>',
            f'Это {company.penalty_percent}% стоимости пропущенной услуги — по правилам салона, '
            f'которые клиент видит при записи.',
            'В панели штраф сам добавится к счёту в окне «Оплата».',
        ]
    else:
        lines.append('Долгов нет — штрафы оплачены. Клиент уже пропускал записи: напомните о правилах салона.')
    lines.append(DIVIDER)
    return '\n'.join(lines)


def staff_new(appointment, penalties=()):
    note = f'\nКомментарий: {escape(appointment.notes)}' if appointment.notes else ''
    lang = '\nЯзык клиента: румынский' if lang_of(appointment) == 'ro' else ''
    return (f'🆕 <b>Новая запись</b>\n{details(appointment)}\n'
            f'Клиент: {escape(appointment.client_name)}, {escape(appointment.client_phone)}{note}{lang}'
            f'{staff_blacklist(list(penalties))}')


def debt_note(appointment, unpaid):
    """Блок для клиента: неоплаченный штраф, за что и где оплатить (по правилам салона)."""
    lang = lang_of(appointment)
    company = appointment.company
    debt = sum(p.amount for p in unpaid)
    rule = (tr('за неявку или отмену меньше чем за {hours} ч до визита', lang, hours=company.late_cancel_hours)
            if company.late_cancel_hours else tr('за неявку', lang))
    lines = ['', '', DIVIDER, tr('⚠️ <b>Неоплаченный штраф: {amount}</b>', lang, amount=money(debt)), '']
    lines += [penalty_reason(p, lang) for p in unpaid]
    lines += [
        '',
        tr('Согласно правилам салона, с которыми вы ознакомились при записи, {rule} взимается штраф '
           '{percent}% стоимости услуги.', lang, rule=rule, percent=company.penalty_percent),
        '',
        tr('💳 Пожалуйста, оплатите его в салоне при этом визите.', lang),
        DIVIDER,
    ]
    return '\n'.join(lines)


def late_cancel_warning(appointment, fee):
    return '\n\n' + tr('⚠️ До визита меньше {hours} ч: при отмене будет начислен штраф {amount} '
                        '(оплата в салоне при следующем визите).', lang_of(appointment),
                        hours=appointment.company.late_cancel_hours, amount=money(fee))


def penalty_client(penalty):
    a = penalty.appointment
    lang = lang_of(a)
    title = ('❗ <b>Вы не пришли на запись</b>' if penalty.kind == 'no_show'
             else '❗ <b>Запись отменена позже чем за {hours} ч до визита</b>')
    return (f'{tr(title, lang, hours=a.company.late_cancel_hours)}\n{details(a, lang)}\n\n'
            + tr('Начислен штраф {amount} ({percent}% стоимости услуги). Его нужно будет оплатить '
                 'в салоне при следующем визите.', lang, amount=money(penalty.amount),
                 percent=a.company.penalty_percent))


def penalty_staff(penalty):
    a = penalty.appointment
    return (f'⚠️ <b>Поздняя отмена — клиент в чёрном списке</b>\n{details(a)}\n'
            f'Клиент: {escape(a.client_name)}, {escape(a.client_phone)}\n'
            f'Штраф {money(penalty.amount)} — получить при следующем визите')


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
