"""Раздел «Привлечение»: ссылки мастеров и откуда приходят записи."""
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Appointment, Master
from bookings.slots import get_free_slots
from bookings.sources import ADMIN, SITE
from dashboard.errors import StaffError
from dashboard.permissions import IsStaff
from retention.rhythm import salon_url

PERIODS = (7, 30, 90)
# Окно для сторис: можно записаться хотя бы на час, начало — каждые полчаса
WINDOW_MINUTES = 60
WINDOW_STEP = timedelta(minutes=30)
MAX_DAYS = 7
SLUG_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$')


def master_link(company, master):
    # Адрес салона бывает с ?salon=<slug> (старый адрес) — путь мастера вставляем до запроса
    parts = urlsplit(salon_url(company))
    path = parts.path.rstrip('/') + f'/m/{master.link_slug or master.id}'
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))


def visible_masters(staff):
    masters = staff.company.masters.filter(is_active=True).order_by('name', 'id')
    if staff.is_manager:
        return masters
    return masters.filter(id=staff.master.id) if staff.master else masters.none()


def source_counts(company, since, master=None):
    """{источник: число записей} за период; отменённые не считаем."""
    rows = Appointment.objects.filter(company=company, created_at__gte=since).exclude(status='cancelled')
    if master is not None:
        rows = rows.filter(master=master)
    # Части одного визита — одна запись клиента
    counts = {}
    seen_visits = set()
    for source, by_staff, visit in rows.values_list('source', 'created_by_staff', 'visit'):
        if visit:
            if visit in seen_visits:
                continue
            seen_visits.add(visit)
        key = source or (ADMIN if by_staff else SITE)
        counts[key] = counts.get(key, 0) + 1
    return counts


class PromoView(APIView):
    """GET ?days=30 -> {enabled, site_url, masters: [{id, name, link_slug, url}], sources: {...} | None}"""
    permission_classes = [IsStaff]

    def get(self, request):
        staff = request.staff
        company = staff.company
        try:
            days = int(request.query_params.get('days', 30))
        except ValueError:
            days = 30
        if days not in PERIODS:
            days = 30
        since = timezone.now() - timedelta(days=days)
        sources = None
        if staff.is_manager:
            sources = source_counts(company, since)
        elif company.masters_independent and staff.master:
            sources = source_counts(company, since, master=staff.master)
        return Response({
            'enabled': company.promo_enabled,
            'site_url': salon_url(company),
            'days': days,
            'masters': [{'id': m.id, 'name': m.name, 'link_slug': m.link_slug, 'url': master_link(company, m)}
                        for m in visible_masters(staff)],
            'sources': sources,
        })


class MasterLinkView(APIView):
    """PATCH {link_slug} — короткая ссылка мастера (владелец, админ или сам мастер)."""
    permission_classes = [IsStaff]

    def patch(self, request, pk):
        staff = request.staff
        master = get_object_or_404(Master, pk=pk, company=staff.company)
        if not staff.is_manager and (not staff.master or staff.master.id != master.id):
            raise StaffError('Недостаточно прав', status_code=403)
        slug = str(request.data.get('link_slug') or '').strip().lower()
        if slug and not SLUG_RE.match(slug):
            raise StaffError('Только латинские буквы, цифры и дефис, до 40 символов')
        if slug.isdigit():
            raise StaffError('Ссылка не может состоять только из цифр')
        if slug and Master.objects.filter(company=staff.company, link_slug=slug).filter(~Q(pk=master.pk)).exists():
            raise StaffError('Такая ссылка уже у другого мастера')
        master.link_slug = slug
        master.save(update_fields=['link_slug'])
        return Response({'id': master.id, 'name': master.name, 'link_slug': slug,
                         'url': master_link(staff.company, master)})


def free_windows(master, date):
    """Свободные промежутки дня: [{'from': '12:00', 'to': '15:00'}] — внутри можно начать часовую запись."""
    starts = [datetime.combine(date, slot) for slot in
              get_free_slots(master, date, WINDOW_MINUTES, step=WINDOW_STEP)]
    windows = []
    for start in starts:
        end = start + timedelta(minutes=WINDOW_MINUTES)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    return [{'from': a.strftime('%H:%M'), 'to': b.strftime('%H:%M')} for a, b in windows]


class WindowsView(APIView):
    """GET ?date=YYYY-MM-DD&days=1..7&master=<id> -> {days: [{date, masters: [{id, name, windows}]}]}

    Для картинки и текста «Свободные окна»: мастер видит только себя."""
    permission_classes = [IsStaff]

    def get(self, request):
        staff = request.staff
        params = request.query_params
        try:
            start = datetime.strptime(params.get('date') or '', '%Y-%m-%d').date()
        except ValueError:
            start = timezone.localdate()
        try:
            days = max(1, min(MAX_DAYS, int(params.get('days', 1))))
        except ValueError:
            days = 1
        masters = list(visible_masters(staff))
        if params.get('master'):
            masters = [m for m in masters if str(m.id) == str(params.get('master'))]
        result = []
        for offset in range(days):
            date = start + timedelta(days=offset)
            rows = [{'id': m.id, 'name': m.name, 'windows': free_windows(m, date)} for m in masters]
            result.append({'date': date.isoformat(), 'masters': [r for r in rows if r['windows']]})
        return Response({'days': result})
