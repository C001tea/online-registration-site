import math
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status

from bookings.models import Appointment, ClientProfile, Company, Master, Service, ServiceCategory
from bookings.phone import normalize_phone
from bookings.slots import BUSY_STATUSES
from .errors import StaffError

# Регион для номеров без «+»: в панели можно вводить 069123456 или 69123456
PHONE_REGION = 'MD'
DEFAULT_DAY_START = time(9)
DEFAULT_DAY_END = time(18)
DAY_NAMES = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
# Отметить визит («пришёл» / «не пришёл») можно только в день записи или позже
VISIT_STATUSES = ('completed', 'no_show')


def hhmm_field(**kwargs):
    """Время в API персонала — ровно "HH:MM" и в ответах, и на вход (секунды → 400)."""
    return serializers.TimeField(format='%H:%M', input_formats=['%H:%M'], **kwargs)


def staff_company(context):
    staff = context.get('staff')
    return staff.company if staff else None


def minutes_between(start, end):
    delta = datetime.combine(datetime.min, end) - datetime.combine(datetime.min, start)
    return max(int(delta.total_seconds() // 60), 0)


def schedule_days(master):
    """График мастера ровно из 7 дней (0 — понедельник); отсутствующие дни — выходные 09:00–18:00."""
    by_day = {row.day_of_week: row for row in master.schedule.all()}
    days = []
    for day in range(7):
        row = by_day.get(day)
        days.append({
            'day_of_week': day,
            'is_working': bool(row and row.is_working),
            'start_time': (row.start_time if row else DEFAULT_DAY_START).strftime('%H:%M'),
            'end_time': (row.end_time if row else DEFAULT_DAY_END).strftime('%H:%M'),
        })
    return days


def staff_me(user, staff):
    master = staff.master
    return {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or (master.name if master else '') or user.username,
        'role': staff.role,
        'company': {'id': staff.company.id, 'slug': staff.company.slug, 'name': staff.company.name},
        'master_id': master.id if master else None,
    }


# --- Вход и пароль ---

class StaffLoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(trim_whitespace=False, max_length=128)


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(trim_whitespace=False, max_length=128)
    new_password = serializers.CharField(trim_whitespace=False, max_length=128)


# --- Записи ---

class StaffAppointmentSerializer(serializers.ModelSerializer):
    """Элемент журнала (ответы всех эндпоинтов записей)."""
    start_time = hhmm_field(read_only=True)
    end_time = hhmm_field(read_only=True)
    duration = serializers.SerializerMethodField()
    master = serializers.SerializerMethodField()
    service = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'id', 'date', 'start_time', 'end_time', 'duration', 'status', 'master', 'service',
            'client_name', 'client_phone', 'client_email', 'notes', 'source', 'created_at',
        ]
        read_only_fields = fields

    def get_duration(self, obj):
        return minutes_between(obj.start_time, obj.end_time)

    def get_master(self, obj):
        if obj.master is None:
            return None
        return {'id': obj.master.id, 'name': obj.master.name, 'is_active': obj.master.is_active}

    def get_service(self, obj):
        if obj.service is None:
            return None
        return {
            'id': obj.service.id, 'name': obj.service.name,
            'price': str(obj.service.price.quantize(Decimal('0.01'))), 'is_active': obj.service.is_active,
        }

    def get_source(self, obj):
        return 'staff' if obj.created_by_staff else 'online'


def client_for_phone(phone):
    """Аккаунт клиента с этим телефоном (если клиент зарегистрирован)."""
    profile = ClientProfile.objects.filter(phone=phone).select_related('user').first()
    return profile.user if profile else None


def calc_end_time(date, start_time, duration):
    end = datetime.combine(date, start_time) + timedelta(minutes=duration)
    if end.date() != date:
        raise StaffError('Запись должна закончиться до полуночи')
    return end.time()


def check_visit_date(status_value, date):
    if status_value in VISIT_STATUSES and date > timezone.localdate():
        raise StaffError('Отметить визит можно только в день записи или позже')


def check_overlap(master, date, start_time, end_time, exclude_pk=None):
    """Пересечение с занимающими время записями мастера. Вызывать внутри transaction.atomic()."""
    # Блокируем мастера, чтобы две одновременные записи не заняли одно время
    Master.objects.select_for_update().get(pk=master.pk)
    overlapping = Appointment.objects.filter(
        master=master, date=date, status__in=BUSY_STATUSES,
        start_time__lt=end_time, end_time__gt=start_time,  # касание концами допустимо
    )
    if exclude_pk is not None:
        overlapping = overlapping.exclude(pk=exclude_pk)
    other = overlapping.order_by('start_time').first()
    if other:
        raise StaffError(
            f'Пересекается с записью {other.start_time:%H:%M}–{other.end_time:%H:%M} ({other.client_name})'
        )


class StaffAppointmentWriteSerializer(serializers.Serializer):
    """Создание (POST) и изменение (PATCH, partial=True) записи персоналом.

    Поля, которых здесь нет (company, client, created_by_staff, end_time, ...), из тела не читаются.
    """
    master = serializers.PrimaryKeyRelatedField(queryset=Master.objects.none())
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.none())
    date = serializers.DateField()
    start_time = hhmm_field()
    duration = serializers.IntegerField(min_value=5, max_value=600, required=False)
    client_name = serializers.CharField(max_length=200)
    client_phone = serializers.CharField(max_length=32)
    client_email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    status = serializers.ChoiceField(choices=Appointment.STATUS_CHOICES, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = staff_company(self.context)
        # Мастер и услуга — только этого салона (скрытые тоже: у старых записей они остаются)
        self.fields['master'].queryset = Master.objects.filter(company=company)
        self.fields['service'].queryset = Service.objects.filter(company=company)
        staff = self.context.get('staff')
        if staff and staff.role == 'master' and self.instance is None:
            # Мастер записывает только к себе и сразу подтверждённо
            self.fields.pop('master')
            self.fields.pop('status')

    def validate_client_phone(self, value):
        phone = normalize_phone(value.strip(), PHONE_REGION)
        if not phone:
            raise serializers.ValidationError('Неверный номер телефона')
        return phone

    @staticmethod
    def check_master_service(master, service, master_changed, service_changed):
        if master_changed and not master.is_active:
            raise serializers.ValidationError({'master': ['Мастер скрыт — выберите другого']})
        if service_changed and not service.is_active:
            raise serializers.ValidationError({'service': ['Услуга скрыта — выберите другую']})
        # Пара мастер–услуга проверяется, если изменилось хоть что-то из двух
        if (master_changed or service_changed) and master is not None and service is not None \
                and not master.services.filter(pk=service.pk).exists():
            raise serializers.ValidationError({'service': ['Мастер не оказывает эту услугу']})

    def create(self, validated_data):
        staff = self.context['staff']
        data = dict(validated_data)
        if staff.role == 'master':
            data['master'] = staff.master
            data['status'] = 'confirmed'
            if data['date'] < timezone.localdate():
                raise StaffError('Нельзя создать запись на прошедшую дату')

        master, service, date = data['master'], data['service'], data['date']
        self.check_master_service(master, service, True, True)
        duration = data.get('duration', service.duration)
        end_time = calc_end_time(date, data['start_time'], duration)
        status_value = data.get('status', 'confirmed')
        check_visit_date(status_value, date)

        with transaction.atomic():
            if status_value in BUSY_STATUSES:
                check_overlap(master, date, data['start_time'], end_time)
            return Appointment.objects.create(
                company=staff.company,
                master=master,
                service=service,
                date=date,
                start_time=data['start_time'],
                end_time=end_time,
                status=status_value,
                client_name=data['client_name'],
                client_phone=data['client_phone'],
                client_email=data.get('client_email', ''),
                notes=data.get('notes', ''),
                client=client_for_phone(data['client_phone']),
                created_by_staff=True,
            )

    def update(self, instance, validated_data):
        staff = self.context['staff']
        data = dict(validated_data)
        old_duration = minutes_between(instance.start_time, instance.end_time)
        current = {
            'master': instance.master, 'service': instance.service, 'date': instance.date,
            'start_time': instance.start_time, 'duration': old_duration, 'status': instance.status,
            'client_name': instance.client_name, 'client_phone': instance.client_phone,
            'client_email': instance.client_email, 'notes': instance.notes,
        }
        # Поля с тем же значением, что и сейчас, считаем не переданными
        changes = {key: value for key, value in data.items() if value != current[key]}

        if staff.role == 'master':
            forbidden = set(changes) - {'status', 'notes'}
            if forbidden:
                raise StaffError('Мастер может менять только статус и заметку', status.HTTP_403_FORBIDDEN)

        master = changes.get('master', instance.master)
        service = changes.get('service', instance.service)
        master_changed, service_changed = 'master' in changes, 'service' in changes
        # Активность и «оказывает услугу» — только для того, что меняется
        self.check_master_service(master, service, master_changed, service_changed)

        date = data.get('date', instance.date)
        start_time = data.get('start_time', instance.start_time)
        if 'duration' in data:
            duration = data['duration']
        elif service_changed:
            duration = service.duration
        else:
            duration = old_duration
        timing_changed = date != instance.date or start_time != instance.start_time or duration != old_duration

        if master is None and (timing_changed or service_changed):
            raise StaffError('Укажите мастера')

        end_time = calc_end_time(date, start_time, duration) if timing_changed else instance.end_time
        status_value = data.get('status', instance.status)
        if 'status' in changes or date != instance.date:
            check_visit_date(status_value, date)

        need_overlap_check = (
            master is not None and status_value in BUSY_STATUSES
            and (master_changed or service_changed or timing_changed or instance.status not in BUSY_STATUSES)
        )

        with transaction.atomic():
            if need_overlap_check:
                check_overlap(master, date, start_time, end_time, exclude_pk=instance.pk)
            instance.master = master
            instance.service = service
            instance.date = date
            instance.start_time = start_time
            instance.end_time = end_time
            instance.status = status_value
            for field in ('client_name', 'client_email', 'notes'):
                if field in changes:
                    setattr(instance, field, changes[field])
            if 'client_phone' in changes:
                instance.client_phone = changes['client_phone']
                instance.client = client_for_phone(instance.client_phone)
            instance.save()
        return instance


# --- Справочники ---

class StaffCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceCategory
        fields = ['id', 'name']


class StaffServiceSerializer(serializers.ModelSerializer):
    category = serializers.PrimaryKeyRelatedField(
        queryset=ServiceCategory.objects.none(), allow_null=True, required=False
    )
    category_name = serializers.SerializerMethodField()
    duration = serializers.IntegerField(min_value=5, max_value=600)
    price = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('0'))

    class Meta:
        model = Service
        fields = ['id', 'name', 'category', 'category_name', 'duration', 'price', 'is_active']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['category'].queryset = ServiceCategory.objects.filter(company=staff_company(self.context))

    def get_category_name(self, obj):
        return obj.category.name if obj.category else None


class StaffMasterSerializer(serializers.ModelSerializer):
    photo = serializers.SerializerMethodField()
    services = serializers.PrimaryKeyRelatedField(many=True, queryset=Service.objects.none(), required=False)
    schedule = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()
    has_access = serializers.SerializerMethodField()

    class Meta:
        model = Master
        fields = ['id', 'name', 'description', 'is_active', 'photo', 'services', 'schedule', 'user', 'has_access']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['services'].child_relation.queryset = Service.objects.filter(company=staff_company(self.context))

    def get_photo(self, obj):
        return obj.photo.url if obj.photo else None

    def get_schedule(self, obj):
        return schedule_days(obj)

    def get_user(self, obj):
        return obj.user.username if obj.user_id else None

    def get_has_access(self, obj):
        return obj.user_id is not None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['services'] = sorted(data['services'])
        staff = self.context.get('staff')
        if not staff or not staff.is_manager:
            # Логин мастера видят только владелец и администраторы
            data.pop('user', None)
        return data

    def update(self, instance, validated_data):
        services = validated_data.pop('services', None)
        instance = super().update(instance, validated_data)
        if services is not None:
            # Заменяем только связи с активными услугами: связи со скрытыми не теряются молча
            hidden = set(instance.services.filter(is_active=False))
            instance.services.set(set(services) | hidden)
        return instance


class ScheduleDaySerializer(serializers.Serializer):
    day_of_week = serializers.IntegerField(min_value=0, max_value=6)
    is_working = serializers.BooleanField()
    start_time = hhmm_field(required=False)
    end_time = hhmm_field(required=False)

    def validate(self, attrs):
        if attrs['is_working']:
            start, end = attrs.get('start_time'), attrs.get('end_time')
            if start is None or end is None:
                raise serializers.ValidationError('укажите время начала и окончания')
            if start >= end:
                raise serializers.ValidationError('время начала должно быть раньше окончания')
        return attrs


class DayOffInputSerializer(serializers.Serializer):
    date = serializers.DateField()


def day_off_data(day_off):
    return {'id': day_off.id, 'master': day_off.master_id, 'date': day_off.date.isoformat()}


class MasterAccessSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, validators=[UnicodeUsernameValidator()])
    password = serializers.CharField(trim_whitespace=False, max_length=128)


# --- Компания ---

class CoordinateField(serializers.FloatField):
    """Координата числом или null; "" → null. Сохраняется с точностью 6 знаков."""

    def validate_empty_values(self, data):
        if isinstance(data, str) and not data.strip():
            data = None
        return super().validate_empty_values(data)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not math.isfinite(value):
            self.fail('invalid')
        return value

    def run_validation(self, data=serializers.empty):
        value = super().run_validation(data)
        if value is None:
            return None
        # Из Google Maps приходят 7 знаков после точки, в базе — 6
        return Decimal(repr(value)).quantize(Decimal('0.000001'), rounding=ROUND_HALF_UP)


class StaffCompanySerializer(serializers.ModelSerializer):
    latitude = CoordinateField(allow_null=True, required=False, min_value=-90, max_value=90)
    longitude = CoordinateField(allow_null=True, required=False, min_value=-180, max_value=180)

    class Meta:
        model = Company
        fields = ['id', 'slug', 'name', 'address', 'phone', 'description', 'latitude', 'longitude']
        read_only_fields = ['id', 'slug']
