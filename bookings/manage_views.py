"""Ссылка «Управлять записью»: клиент без входа видит запись, переносит или отменяет её.

Ссылка приходит на экране после записи и в Telegram; секрет — Appointment.manage_token.
Правила те же, что в личном кабинете: отмена — не позже cancel_hours до визита (поздняя отмена —
штраф по правилам салона); перенос — пока можно отменить без штрафа, иначе — звонок в салон.
"""
from datetime import datetime, timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from finance import penalties
from notifications import services as notifications
from .i18n import client_lang, tr
from .models import Appointment, Master
from .rules import cancel_rule_text, client_can_cancel, starts_at
from .security import TOO_MANY_MESSAGE, client_ip, hit
from .slots import ACTIVE_STATUSES, get_free_slots

# Запросов по ссылкам управления с одного IP в час (защита от перебора и спама)
MANAGE_PER_IP_HOUR = 300
RESCHEDULES_PER_APPOINTMENT_DAY = 5
LATE_RESCHEDULE_MESSAGE = 'Перенести запись онлайн уже нельзя. Позвоните в салон'


def can_reschedule(appointment, now=None):
    return client_can_cancel(appointment, now) and not penalties.is_late_cancel(appointment, now)


def appointment_data(appointment):
    company = appointment.company
    fee = penalties.late_cancel_amount(appointment)
    return {
        'id': appointment.id,
        'salon': {'name': company.name, 'slug': company.slug, 'address': company.address, 'phone': company.phone},
        'master': {'id': appointment.master_id, 'name': appointment.master.name} if appointment.master else None,
        'service': ({'id': appointment.service_id, 'name': appointment.service.name,
                     'duration': appointment.service.duration, 'price': str(appointment.service.price)}
                    if appointment.service else None),
        'date': appointment.date.isoformat(),
        'start_time': appointment.start_time.strftime('%H:%M'),
        'end_time': appointment.end_time.strftime('%H:%M'),
        'status': appointment.status,
        'can_cancel': client_can_cancel(appointment),
        'can_reschedule': can_reschedule(appointment),
        'late_cancel_penalty': str(fee) if fee else None,
    }


class ManageMixin:
    authentication_classes = []
    permission_classes = []

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if hit(f'manage:ip:{client_ip(request)}', MANAGE_PER_IP_HOUR, 3600):
            from rest_framework.exceptions import Throttled
            raise Throttled(detail={'error': TOO_MANY_MESSAGE})

    def get_appointment(self, token):
        return get_object_or_404(
            Appointment.objects.select_related('company', 'master', 'service'),
            manage_token=token, company__status='active',
        )


def error(message, request, code=status.HTTP_400_BAD_REQUEST, **params):
    return Response({'error': tr(message, client_lang(request), **params)}, status=code)


class ManageAppointmentView(ManageMixin, APIView):
    def get(self, request, token):
        return Response(appointment_data(self.get_appointment(token)))


class ManageCancelView(ManageMixin, APIView):
    def post(self, request, token):
        appointment = self.get_appointment(token)
        if appointment.status not in ACTIVE_STATUSES:
            return error('Эту запись нельзя отменить', request)
        if starts_at(appointment) <= timezone.now():
            return error('Прошедшую запись нельзя отменить', request)
        if not client_can_cancel(appointment):
            return Response({'error': cancel_rule_text(appointment.company, client_lang(request))},
                            status=status.HTTP_400_BAD_REQUEST)
        late = penalties.is_late_cancel(appointment)
        appointment.status = 'cancelled'
        appointment.save(update_fields=['status'])
        notifications.appointment_cancelled_by_client(appointment)
        if late:
            penalty = penalties.create(appointment, 'late_cancel')
            if penalty:
                notifications.penalty_created(penalty)
        return Response(appointment_data(appointment))


class ManageSlotsView(ManageMixin, APIView):
    """Свободное время того же мастера на ту же услугу для переноса (сама запись не мешает)."""

    def get(self, request, token):
        appointment = self.get_appointment(token)
        try:
            date = datetime.strptime(request.query_params.get('date') or '', '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Date format: YYYY-MM-DD'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_reschedule(appointment) or not appointment.master or not appointment.service:
            return Response({'date': date.isoformat(), 'slots': []})
        slots = get_free_slots(appointment.master, date, appointment.service.duration, exclude_id=appointment.id)
        return Response({'date': date.isoformat(), 'slots': [s.strftime('%H:%M') for s in slots]})


class ManageRescheduleView(ManageMixin, APIView):
    def post(self, request, token):
        appointment = self.get_appointment(token)
        if appointment.status not in ACTIVE_STATUSES or not appointment.master or not appointment.service:
            return error('Эту запись нельзя перенести', request)
        if not can_reschedule(appointment):
            return error(LATE_RESCHEDULE_MESSAGE, request)
        try:
            date = datetime.strptime(str(request.data.get('date') or ''), '%Y-%m-%d').date()
            start = datetime.strptime(str(request.data.get('start_time') or '')[:5], '%H:%M').time()
        except ValueError:
            return Response({'error': 'Format: date=YYYY-MM-DD, start_time=HH:MM'},
                            status=status.HTTP_400_BAD_REQUEST)
        if hit(f'manage:move:{appointment.id}', RESCHEDULES_PER_APPOINTMENT_DAY, 86400):
            return error('Запись переносили слишком часто. Позвоните в салон', request,
                         code=status.HTTP_429_TOO_MANY_REQUESTS)

        before = notifications.snapshot(appointment)
        with transaction.atomic():
            master = Master.objects.select_for_update().get(pk=appointment.master_id)
            duration = appointment.service.duration
            if start not in get_free_slots(master, date, duration, exclude_id=appointment.id):
                return error('Это время уже заняли. Выберите другое.', request)
            appointment.date = date
            appointment.start_time = start
            appointment.end_time = (datetime.combine(date, start) + timedelta(minutes=duration)).time()
            appointment.save(update_fields=['date', 'start_time', 'end_time'])
        notifications.appointment_rescheduled_by_client(before, appointment)
        return Response(appointment_data(appointment))
