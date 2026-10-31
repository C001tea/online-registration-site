"""Клиентская база салона: клиенты — номера телефонов из записей этого салона."""
import re
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, F, Max, Min, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Appointment
from bookings.phone import normalize_phone
from bookings.slots import ACTIVE_STATUSES
from .errors import StaffError
from finance import penalties
from finance.models import METHOD_CHOICES, Penalty
from .models import ClientNote
from .permissions import NOT_ENOUGH_RIGHTS_MESSAGE, IsStaffManager
from .serializers import PHONE_REGION

PAGE_SIZE = 50
MAX_HISTORY = 200
SORTS = {
    'last': ('-last_date', 'client_phone'),
    'visits': ('-visits', '-last_date', 'client_phone'),
    # Без оплат (NULL) — в конце: в Postgres NULL при DESC идёт первым
    'paid': (F('paid_sum').desc(nulls_last=True), '-last_date', 'client_phone'),
    'name': ('name', 'client_phone'),
}
NOT_FOUND_MESSAGE = 'Клиент не найден'


def money(value):
    return str((value or Decimal('0')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def client_rows(company, **filters):
    """Записи салона, сгруппированные по телефону, со статистикой."""
    today = timezone.localdate()
    appointments = Appointment.objects.filter(company=company, **filters).exclude(client_phone='')
    latest_name = (Appointment.objects.filter(company=company, client_phone=OuterRef('client_phone'))
                   .order_by('-date', '-start_time', '-id').values('client_name')[:1])
    return appointments.values('client_phone').annotate(
        name=Subquery(latest_name),
        total=Count('id'),
        visits=Count('id', filter=Q(status='completed')),
        no_shows=Count('id', filter=Q(status='no_show')),
        cancelled=Count('id', filter=Q(status='cancelled')),
        paid_sum=Sum('payment__amount'),
        paid_count=Count('payment'),
        first_date=Min('date'),
        last_date=Max('date'),
        last_visit=Max('date', filter=Q(status='completed')),
        next_visit=Min('date', filter=Q(status__in=ACTIVE_STATUSES, date__gte=today)),
    )


def row_data(row):
    paid_count = row['paid_count']
    return {
        'phone': row['client_phone'],
        'name': row['name'] or '',
        'total': row['total'],
        'visits': row['visits'],
        'no_shows': row['no_shows'],
        'cancelled': row['cancelled'],
        'paid': money(row['paid_sum']),
        'average_check': money(row['paid_sum'] / paid_count) if paid_count else None,
        'first_date': row['first_date'].isoformat() if row['first_date'] else None,
        'last_visit': row['last_visit'].isoformat() if row['last_visit'] else None,
        'next_visit': row['next_visit'].isoformat() if row['next_visit'] else None,
    }


def phone_param(value):
    phone = normalize_phone((value or '').strip(), PHONE_REGION)
    if not phone:
        raise StaffError('Неверный номер телефона')
    return phone


class ClientListView(APIView):
    """GET ?q=имя или цифры телефона&sort=last|visits|paid|name&page=1 -> {count, page_size, results}"""
    permission_classes = [IsStaffManager]

    def get(self, request):
        company = request.staff.company
        rows = client_rows(company)
        query = (request.query_params.get('q') or '').strip()
        if query:
            # Номер ищем без ведущего 0: «069…» хранится как «+37369…»
            digits = re.sub(r'\D', '', query).lstrip('0')
            match = Q(client_name__icontains=query)
            if len(digits) >= 3:
                match |= Q(client_phone__contains=digits)
            phones = Appointment.objects.filter(company=company).filter(match).values('client_phone')
            rows = rows.filter(client_phone__in=phones)
        if request.query_params.get('blacklist') == '1':
            rows = rows.filter(client_phone__in=Penalty.objects.filter(
                company=company, status__in=penalties.ACTIVE).values('phone'))
        rows = rows.order_by(*SORTS.get(request.query_params.get('sort'), SORTS['last']))
        try:
            page = max(1, int(request.query_params.get('page') or 1))
        except ValueError:
            page = 1
        start = (page - 1) * PAGE_SIZE
        page_rows = list(rows[start:start + PAGE_SIZE])
        black = penalties.blacklist(company, [row['client_phone'] for row in page_rows])
        return Response({
            'count': rows.count(),
            'page_size': PAGE_SIZE,
            'results': [{**row_data(row), 'blacklist': penalties.blacklist_data(black.get(row['client_phone']))}
                        for row in page_rows],
        })


def most_frequent(appointments, field):
    row = (appointments.filter(status='completed').exclude(**{f'{field}__isnull': True})
           .values(f'{field}__name').annotate(n=Count('id')).order_by('-n', f'{field}__name').first())
    return row[f'{field}__name'] if row else None


class ClientCardView(APIView):
    """GET ?phone= -> статистика, заметка салона и история визитов клиента в этом салоне."""
    permission_classes = [IsStaffManager]

    def get(self, request):
        company = request.staff.company
        phone = phone_param(request.query_params.get('phone'))
        rows = list(client_rows(company, client_phone=phone))
        if not rows:
            raise StaffError(NOT_FOUND_MESSAGE, status.HTTP_404_NOT_FOUND)
        row = rows[0]
        appointments = Appointment.objects.filter(company=company, client_phone=phone)
        history = (appointments.select_related('service', 'master', 'payment')
                   .order_by('-date', '-start_time', '-id')[:MAX_HISTORY])
        note = ClientNote.objects.filter(company=company, phone=phone).first()
        return Response({
            **row_data(row),
            'top_master': most_frequent(appointments, 'master'),
            'top_service': most_frequent(appointments, 'service'),
            'note': note.text if note else '',
            'note_updated_at': note.updated_at.isoformat() if note else None,
            'appointments': [history_item(a) for a in history],
            'blacklist': penalties.blacklist_data(penalties.blacklist(company, [phone]).get(phone)),
            'penalties': [penalties.penalty_row(p) for p in Penalty.objects.filter(company=company, phone=phone)
                          .select_related('appointment__service', 'appointment__master').order_by('-created_at')],
        })


def history_item(a):
    payment = getattr(a, 'payment', None)
    return {
        'id': a.id,
        'date': a.date.isoformat(),
        'start_time': a.start_time.strftime('%H:%M'),
        'status': a.status,
        'service': a.service.name if a.service else None,
        'master': a.master.name if a.master else None,
        'price': str(a.service.price) if a.service else None,
        'paid': str(payment.amount) if payment else None,
        'notes': a.notes,
    }


class NoteSerializer(serializers.Serializer):
    phone = serializers.CharField()
    text = serializers.CharField(allow_blank=True, max_length=2000, trim_whitespace=True)


class ClientNoteView(APIView):
    """PUT {phone, text} — заметка о клиенте (видна только персоналу салона)."""
    permission_classes = [IsStaffManager]

    def put(self, request):
        serializer = NoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = request.staff.company
        phone = phone_param(serializer.validated_data['phone'])
        if not Appointment.objects.filter(company=company, client_phone=phone).exists():
            raise StaffError(NOT_FOUND_MESSAGE, status.HTTP_404_NOT_FOUND)
        note, _ = ClientNote.objects.update_or_create(
            company=company, phone=phone,
            defaults={'text': serializer.validated_data['text'], 'updated_by': request.user},
        )
        return Response({'note': note.text, 'note_updated_at': note.updated_at.isoformat()})


# --- Чёрный список: действия со штрафом ---

class PenaltyActionSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHOD_CHOICES, required=False, default='cash')
    note = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')


class PenaltyActionView(APIView):
    """POST pay {method} — штраф оплачен на месте (владелец, админ); DELETE pay — отменить оплату
    (только владелец); POST forgive {note} — простить (владелец, админ)."""
    permission_classes = [IsStaffManager]

    def penalty(self, request, pk):
        penalty = Penalty.objects.select_related('appointment').filter(company=request.staff.company, pk=pk).first()
        if penalty is None:
            raise StaffError('Штраф не найден', status.HTTP_404_NOT_FOUND)
        return penalty

    def post(self, request, pk, action):
        penalty = self.penalty(request, pk)
        serializer = PenaltyActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if penalty.status != 'unpaid':
            raise StaffError('Штраф уже оплачен или прощён')
        if action == 'pay':
            penalties.pay([penalty], method=serializer.validated_data['method'], user=request.user)
        else:
            penalties.forgive(penalty, user=request.user, note=serializer.validated_data['note'])
        penalty.refresh_from_db()
        return Response(penalties.penalty_row(penalty))

    def delete(self, request, pk, action):
        if action != 'pay':
            raise StaffError('Неизвестное действие', status.HTTP_404_NOT_FOUND)
        if request.staff.role != 'owner':
            raise StaffError(NOT_ENOUGH_RIGHTS_MESSAGE, status.HTTP_403_FORBIDDEN)
        penalty = self.penalty(request, pk)
        if penalty.status != 'paid':
            raise StaffError('Штраф не оплачен')
        penalties.cancel_payment(penalty)
        penalty.refresh_from_db()
        return Response(penalties.penalty_row(penalty))
