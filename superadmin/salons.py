"""Сводка платформы и управление салонами в супер-админке.

Все действия с салоном пишутся в журнал (SuperAdminLog). Блокировка: сайт записи скрыт
(PublicCompanyManager), сотрудники не входят в панель (IsStaff, StaffLoginView).
"""
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count, Max, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from bookings.models import AdminProfile, Appointment, Company, Master, MasterSchedule, MasterWorkDay, Service
from dashboard.signup import public_url, send_password_reset, send_verification
from dashboard.tokens import get_impersonation_token
from notifications.models import StaffTelegram

from .auth import log
from .models import SalonNote, SuperAdminLog
from .views import SuperView

TRIAL_ENDING_DAYS = 3
INACTIVE_DAYS = 7
SERIES_DAYS = 30
TRIAL_PRESETS = (7, 14, 30)
NOTE_MAX = 2000


def local_midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def staff_user_ids(company_ids=None):
    """{company_id: {user_id, ...}} — владельцы, администраторы и мастера с входом."""
    result = {}
    profiles = AdminProfile.objects.filter(company__isnull=False)
    masters = Master.objects.filter(user__isnull=False, is_active=True)
    if company_ids is not None:
        profiles = profiles.filter(company_id__in=company_ids)
        masters = masters.filter(company_id__in=company_ids)
    for company_id, user_id in profiles.values_list('company_id', 'user_id'):
        result.setdefault(company_id, set()).add(user_id)
    for company_id, user_id in masters.values_list('company_id', 'user_id'):
        result.setdefault(company_id, set()).add(user_id)
    return result


def last_activity(company_ids):
    """{company_id: последний вход любого сотрудника салона}."""
    users = staff_user_ids(company_ids)
    logins = dict(User.objects.filter(pk__in=set().union(*users.values()) if users else [])
                  .values_list('pk', 'last_login'))
    result = {}
    for company_id, ids in users.items():
        times = [logins.get(pk) for pk in ids if logins.get(pk)]
        result[company_id] = max(times) if times else None
    return result


def owners(company_ids):
    """{company_id: User владельца}."""
    rows = (AdminProfile.objects.filter(company_id__in=company_ids, role='owner')
            .select_related('user').order_by('id'))
    result = {}
    for profile in rows:
        result.setdefault(profile.company_id, profile.user)
    return result


def trial_state(company, now):
    if not company.trial_ends_at:
        return 'none'
    if company.trial_ends_at <= now:
        return 'expired'
    if company.trial_ends_at <= now + timedelta(days=TRIAL_ENDING_DAYS):
        return 'ending'
    return 'active'


def iso(value):
    return value.isoformat() if value else None


def salon_rows(companies):
    """Строки списка салонов с владельцем, счётчиками и последней активностью."""
    companies = list(companies)
    ids = [c.id for c in companies]
    now = timezone.now()
    month_ago = now - timedelta(days=30)
    counts = {row['id']: row for row in Company.objects.filter(id__in=ids).annotate(
        masters_count=Count('masters', filter=Q(masters__is_active=True), distinct=True),
        services_count=Count('services', filter=Q(services__is_active=True), distinct=True),
    ).values('id', 'masters_count', 'services_count')}
    appointments = {row['company_id']: row for row in Appointment.objects.filter(company_id__in=ids)
                    .values('company_id').annotate(total=Count('id'),
                                                   month=Count('id', filter=Q(created_at__gte=month_ago)),
                                                   last=Max('created_at'))}
    activity = last_activity(ids)
    owner_users = owners(ids)
    rows = []
    for c in companies:
        owner = owner_users.get(c.id)
        stats = appointments.get(c.id, {})
        rows.append({
            'id': c.id,
            'name': c.name,
            'slug': c.slug,
            'status': c.status,
            'phone': c.phone,
            'created_at': iso(c.created_at),
            'trial_ends_at': iso(c.trial_ends_at),
            'trial': trial_state(c, now),
            'owner': {'username': owner.username, 'email': owner.email} if owner else None,
            'masters': counts.get(c.id, {}).get('masters_count', 0),
            'services': counts.get(c.id, {}).get('services_count', 0),
            'appointments': stats.get('total', 0),
            'appointments_month': stats.get('month', 0),
            'last_appointment_at': iso(stats.get('last')),
            'last_activity_at': iso(activity.get(c.id)),
            'url': public_url(c),
        })
    return rows


class OverviewView(SuperView):
    """Сводка: салоны по статусам, записи, графики за 30 дней, что требует внимания."""

    def get(self, request):
        now = timezone.now()
        today = timezone.localdate()
        companies = list(Company.objects.all())
        by_status = {key: 0 for key, _ in Company.STATUS_CHOICES}
        trial = {'active': 0, 'ending': 0, 'expired': 0, 'none': 0}
        for c in companies:
            by_status[c.status] = by_status.get(c.status, 0) + 1
            trial[trial_state(c, now)] += 1

        appts = Appointment.objects.all()
        start = today - timedelta(days=SERIES_DAYS - 1)
        month_start = local_midnight(today - timedelta(days=29))
        totals = {
            'today': appts.filter(created_at__gte=local_midnight(today)).count(),
            'week': appts.filter(created_at__gte=local_midnight(today - timedelta(days=6))).count(),
            'month': appts.filter(created_at__gte=month_start).count(),
            'month_online': appts.filter(created_at__gte=month_start, created_by_staff=False).count(),
            'visits_today': appts.filter(date=today, status__in=['pending', 'confirmed', 'completed']).count(),
        }

        per_day = {}
        for row in (appts.filter(created_at__gte=local_midnight(start))
                    .annotate(day=TruncDate('created_at')).values('day')
                    .annotate(total=Count('id'), online=Count('id', filter=Q(created_by_staff=False)))):
            per_day[row['day']] = row
        signups = {row['day']: row['n'] for row in (Company.objects.filter(created_at__gte=local_midnight(start))
                                                    .annotate(day=TruncDate('created_at')).values('day')
                                                    .annotate(n=Count('id')))}
        series = []
        for i in range(SERIES_DAYS):
            day = start + timedelta(days=i)
            row = per_day.get(day, {})
            series.append({'date': day.isoformat(), 'appointments': row.get('total', 0),
                           'online': row.get('online', 0), 'signups': signups.get(day, 0)})

        # Требуют внимания
        rows = {r['id']: r for r in salon_rows(c for c in companies if c.status != Company.STATUS_BLOCKED)}
        activity = last_activity(list(rows))
        inactive_before = now - timedelta(days=INACTIVE_DAYS)

        def pick(test):
            return [rows[c.id] for c in companies if c.id in rows and test(c, rows[c.id])][:20]

        attention = {
            'trial_ending': pick(lambda c, r: r['trial'] == 'ending'),
            'trial_expired': pick(lambda c, r: r['trial'] == 'expired'),
            'unverified': pick(lambda c, r: c.status == Company.STATUS_PENDING),
            'inactive': pick(lambda c, r: c.status == Company.STATUS_ACTIVE and (
                not activity.get(c.id) or activity[c.id] < inactive_before)),
            'not_ready': pick(lambda c, r: c.status == Company.STATUS_ACTIVE and (not r['masters'] or not r['services'])),
        }
        return Response({
            'salons': {'total': len(companies), 'by_status': by_status, 'trial': trial},
            'appointments': totals,
            'series': series,
            'attention': attention,
        })


class SalonListView(SuperView):
    """Список салонов: ?q= (название, адрес сайта, email, телефон), ?status=, ?trial=, ?sort=."""
    SORTS = {'new': '-created_at', 'old': 'created_at', 'name': 'name', 'trial': 'trial_ends_at'}

    def get(self, request):
        companies = Company.objects.all()
        q = str(request.query_params.get('q') or '').strip()
        if q:
            owner_companies = AdminProfile.objects.filter(
                Q(user__email__icontains=q) | Q(user__username__icontains=q)).values('company_id')
            companies = companies.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(phone__icontains=q)
                                         | Q(id__in=owner_companies))
        state = request.query_params.get('status')
        if state in dict(Company.STATUS_CHOICES):
            companies = companies.filter(status=state)
        companies = companies.order_by(self.SORTS.get(request.query_params.get('sort'), '-created_at'), 'id')
        rows = salon_rows(companies)
        trial = request.query_params.get('trial')
        if trial in ('active', 'ending', 'expired', 'none'):
            rows = [r for r in rows if r['trial'] == trial]
        if request.query_params.get('sort') == 'activity':
            rows.sort(key=lambda r: r['last_activity_at'] or '', reverse=True)
        return Response(rows)


def log_row(entry):
    return {'id': entry.id, 'at': entry.created_at.isoformat(), 'user': entry.user.username if entry.user else None,
            'action': entry.action, 'details': entry.details}


def setup_steps(company):
    masters = Master.objects.filter(company=company, is_active=True, services__is_active=True).distinct()
    staff_users = AdminProfile.objects.filter(company=company).values_list('user_id', flat=True)
    return [
        {'key': 'email', 'done': company.status != Company.STATUS_PENDING},
        {'key': 'salon', 'done': bool(company.address and company.phone)},
        {'key': 'services', 'done': Service.objects.filter(company=company, is_active=True).exists()},
        {'key': 'masters', 'done': masters.exists()},
        {'key': 'schedule', 'done': MasterSchedule.objects.filter(master__in=masters, is_working=True).exists()
         or MasterWorkDay.objects.filter(master__in=masters).exists()},
        {'key': 'telegram', 'done': StaffTelegram.objects.filter(user_id__in=staff_users).exists()},
    ]


def salon_detail(company):
    row = salon_rows([company])[0]
    users = staff_user_ids([company.id]).get(company.id, set())
    roles = dict(AdminProfile.objects.filter(company=company).values_list('user_id', 'role'))
    staff = [{
        'username': u.username, 'email': u.email, 'role': roles.get(u.pk, 'master'),
        'last_login': iso(u.last_login), 'is_active': u.is_active,
    } for u in User.objects.filter(pk__in=users).order_by('username')]
    row.update({
        'address': company.address,
        'description': company.description,
        'auto_confirm': company.auto_confirm,
        'terms_accepted_at': iso(company.terms_accepted_at),
        'staff': staff,
        'setup': setup_steps(company),
        'notes': [{'id': n.id, 'at': n.created_at.isoformat(), 'author': n.author.username if n.author else None,
                   'text': n.text} for n in SalonNote.objects.filter(company=company).select_related('author')[:50]],
        'log': [log_row(e) for e in SuperAdminLog.objects.filter(company=company).select_related('user')[:50]],
        'email_configured': settings.EMAIL_CONFIGURED,
    })
    return row


class SalonDetailView(SuperView):
    def get(self, request, pk):
        return Response(salon_detail(get_object_or_404(Company, pk=pk)))


def bad(message):
    return Response({'error': message}, status=status.HTTP_400_BAD_REQUEST)


class SalonActionView(SuperView):
    """POST /salons/<id>/<action>/: status, trial, resend-verification, password-reset, note."""

    def post(self, request, pk, action):
        company = get_object_or_404(Company, pk=pk)
        handler = {
            'status': self.set_status, 'trial': self.set_trial, 'resend-verification': self.resend,
            'password-reset': self.password_reset, 'note': self.note,
        }.get(action)
        if handler is None:
            return Response({'error': 'Неизвестное действие'}, status=status.HTTP_404_NOT_FOUND)
        error = handler(request, company)
        if error:
            return bad(error)
        return Response(salon_detail(company))

    def set_status(self, request, company):
        new = request.data.get('status')
        if new not in (Company.STATUS_ACTIVE, Company.STATUS_BLOCKED):
            return 'Статус: active или blocked'
        if new == company.status:
            return None
        reason = str(request.data.get('reason') or '').strip()[:300]
        if new == Company.STATUS_BLOCKED and not reason:
            return 'Укажите причину блокировки'
        old = company.status
        company.status = new
        company.save(update_fields=['status'])
        log(request, 'salon_blocked' if new == Company.STATUS_BLOCKED else 'salon_unblocked', company=company,
            details={'from': old, 'reason': reason})
        return None

    def set_trial(self, request, company):
        now = timezone.now()
        days = request.data.get('days')
        until = request.data.get('until')
        old = company.trial_ends_at
        if until:
            try:
                day = datetime.strptime(str(until), '%Y-%m-%d').date()
            except ValueError:
                return 'Дата в формате ГГГГ-ММ-ДД'
            if day <= timezone.localdate() or day > timezone.localdate() + timedelta(days=366):
                return 'Дата — от завтра и не дальше года'
            ends = timezone.make_aware(datetime.combine(day, time(23, 59)))
        else:
            try:
                days = int(days)
            except (TypeError, ValueError):
                return 'Укажите, на сколько дней продлить'
            if days not in TRIAL_PRESETS:
                return 'Продлить можно на 7, 14 или 30 дней'
            # Продление от сегодняшнего дня, если пробный период уже кончился
            ends = max(old or now, now) + timedelta(days=days)
        company.trial_ends_at = ends
        company.save(update_fields=['trial_ends_at'])
        log(request, 'trial_extended', company=company, details={'from': iso(old), 'to': iso(ends)})
        return None

    def resend(self, request, company):
        if company.status != Company.STATUS_PENDING:
            return 'Email уже подтверждён'
        owner = owners([company.id]).get(company.id)
        if owner is None or not owner.email:
            return 'У владельца нет email'
        if not send_verification(owner, company):
            return 'Не удалось отправить письмо — проверьте почту сервера'
        log(request, 'verification_resent', company=company, details={'email': owner.email})
        return None

    def password_reset(self, request, company):
        owner = owners([company.id]).get(company.id)
        if owner is None or not owner.email:
            return 'У владельца нет email'
        if not owner.is_active:
            return 'Учётная запись владельца отключена'
        if not send_password_reset(owner, by_support=True):
            return 'Не удалось отправить письмо — проверьте почту сервера'
        log(request, 'password_reset_sent', company=company, details={'email': owner.email})
        return None

    def note(self, request, company):
        text = str(request.data.get('text') or '').strip()
        if not text:
            return 'Пустая заметка'
        SalonNote.objects.create(company=company, author=request.user, text=text[:NOTE_MAX])
        log(request, 'note_added', company=company)
        return None


class ImpersonateView(SuperView):
    """Вход в панель салона от имени владельца: токен на 1 час, без продления. Пишется в журнал."""

    def post(self, request, pk):
        company = get_object_or_404(Company, pk=pk)
        if company.status == Company.STATUS_BLOCKED:
            return bad('Салон заблокирован — разблокируйте его, чтобы войти')
        owner = owners([company.id]).get(company.id)
        if owner is None or not owner.is_active:
            return bad('У салона нет активного владельца')
        token = get_impersonation_token(owner, request.user)
        log(request, 'impersonation_started', company=company, details={'as': owner.username})
        return Response({'access': str(token), 'expires_at': iso(timezone.now() + token.lifetime),
                         'salon': {'id': company.id, 'name': company.name}})
