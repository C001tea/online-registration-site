"""Портфолио в панели и на сайте записи.

Панель (любой сотрудник; мастер — только свои записи и свою галерею):
- GET/PATCH /api/staff/works/visit/<id>/ — блок «Работа» в карточке: фото, «Материалы и детали»,
  «в прошлый раз» (предыдущий визит этого клиента с фото или деталями);
- POST /api/staff/works/visit/<id>/photos/ — фото работы (до MAX_PER_VISIT);
- GET/POST /api/staff/works/master/<id>/ — галерея мастера, загрузка старых работ без визита;
- PATCH/DELETE /api/staff/works/<id>/ — «показывать на сайте» (мастер), «скрыть» (владелец, админ), удалить.
Сайт: GET /api/companies/<slug>/masters/<id>/works/ — фото, которые можно показывать.
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Appointment, Company, Master
from bookings.terms import terms
from dashboard.errors import StaffError
from dashboard.permissions import IsStaff
from dashboard.photos import prepare_image
from .models import WorkPhoto

MAX_PER_VISIT = 5
MAX_NOTES = 1000
PUBLIC_LIMIT = 60
GALLERY_LIMIT = 200
DISABLED_MESSAGE = 'Портфолио выключено. Включить: «Настройки» → «Правила записи» → «Портфолио работ»'


def photo_row(photo):
    return {
        'id': photo.id,
        'url': photo.image.url,
        'public': photo.public,
        'hidden': photo.hidden,
        'appointment': photo.appointment_id,
        'service': {'id': photo.service_id, 'name': photo.service.name} if photo.service else None,
        'created_at': photo.created_at.isoformat(),
    }


def require_enabled(company):
    if not company.portfolio_enabled:
        raise StaffError(DISABLED_MESSAGE, status.HTTP_403_FORBIDDEN)


def visit_of(request, pk):
    staff = request.staff
    appointments = Appointment.objects.filter(company=staff.company).select_related('service', 'master')
    if staff.role == 'master':
        appointments = appointments.filter(master=staff.master)
    appointment = appointments.filter(pk=pk).first()
    if not appointment:
        raise StaffError('Запись не найдена', status.HTTP_404_NOT_FOUND)
    return appointment


def master_of(request, pk):
    staff = request.staff
    if staff.role == 'master' and staff.master and staff.master.id != pk:
        raise StaffError('Можно менять только свою галерею', status.HTTP_403_FORBIDDEN)
    master = Master.objects.filter(company=staff.company, pk=pk).first()
    if not master:
        raise StaffError('Мастер не найден', status.HTTP_404_NOT_FOUND)
    return master


def save_photo(request, **fields):
    upload = request.FILES.get('file')
    if upload is None:
        raise StaffError('Выберите файл с фото')
    content = prepare_image(upload, 'image')
    photo = WorkPhoto(company=request.staff.company, created_by=request.user, **fields)
    photo.image.save(content.name, content, save=False)
    photo.save()
    return photo


def previous_visit(appointment, staff):
    """Прошлый визит клиента (раньше этой записи) с фото или «Материалами и деталями»."""
    if not appointment.client_phone:
        return None
    earlier = (Appointment.objects.filter(company=appointment.company, client_phone=appointment.client_phone,
                                          status='completed')
               .filter(Q(date__lt=appointment.date) | Q(date=appointment.date, start_time__lt=appointment.start_time))
               .filter(Q(work_photos__isnull=False) | ~Q(work_notes=''))
               .select_related('service', 'master').order_by('-date', '-start_time').distinct())
    if staff.role == 'master':
        earlier = earlier.filter(master=staff.master)
    visit = earlier.first()
    if not visit:
        return None
    return {
        'id': visit.id,
        'date': visit.date.isoformat(),
        'service': visit.service.name if visit.service else None,
        'master': visit.master.name if visit.master else None,
        'notes': visit.work_notes,
        'photos': [photo_row(p) for p in visit.work_photos.select_related('service')],
    }


class VisitWorkView(APIView):
    permission_classes = [IsStaff]

    def payload(self, appointment, staff):
        return {
            'enabled': appointment.company.portfolio_enabled,
            'notes': appointment.work_notes,
            'photos': [photo_row(p) for p in appointment.work_photos.select_related('service')],
            'max_photos': MAX_PER_VISIT,
            'previous': previous_visit(appointment, staff),
        }

    def get(self, request, pk):
        return Response(self.payload(visit_of(request, pk), request.staff))

    def patch(self, request, pk):
        appointment = visit_of(request, pk)
        require_enabled(appointment.company)
        notes = request.data.get('notes')
        if not isinstance(notes, str):
            raise StaffError('notes — текст')
        notes = notes.strip()
        if len(notes) > MAX_NOTES:
            raise StaffError(f'Не больше {MAX_NOTES} символов')
        appointment.work_notes = notes
        appointment.save(update_fields=['work_notes'])
        return Response(self.payload(appointment, request.staff))


class VisitPhotoView(APIView):
    permission_classes = [IsStaff]

    def post(self, request, pk):
        appointment = visit_of(request, pk)
        require_enabled(appointment.company)
        if not appointment.master_id:
            raise StaffError('У записи нет мастера — фото некуда отнести')
        if appointment.work_photos.count() >= MAX_PER_VISIT:
            raise StaffError(f'К визиту — не больше {MAX_PER_VISIT} фото')
        photo = save_photo(request, master_id=appointment.master_id, appointment=appointment,
                           service=appointment.service, public=request.data.get('public', 'true') != 'false')
        return Response(photo_row(photo), status=status.HTTP_201_CREATED)


class MasterWorksView(APIView):
    permission_classes = [IsStaff]

    def get(self, request, pk):
        master = master_of(request, pk)
        photos = master.work_photos.select_related('service')[:GALLERY_LIMIT]
        return Response({'enabled': request.staff.company.portfolio_enabled,
                         'results': [photo_row(p) for p in photos]})

    def post(self, request, pk):
        master = master_of(request, pk)
        require_enabled(request.staff.company)
        service_id = request.data.get('service')
        service = None
        if service_id:
            service = master.company.services.filter(pk=service_id).first() if str(service_id).isdecimal() else None
            if not service:
                raise StaffError('Услуга не найдена')
        photo = save_photo(request, master=master, service=service)
        return Response(photo_row(photo), status=status.HTTP_201_CREATED)


class WorkPhotoView(APIView):
    permission_classes = [IsStaff]

    def photo(self, request, pk):
        staff = request.staff
        photo = WorkPhoto.objects.filter(company=staff.company, pk=pk).select_related('service').first()
        if not photo or (staff.role == 'master' and photo.master_id != getattr(staff.master, 'id', None)):
            raise StaffError('Фото не найдено', status.HTTP_404_NOT_FOUND)
        return photo

    def patch(self, request, pk):
        photo = self.photo(request, pk)
        fields = []
        if 'public' in request.data:
            photo.public = bool(request.data['public'])
            fields.append('public')
        if 'hidden' in request.data:
            if not request.staff.is_manager:
                raise StaffError('Скрыть фото может владелец или администратор', status.HTTP_403_FORBIDDEN)
            photo.hidden = bool(request.data['hidden'])
            fields.append('hidden')
        if fields:
            photo.save(update_fields=fields)
        return Response(photo_row(photo))

    def delete(self, request, pk):
        self.photo(request, pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicMasterWorksView(APIView):
    """Работы мастера на сайте записи: только разрешённые и не скрытые, с услугой и ценой у этого мастера."""
    authentication_classes = []
    permission_classes = []

    def get(self, request, slug, master_id):
        company = get_object_or_404(Company.public, slug=slug)
        master = get_object_or_404(Master, pk=master_id, company=company, is_active=True)
        if not company.portfolio_enabled:
            return Response([])
        photos = (master.work_photos.filter(public=True, hidden=False)
                  .select_related('service')[:PUBLIC_LIMIT])
        rows = []
        for photo in photos:
            service = photo.service if photo.service and photo.service.is_active else None
            row = {'id': photo.id, 'url': photo.image.url, 'service': None}
            if service:
                row['service'] = {'id': service.id, 'name': service.name, 'price': str(terms(master, service)[1])}
            rows.append(row)
        return Response(rows)
