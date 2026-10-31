import math
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status

from bookings.models import Appointment, ClientProfile, Company, Master, Service, ServiceCategory
from bookings.phone import normalize_phone
from bookings.slots import BUSY_STATUSES
from finance import penalties
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


def hhmm(value):
    return value.strftime('%H:%M') if value else None


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
            'break_start': hhmm(row.break_start) if row else None,
            'break_end': hhmm(row.break_end) if row else None,
        })
    return days


def validate_break(attrs, start, end):
    """Обед: оба времени или ни одного, внутри рабочего дня, начало раньше конца."""
    break_start, break_end = attrs.get('break_start'), attrs.get('break_end')
    if (break_start is None) != (break_end is None):
        raise serializers.ValidationError('укажите и начало, и конец обеда (или ни то, ни другое)')
    if break_start is not None:
        if break_start >= break_end:
            raise serializers.ValidationError('начало обеда должно быть раньше конца')
        if break_start <= start or break_end >= end:
            raise serializers.ValidationError('обед должен быть внутри рабочего дня')
    attrs['break_start'], attrs['break_end'] = break_start, break_end


def staff_me(user, staff):
    master = staff.master
    return {
        'id': user.id,
        'username': user.username,
        'name': user.get_full_name() or (master.name if master else '') or user.username,
        'role': staff.role,
        'company': {'id': staff.company.id, 'slug': staff.company.slug, 'name': staff.company.name,
                    # Чёрный список: панель спрашивает про штраф при «Не пришёл» и поздней отмене
                    'penalty_percent': staff.company.penalty_percent,
                    'late_cancel_hours': staff.company.late_cancel_hours},
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
    payment = serializers.SerializerMethodField()
    blacklist = serializers.SerializerMethodField()
    penalty = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'id', 'date', 'start_time', 'end_time', 'duration', 'status', 'master', 'service',
            'client_name', 'client_phone', 'client_email', 'notes', 'source', 'created_at', 'payment',
            'blacklist', 'penalty',
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

    def get_payment(self, obj):
        payment = paid(obj)
        if payment is None:
            return None
        return {'amount': str(payment.amount), 'discount': str(payment.discount), 'method': payment.method}

    def get_blacklist(self, obj):
        """Клиент в чёрном списке салона: {debt, count} или None. Список журнала передаёт
        всё сразу в context['blacklist'], одиночный ответ считает сам."""
        known = self.context.get('blacklist')
        if known is None:
            known = penalties.blacklist(obj.company, [obj.client_phone])
        return penalties.blacklist_data(known.get(obj.client_phone))

    def get_penalty(self, obj):
        """Штраф, начисленный по этой записи (не пришёл / поздняя отмена)."""
        penalty = getattr(obj, 'penalty', None)  # нет штрафа — None (hasattr/getattr ловят DoesNotExist)
        if penalty is None:
            return None
        return {'id': penalty.id, 'kind': penalty.kind, 'status': penalty.status, 'amount': str(penalty.amount)}


def paid(appointment):
    """Оплата визита или None (обратная связь OneToOne без оплаты бросает исключение)."""
    try:
        return appointment.payment
    except ObjectDoesNotExist:
        return None


def client_for_phone(phone):
    """Аккаунт клиента с этим телефоном (если клиент зарегистрирован)."""
    profile = ClientProfile.objects.filter(phone=phone).select_related('user').first()
    return profile.user if profile else None


def calc_end_time(date, start_time, duration):
    # Считаем в минутах, без datetime: для 9999-12-31 datetime + timedelta переполняется (500)
    end_minutes = start_time.hour * 60 + start_time.minute + duration
    if end_minutes >= 24 * 60:
        raise StaffError('Запись должна закончиться до полуночи')
    return time(end_minutes // 60, end_minutes % 60, start_time.second)


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
        with transaction.atomic():
            # instance загружен до начала транзакции и мог устареть (параллельная отмена, перенос):
            # всё считаем от свежей строки под блокировкой и пишем только изменённые поля
            instance = Appointment.objects.select_for_update().get(pk=instance.pk)
            if staff.role == 'master' and instance.master_id != staff.master.id:
                # Запись успели перевести к другому мастеру
                raise StaffError('Запись не найдена', status.HTTP_404_NOT_FOUND)
            return self.apply_update(instance, data, staff)

    def apply_update(self, instance, data, staff):
        """Изменение записи; вызывается внутри transaction.atomic() с заблокированной instance."""
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
        if need_overlap_check:
            # Блокирует мастера (куда ставим запись) и проверяет пересечения
            check_overlap(master, date, start_time, end_time, exclude_pk=instance.pk)

        updates = {
            'master': master, 'service': service, 'date': date, 'start_time': start_time,
            'end_time': end_time, 'status': status_value,
        }
        for field in ('client_name', 'client_email', 'notes'):
            if field in changes:
                updates[field] = changes[field]
        if 'client_phone' in changes:
            updates['client_phone'] = changes['client_phone']
            updates['client'] = client_for_phone(changes['client_phone'])

        changed_fields = [field for field, value in updates.items() if getattr(instance, field) != value]
        for field in changed_fields:
            setattr(instance, field, updates[field])
        if changed_fields:
            # Только изменённые колонки: PATCH заметки не перезапишет статус или время
            instance.save(update_fields=changed_fields)
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
    break_start = hhmm_field(required=False, allow_null=True)
    break_end = hhmm_field(required=False, allow_null=True)

    def validate(self, attrs):
        start, end = attrs.get('start_time'), attrs.get('end_time')
        if attrs['is_working'] and (start is None or end is None):
            raise serializers.ValidationError('укажите время начала и окончания')
        # Для нерабочего дня время можно не передавать — подставляем 09:00–18:00.
        # Порядок проверяем для любого дня: иначе сохранится перевёрнутое время, и день потом
        # нельзя будет просто отметить рабочим
        attrs['start_time'] = start or DEFAULT_DAY_START
        attrs['end_time'] = end or DEFAULT_DAY_END
        if attrs['start_time'] >= attrs['end_time']:
            raise serializers.ValidationError('время начала должно быть раньше окончания')
        if attrs['is_working']:
            validate_break(attrs, attrs['start_time'], attrs['end_time'])
        else:
            attrs['break_start'] = attrs['break_end'] = None  # у нерабочего дня обеда нет
        return attrs


# Календарь мастера: на сколько дней вперёд можно расставлять график и сколько дней за раз
MAX_SCHEDULE_DAYS_AHEAD = 366
MAX_CALENDAR_RANGE = 62
DAY_MODES = ('custom', 'off', 'template')


class WorkDaysInputSerializer(serializers.Serializer):
    """Применить к выбранным датам: custom — свои часы и обед, off — выходной, template — как в неделе."""
    dates = serializers.ListField(child=serializers.DateField(), min_length=1, max_length=MAX_CALENDAR_RANGE)
    mode = serializers.ChoiceField(choices=DAY_MODES)
    start_time = hhmm_field(required=False)
    end_time = hhmm_field(required=False)
    break_start = hhmm_field(required=False, allow_null=True)
    break_end = hhmm_field(required=False, allow_null=True)

    def validate_dates(self, value):
        today = timezone.localdate()
        if any(d < today for d in value):
            raise serializers.ValidationError('прошедшие дни менять нельзя')
        if any((d - today).days > MAX_SCHEDULE_DAYS_AHEAD for d in value):
            raise serializers.ValidationError('не дальше чем на год вперёд')
        return sorted(set(value))

    def validate(self, attrs):
        if attrs['mode'] != 'custom':
            return attrs
        start, end = attrs.get('start_time'), attrs.get('end_time')
        if start is None or end is None:
            raise serializers.ValidationError('укажите время начала и окончания')
        if start >= end:
            raise serializers.ValidationError('время начала должно быть раньше окончания')
        validate_break(attrs, start, end)
        return attrs


def day_schedule_data(date, schedule, appointments_count=0):
    return {
        'date': date.isoformat(),
        'source': schedule.source,
        'is_working': schedule.is_working,
        'start_time': hhmm(schedule.start),
        'end_time': hhmm(schedule.end),
        'break_start': hhmm(schedule.break_start),
        'break_end': hhmm(schedule.break_end),
        'appointments': appointments_count,
    }


def work_day_data(row):
    return {
        'master': row.master_id, 'date': row.date.isoformat(),
        'start_time': hhmm(row.start_time), 'end_time': hhmm(row.end_time),
        'break_start': hhmm(row.break_start), 'break_end': hhmm(row.break_end),
    }


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
    # Фото загружаются отдельно: /company/logo/, /company/main-photo/
    logo = serializers.SerializerMethodField()
    main_photo = serializers.SerializerMethodField()

    class Meta:
        model = Company
        fields = ['id', 'slug', 'name', 'address', 'phone', 'description', 'latitude', 'longitude', 'cancel_hours',
                  'penalty_percent', 'late_cancel_hours', 'auto_confirm', 'logo', 'main_photo']
        read_only_fields = ['id', 'slug']
        extra_kwargs = {'cancel_hours': {'max_value': 168}, 'penalty_percent': {'max_value': 100},
                        'late_cancel_hours': {'max_value': 48}}

    def get_logo(self, obj):
        return obj.logo.url if obj.logo else None

    def get_main_photo(self, obj):
        return obj.main_photo.url if obj.main_photo else None
