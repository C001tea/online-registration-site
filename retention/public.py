"""Отписка от напоминаний «пора записаться» по ссылке из письма (без входа).

GET показывает вопрос с кнопкой (почтовые сканеры открывают ссылки сами — по GET не отписываем),
POST отписывает; POST без формы — «отписка в один клик» почтовых программ (List-Unsubscribe-Post).
"""
from django.http import HttpResponse
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from bookings.i18n import tr
from bookings.models import Company
from .auto import opt_out, read_opt_out_token

PAGE = ('<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        '<title>{title}</title></head>'
        '<body style="font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 16px;'
        'color:#1f2937;line-height:1.5">{body}</body></html>')


def page(lang, title, body, status=200):
    return HttpResponse(PAGE.format(lang=lang, title=escape(title), body=body), status=status)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def rhythm_opt_out(request, token):
    data = read_opt_out_token(token)
    company = Company.objects.filter(pk=data['c']).first() if data else None
    lang = data.get('l') if data and data.get('l') in ('ru', 'ro') else 'ru'
    if not company:
        text = tr('Ссылка неверна или устарела.', lang)
        return page(lang, text, f'<p>{escape(text)}</p>', status=404)
    if request.method == 'POST':
        opt_out(company.id, data['p'])
        text = tr('Готово: напоминания «пора записаться» от {salon} больше не придут.', lang, salon=company.name)
        return page(lang, company.name, f'<p>{escape(text)}</p>')
    question = tr('Не присылать напоминания «пора записаться» от {salon}?', lang, salon=company.name)
    note = tr('Письма о ваших записях (подтверждение и напоминание о визите) продолжат приходить.', lang)
    button = tr('Не присылать', lang)
    body = (f'<p><b>{escape(question)}</b></p><p>{escape(note)}</p>'
            f'<form method="post"><button type="submit" style="font-size:16px;padding:10px 18px;'
            f'border-radius:10px;border:1px solid #d1d5db;background:#fff;cursor:pointer">{escape(button)}</button></form>')
    return page(lang, company.name, body)
