"""Оценка визита по ссылке из письма (без входа): /api/review/<token>/?r=1..5.

GET показывает форму (звёзды, комментарий, «показать на сайте») — по GET ничего не сохраняем, ссылки открывают
почтовые сканеры. POST сохраняет оценку и показывает «спасибо» со ссылкой на Google (всем — правило Google).
"""
from django.http import HttpResponse
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from bookings.i18n import tr
from .models import VisitReview
from .service import CHANGE_WINDOW, MAX_COMMENT, instagram_url, lang_of, save_rating

PAGE = ('<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        '<title>{title}</title></head>'
        '<body style="font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px;'
        'color:#1f2937;line-height:1.5">{body}</body></html>')
BUTTON = ('display:inline-block;font-size:16px;padding:10px 18px;border-radius:10px;border:1px solid #d1d5db;'
          'background:#fff;color:#1f2937;text-decoration:none;cursor:pointer;margin:4px 8px 4px 0')


def page(lang, title, body, status=200):
    return HttpResponse(PAGE.format(lang=lang, title=escape(title), body=body), status=status)


def form(review, lang, selected):
    appointment = review.appointment
    question = (tr('Как вам визит к мастеру {master}?', lang, master=appointment.master.name) if appointment.master
                else tr('Как вам визит?', lang))
    radios = ''.join(
        f'<label style="display:block;font-size:20px;margin:6px 0"><input type="radio" name="r" value="{n}"'
        f'{" checked" if n == selected else ""} required> {"★" * n}{"☆" * (5 - n)}</label>'
        for n in range(5, 0, -1))
    return (
        f'<p style="color:#6b7280;margin:0">{escape(review.company.name)}</p><h1 style="font-size:22px">{escape(question)}</h1>'
        f'<form method="post">{radios}'
        f'<p><label>{escape(tr("Пара слов о визите (необязательно)", lang))}<br>'
        f'<textarea name="comment" rows="4" maxlength="{MAX_COMMENT}" style="width:100%;font-size:16px;'
        f'border:1px solid #d1d5db;border-radius:10px;padding:8px;box-sizing:border-box">{escape(review.comment)}</textarea>'
        f'</label></p><p><label><input type="checkbox" name="publish" value="1"{" checked" if review.publish else ""}> '
        f'{escape(tr("Можно показать мой отзыв на сайте салона", lang))}</label></p>'
        f'<button type="submit" style="{BUTTON}">{escape(tr("Отправить", lang))}</button></form>')


def thanks(review, lang):
    company = review.company
    if review.is_low:
        text = tr('Спасибо, что рассказали. Нам очень жаль — владелец салона уже знает и свяжется с вами.', lang)
    else:
        text = tr('Спасибо за отзыв! Будем рады, если расскажете о нас:', lang)
    links = []
    if company.review_google_url.startswith('https://'):
        links.append(f'<a href="{escape(company.review_google_url)}" style="{BUTTON}">'
                     f'{escape(tr("Отзыв в Google", lang))}</a>')
    insta = instagram_url(company)
    if insta and not review.is_low:
        links.append(f'<a href="{escape(insta)}" style="{BUTTON}">Instagram</a>')
    return f'<p>{escape(text)}</p><p>{"".join(links)}</p>'


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def review_page(request, token):
    review = (VisitReview.objects.select_related('appointment__master', 'appointment__company', 'company')
              .filter(token=token).first())
    if not review:
        text = tr('Ссылка неверна или устарела.', 'ru')
        return page('ru', text, f'<p>{escape(text)}</p>', status=404)
    lang = lang_of(review.appointment)
    locked = review.rated_at and timezone.now() - review.rated_at > CHANGE_WINDOW
    if request.method == 'POST' and not locked:
        try:
            rating = int(request.POST.get('r', ''))
        except ValueError:
            rating = 0
        if 1 <= rating <= 5:
            save_rating(review, rating, comment=request.POST.get('comment', '').strip(),
                        publish=request.POST.get('publish') == '1')
            return page(lang, review.company.name, thanks(review, lang))
    if locked:
        return page(lang, review.company.name, thanks(review, lang))
    try:
        selected = int(request.GET.get('r', ''))
    except ValueError:
        selected = review.rating or 0
    return page(lang, review.company.name, form(review, lang, selected))
