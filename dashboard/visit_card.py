"""Действия карточки записи в панели: история изменений, повтор записи, напоминание клиенту,
ещё одна услуга в визите. Подмешиваются в AppointmentViewSet."""
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from bookings import audit
from bookings.i18n import tr
from bookings.models import Appointment, Master, Service
from bookings.slots import ACTIVE_STATUSES, BUSY_STATUSES
from bookings.terms import terms
from notifications import email as client_email, telegram, texts
from notifications import services as notifications
from .errors import StaffError
from .serializers import StaffAppointmentSerializer, StaffAppointmentWriteSerializer, calc_end_time, check_overlap

MAX_REPEATS = 12
REMIND_COOLDOWN = timedelta(minutes=10)


def int_param(data, key, low, high, message):
    try:
        value = int(data.get(key))
    except (TypeError, ValueError):
        raise StaffError(message)
    if not low <= value <= high:
        raise StaffError(message)
    return value


def copy_appointment(source, **changes):
    """Новая запись с теми же клиентом и условиями (без оплаты, заметки и напоминаний)."""
    fields = dict(
        company=source.company, master=source.master, service=source.service, date=source.date,
        start_time=source.start_time, end_time=source.end_time, status='confirmed', price=source.price,
        discount=source.discount, buffer_minutes=source.buffer_minutes, client_name=source.client_name,
        client_phone=source.client_phone, client_email=source.client_email, client=source.client,
        booker_name=source.booker_name, booker_phone=source.booker_phone, lang=source.lang,
        reminder_minutes=source.reminder_minutes, created_by_staff=True,
    )
    fields.update(changes)
    return Appointment.objects.create(**fields)


class AppointmentCardActions:
    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        appointment = self.get_object()
        return Response([audit.row(entry) for entry in appointment.logs.select_related('user')[:100]])

    @action(detail=True, methods=['post'])
    def repeat(self, request, pk=None):
        """Повторить запись: каждые every_days дней, count раз. Занятые даты пропускаются."""
        source = self.get_object()
        if source.master is None or source.service is None:
            raise StaffError('Укажите мастера и услугу')
        every = int_param(request.data, 'every_days', 1, 90, 'Интервал — от 1 до 90 дней')
        count = int_param(request.data, 'count', 1, MAX_REPEATS, f'Повторов — от 1 до {MAX_REPEATS}')
        created, skipped = [], []
        for i in range(1, count + 1):
            day = source.date + timedelta(days=every * i)
            try:
                with transaction.atomic():
                    check_overlap(source.master, day, source.start_time, source.end_time,
                                  buffer_minutes=source.buffer_minutes)
                    copy = copy_appointment(source, date=day)
                    audit.log(copy, 'created', request.user, details={'repeat_of': source.id})
                created.append(copy)
            except StaffError as exc:
                skipped.append({'date': day.isoformat(), 'reason': exc.detail.get('error', '')})
        if created:
            audit.log(source, 'repeated', request.user,
                      details={'dates': [a.date.isoformat() for a in created], 'skipped': len(skipped)})
        return Response({
            'created': StaffAppointmentSerializer(created, many=True, context=self.get_serializer_context()).data,
            'skipped': skipped,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def remind(self, request, pk=None):
        """Напомнить клиенту о визите сейчас: в Telegram, а если его нет — письмом."""
        appointment = self.get_object()
        if appointment.status not in ACTIVE_STATUSES or appointment.date < timezone.localdate():
            raise StaffError('Напомнить можно только о предстоящей записи')
        recent = appointment.logs.filter(action='reminded', created_at__gte=timezone.now() - REMIND_COOLDOWN)
        if recent.exists():
            raise StaffError('Напоминание уже отправлено — повторить можно через 10 минут')
        link = getattr(appointment, 'telegram', None) if hasattr(appointment, 'telegram') else None
        if link and link.chat_id and telegram.enabled():
            notifications.enqueue(link.chat_id, texts.reminder_24h(appointment), notifications.client_buttons(link))
            channel = 'telegram'
        elif appointment.client_email and client_email.enabled():
            lang = appointment.lang or 'ru'
            subject = tr('Напоминание о записи: {salon}', lang, salon=appointment.company.name)
            if not client_email.send(appointment, subject, client_email.body(appointment, 'Напоминаем о вашей записи.')):
                raise StaffError('Письмо не отправилось — попробуйте позже')
            channel = 'email'
        else:
            raise StaffError('У клиента нет Telegram и email — позвоните ему')
        audit.log(appointment, 'reminded', request.user, details={'channel': channel})
        return Response({'channel': channel})

    @action(detail=True, methods=['post'], url_path='add-service')
    def add_service(self, request, pk=None):
        """Ещё одна услуга в этом визите: сразу после последней услуги визита."""
        source = self.get_object()
        staff = self.staff
        try:
            service = Service.objects.get(pk=int(request.data.get('service')), company=staff.company)
        except (TypeError, ValueError, Service.DoesNotExist):
            raise StaffError('Выберите услугу')
        master = source.master
        if request.data.get('master') and staff.role != 'master':
            try:
                master = Master.objects.get(pk=int(request.data['master']), company=staff.company)
            except (TypeError, ValueError, Master.DoesNotExist):
                raise StaffError('Выберите мастера')
        if master is None:
            raise StaffError('Укажите мастера')
        StaffAppointmentWriteSerializer.check_master_service(master, service, True, True)
        duration, price = terms(master, service)
        with transaction.atomic():
            source = Appointment.objects.select_for_update().get(pk=source.pk)
            parts = Appointment.objects.filter(visit=source.visit, company=staff.company) if source.visit else None
            last = (parts.filter(status__in=BUSY_STATUSES).order_by('-end_time').first() if parts else None) or source
            start = last.end_time
            end = calc_end_time(source.date, start, duration)
            check_overlap(master, source.date, start, end)
            if not source.visit:
                source.visit = uuid.uuid4()
                source.save(update_fields=['visit'])
            status_value = source.status if source.status in BUSY_STATUSES else 'confirmed'
            added = copy_appointment(source, master=master, service=service, start_time=start, end_time=end,
                                     price=price, discount=0, buffer_minutes=0, visit=source.visit,
                                     status=status_value)
            audit.log(added, 'created', request.user, details={'visit_of': source.id})
            audit.log(source, 'service_added', request.user, details={'service': service.name})
        return Response(StaffAppointmentSerializer(added, context=self.get_serializer_context()).data,
                        status=status.HTTP_201_CREATED)
