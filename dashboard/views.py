from datetime import datetime, timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.views import TokenRefreshView

from bookings.login_limit import (
    LOGIN_BLOCKED_MESSAGE, increment_counter, is_login_blocked, login_limit_key,
    register_login_failure, reset_login_failures,
)
from bookings.models import (
    AdminProfile, Appointment, ClientProfile, Master, MasterDayOff, MasterSchedule, Service, ServiceCategory
)
from bookings.phone import normalize_phone
from bookings.slots import ACTIVE_STATUSES, get_free_slots
from notifications import services as notifications
from .errors import StaffError
from .permissions import IsStaff, IsStaffManagerOrReadOnly, get_staff, NOT_ENOUGH_RIGHTS_MESSAGE
from .serializers import (
    PHONE_REGION, DAY_NAMES,
    StaffLoginSerializer, PasswordChangeSerializer, StaffAppointmentSerializer, StaffAppointmentWriteSerializer,
    StaffCategorySerializer, StaffServiceSerializer, StaffMasterSerializer, ScheduleDaySerializer,
    DayOffInputSerializer, MasterAccessSerializer, StaffCompanySerializer,
    staff_me, schedule_days, day_off_data,
)
from .tokens import STAFF_SCOPE, StaffRefreshToken, get_staff_tokens, token_matches_password

STATUS_KEYS = [key for key, _ in Appointment.STATUS_CHOICES]
CLIENT_ACCOUNT_MESSAGE = 'Это клиентский аккаунт — пароль меняет сам пользователь'
# Журнал отдаётся без пагинации, поэтому диапазон дат ограничен
MAX_JOURNAL_DAYS = 30
# Список новых записей (бейдж «Новые»): без ограничения по дате, но не больше стольких строк
MAX_PENDING_ITEMS = 200
# Поиск клиента по телефону: не больше LOOKUP_LIMIT запросов в час на пользователя панели
LOOKUP_LIMIT = 120
LOOKUP_WINDOW_SECONDS = 60 * 60
TOO_MANY_REQUESTS_MESSAGE = 'Слишком много запросов, попробуйте позже'
PASSWORD_BLOCKED_MESSAGE = 'Слишком много попыток. Попробуйте через 15 минут'
LOGIN_FAILED_MESSAGE = 'Неверный логин или пароль'


def parse_date(value):
    try:
        return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_int(value):
    value = (value or '').strip()
    return int(value) if value.isdecimal() else None


def date_range_params(params):
    """date_from (обязателен) и date_to (по умолчанию = date_from) из query-параметров."""
    date_from = parse_date(params.get('date_from'))
    if not date_from:
        raise StaffError('Укажите date_from в формате YYYY-MM-DD')
    date_to = date_from
    if params.get('date_to'):
        date_to = parse_date(params.get('date_to'))
        if not date_to:
            raise StaffError('date_to — дата в формате YYYY-MM-DD')
    if date_to < date_from:
        raise StaffError('date_to не может быть раньше date_from')
    return date_from, date_to


def password_errors(field, password, user):
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise ValidationError({field: list(exc.messages)})


class StaffViewMixin:
    """Контекст сериализаторов: персонал из request.staff (кладёт IsStaff)."""

    @property
    def staff(self):
        return self.request.staff

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['staff'] = getattr(self.request, 'staff', None)
        return context


# --- Вход ---

class StaffLoginView(APIView):
    # Без аутентификации: просроченный/битый Bearer в заголовке не мешает войти
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = StaffLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data['username']
        password = serializer.validated_data['password']

        # Счётчик — по аккаунту, в который ведёт логин: любое написание телефона даёт один ключ,
        # общий со входом клиента (/api/auth/login/). Проверяется до любой попытки authenticate().
        limit_key = login_limit_key(username, PHONE_REGION)
        if is_login_blocked(limit_key):
            return Response({'error': LOGIN_BLOCKED_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        user = authenticate(request, username=username, password=password)
        if user is None:
            # Мастер с клиентским аккаунтом входит по телефону (логин клиента — телефон в E.164)
            phone = normalize_phone(username, PHONE_REGION)
            if phone and phone != username:
                user = authenticate(request, username=phone, password=password)

        staff = get_staff(user) if user is not None else None
        if staff is None:
            # Верный пароль от аккаунта без доступа к панели (например, клиентского) — тот же ответ
            # и та же неудачная попытка: по этому эндпоинту нельзя проверять пароли клиентов
            register_login_failure(limit_key)
            return Response({'error': LOGIN_FAILED_MESSAGE}, status=status.HTTP_400_BAD_REQUEST)

        reset_login_failures(limit_key)
        return Response({**get_staff_tokens(user), 'me': staff_me(user, staff)})


class StaffTokenRefreshSerializer(TokenRefreshSerializer):
    token_class = StaffRefreshToken

    def validate(self, attrs):
        refresh = self.token_class(attrs['refresh'])
        if refresh.get('scope') != STAFF_SCOPE:
            raise InvalidToken('Это не токен панели салона')
        user = User.objects.filter(pk=refresh.get(api_settings.USER_ID_CLAIM)).first()
        if user is None:
            raise InvalidToken('Пользователь не найден')
        if not token_matches_password(refresh, user):
            # Пароль сменили или сбросили после выдачи токена
            raise InvalidToken('Пароль изменён — войдите снова')
        try:
            return super().validate(attrs)
        except User.DoesNotExist:
            raise InvalidToken('Пользователь не найден')


class StaffTokenRefreshView(TokenRefreshView):
    authentication_classes = []
    serializer_class = StaffTokenRefreshSerializer


class StaffPasswordView(APIView):
    permission_classes = [IsStaff]

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        # Перебор текущего пароля с украденным токеном: не больше 5 неверных за 15 минут
        limit_key = f'staff-password:{user.pk}'
        if is_login_blocked(limit_key):
            return Response({'error': PASSWORD_BLOCKED_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        if not user.check_password(serializer.validated_data['old_password']):
            register_login_failure(limit_key)
            raise ValidationError({'old_password': ['Неверный текущий пароль']})
        reset_login_failures(limit_key)
        new_password = serializer.validated_data['new_password']
        password_errors('new_password', new_password, user)
        user.set_password(new_password)
        user.save(update_fields=['password'])
        # Старые токены (в т.ч. на других устройствах) после смены пароля не действуют —
        # тому, кто сменил пароль, выдаём новые, чтобы он остался в панели
        return Response(get_staff_tokens(user))


class StaffMeView(APIView):
    permission_classes = [IsStaff]

    def get(self, request):
        return Response(staff_me(request.user, request.staff))


# --- Журнал записей ---

class AppointmentViewSet(StaffViewMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [IsStaff]
    serializer_class = StaffAppointmentSerializer
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        queryset = Appointment.objects.filter(company=self.staff.company).select_related('master', 'service')
        if self.staff.role == 'master':
            # Мастер видит и меняет только свои записи
            queryset = queryset.filter(master=self.staff.master)
        return queryset

    def list(self, request):
        params = request.query_params
        date_from, date_to = date_range_params(params)
        if (date_to - date_from).days > MAX_JOURNAL_DAYS:
            raise StaffError(f'Диапазон дат — не больше {MAX_JOURNAL_DAYS + 1} дня')

        queryset = self.get_queryset().filter(date__gte=date_from, date__lte=date_to)
        if params.get('master') and self.staff.role != 'master':
            master_id = parse_int(params.get('master'))
            if master_id is None:
                raise StaffError('master — id мастера (целое число)')
            queryset = queryset.filter(master_id=master_id)
        if params.get('status'):
            statuses = [value.strip() for value in params['status'].split(',') if value.strip()]
            unknown = [value for value in statuses if value not in STATUS_KEYS]
            if unknown:
                raise StaffError(f'Неизвестный статус: {", ".join(unknown)}')
            queryset = queryset.filter(status__in=statuses)

        queryset = queryset.order_by('date', 'start_time', 'id')
        return Response(self.get_serializer(queryset, many=True).data)

    def create(self, request):
        serializer = StaffAppointmentWriteSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        appointment = serializer.save()
        return Response(StaffAppointmentSerializer(appointment).data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        appointment = self.get_object()
        before = notifications.snapshot(appointment)
        serializer = StaffAppointmentWriteSerializer(
            appointment, data=request.data, partial=True, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        appointment = serializer.save()
        # Отменили или перенесли — клиенту в Telegram, если он подключён
        notifications.appointment_changed_by_staff(before, appointment)
        return Response(StaffAppointmentSerializer(appointment).data)

    def pending_queryset(self):
        """Новые записи (бейдж «Новые»): ожидают подтверждения, с сегодняшнего дня и дальше."""
        return self.get_queryset().filter(status='pending', date__gte=timezone.localdate())

    @action(detail=False, methods=['get'])
    def pending(self, request):
        queryset = self.pending_queryset().order_by('date', 'start_time', 'id')[:MAX_PENDING_ITEMS]
        return Response(self.get_serializer(queryset, many=True).data)

    @action(detail=False, methods=['get'], url_path='pending-count')
    def pending_count(self, request):
        # Тот же набор, что и в списке pending/ (список обрезается до MAX_PENDING_ITEMS)
        return Response({'count': self.pending_queryset().count()})


class FreeTimesView(APIView):
    """Свободное время мастера с шагом 15 минут (для модалки записи)."""
    permission_classes = [IsStaff]

    def get(self, request):
        staff = request.staff
        params = request.query_params

        master_id = parse_int(params.get('master'))
        if master_id is None:
            raise StaffError('Укажите мастера (master)')
        masters = Master.objects.filter(company=staff.company)  # скрытый тоже — для переноса записей
        if staff.role == 'master':
            masters = masters.filter(pk=staff.master.pk)
        master = get_object_or_404(masters, pk=master_id)

        date = parse_date(params.get('date'))
        if not date:
            raise StaffError('Укажите дату в формате YYYY-MM-DD')

        if params.get('duration'):
            duration = parse_int(params.get('duration'))
            if duration is None or not 5 <= duration <= 600:
                raise StaffError('Длительность — от 5 до 600 минут')
        elif params.get('service'):
            service_id = parse_int(params.get('service'))
            service = Service.objects.filter(company=staff.company, pk=service_id).first() if service_id else None
            if not service:
                raise StaffError('Услуга не найдена')
            duration = service.duration
        else:
            raise StaffError('Укажите услугу (service) или длительность (duration)')

        exclude_id = None
        if params.get('exclude'):
            exclude_id = parse_int(params.get('exclude'))
            if exclude_id is None:
                raise StaffError('exclude — id записи (целое число)')

        times = get_free_slots(
            master, date, duration, exclude_id=exclude_id, step=timedelta(minutes=15), include_past=False
        )
        schedule = MasterSchedule.objects.filter(master=master, day_of_week=date.weekday(), is_working=True).first()
        day_off = MasterDayOff.objects.filter(master=master, date=date).exists()
        return Response({
            'times': [slot.strftime('%H:%M') for slot in times],
            'working': schedule is not None and not day_off,
            'day_off': day_off,
            'schedule': {
                'start_time': schedule.start_time.strftime('%H:%M'),
                'end_time': schedule.end_time.strftime('%H:%M'),
            } if schedule else None,
        })


class ClientLookupView(APIView):
    """Клиент по телефону: имя и история визитов только в этом салоне."""
    permission_classes = [IsStaff]

    def get(self, request):
        # Перебор телефонов: не больше LOOKUP_LIMIT запросов в час на пользователя панели
        limit_key = f'lookup:{request.user.pk}'
        if (cache.get(limit_key) or 0) >= LOOKUP_LIMIT:
            raise StaffError(TOO_MANY_REQUESTS_MESSAGE, status.HTTP_429_TOO_MANY_REQUESTS)
        increment_counter(limit_key, LOOKUP_WINDOW_SECONDS)

        phone = normalize_phone((request.query_params.get('phone') or '').strip(), PHONE_REGION)
        if not phone:
            raise ValidationError({'phone': ['Неверный номер телефона']})

        company = request.staff.company
        profile = ClientProfile.objects.filter(phone=phone).select_related('user').first()
        appointments = Appointment.objects.filter(company=company, client_phone=phone)
        last = appointments.order_by('-date', '-start_time', '-id').first()
        # Данные отдаются, только если клиент уже записывался в этот салон: иначе по телефону
        # можно было бы узнать, кто зарегистрирован на платформе, и имена клиентов других салонов
        known = last is not None or (
            profile is not None and Appointment.objects.filter(company=company, client=profile.user).exists()
        )
        if not known:
            raise StaffError('Клиент не найден', status.HTTP_404_NOT_FOUND)

        profile_name = f'{profile.name} {profile.surname}'.strip() if profile else ''
        name = profile_name or (last.client_name if last else '')
        last_visit = appointments.filter(status='completed').aggregate(last=Max('date'))['last']
        return Response({
            'phone': phone,
            'name': name,
            'visits': appointments.filter(status='completed').count(),
            'no_shows': appointments.filter(status='no_show').count(),
            'last_visit': last_visit.isoformat() if last_visit else None,
        })


# --- Справочники ---

class CategoryViewSet(StaffViewMixin, viewsets.ModelViewSet):
    permission_classes = [IsStaffManagerOrReadOnly]
    serializer_class = StaffCategorySerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        return ServiceCategory.objects.filter(company=self.staff.company).order_by('name', 'id')

    def perform_create(self, serializer):
        serializer.save(company=self.staff.company)


class ServiceViewSet(StaffViewMixin, viewsets.ModelViewSet):
    permission_classes = [IsStaffManagerOrReadOnly]
    serializer_class = StaffServiceSerializer
    http_method_names = ['get', 'post', 'patch', 'delete', 'head', 'options']
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        # Все услуги салона, включая скрытые
        return Service.objects.filter(company=self.staff.company).select_related('category').order_by('id')

    def perform_create(self, serializer):
        serializer.save(company=self.staff.company)

    def perform_destroy(self, instance):
        # Мягкое удаление: услуга пропадает с сайта, старые записи сохраняют ссылку на неё
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class MasterViewSet(StaffViewMixin, viewsets.ModelViewSet):
    permission_classes = [IsStaffManagerOrReadOnly]
    serializer_class = StaffMasterSerializer
    http_method_names = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options']
    lookup_value_regex = r'\d+'

    def get_queryset(self):
        queryset = Master.objects.filter(company=self.staff.company)
        if self.staff.role == 'master':
            # Мастер видит только себя; чужие id → 404
            queryset = queryset.filter(pk=self.staff.master.pk)
        return queryset.select_related('user').prefetch_related('services', 'schedule').order_by('id')

    def update(self, request, *args, **kwargs):
        # PUT мастера не поддерживается (PUT есть только у графика)
        if not kwargs.get('partial'):
            return self.http_method_not_allowed(request, *args, **kwargs)
        return super().update(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(company=self.staff.company)

    def destroy(self, request, *args, **kwargs):
        master = self.get_object()
        count = Appointment.objects.filter(
            master=master, date__gte=timezone.localdate(), status__in=ACTIVE_STATUSES
        ).count()
        if count and request.query_params.get('force') != '1':
            raise StaffError(f'У мастера {count} будущих записей — перенесите или отмените их', count=count)
        # Мягкое удаление; пользователь скрытого мастера теряет доступ к панели (get_staff)
        master.is_active = False
        master.save(update_fields=['is_active'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get', 'put'])
    def schedule(self, request, pk=None):
        master = self.get_object()
        if request.method == 'PUT':
            self.save_schedule(master, request.data)
            master = self.get_object()  # перечитать график
        return Response(schedule_days(master))

    @staticmethod
    def save_schedule(master, days):
        if not isinstance(days, list) or len(days) != 7:
            raise StaffError('Нужен график ровно на 7 дней недели')
        rows = []
        for item in days:
            serializer = ScheduleDaySerializer(data=item)
            if not serializer.is_valid():
                day = item.get('day_of_week') if isinstance(item, dict) else None
                errors = serializer.errors
                message = '; '.join(
                    str(msg) for key, msgs in errors.items() for msg in (msgs if isinstance(msgs, list) else [msgs])
                )
                if isinstance(day, int) and 0 <= day <= 6:
                    message = f'{DAY_NAMES[day]}: {message}'
                raise StaffError(message)
            rows.append(serializer.validated_data)
        if sorted(row['day_of_week'] for row in rows) != list(range(7)):
            raise StaffError('Каждый день недели (0–6) должен встречаться ровно один раз')

        with transaction.atomic():
            for row in rows:
                MasterSchedule.objects.update_or_create(
                    master=master, day_of_week=row['day_of_week'],
                    defaults={
                        'is_working': row['is_working'],
                        # Пропущенное время нерабочего дня уже заменено на 09:00–18:00 (ScheduleDaySerializer)
                        'start_time': row['start_time'],
                        'end_time': row['end_time'],
                    },
                )

    @action(detail=True, methods=['post'], url_path='days-off')
    def add_day_off(self, request, pk=None):
        master = self.get_object()
        serializer = DayOffInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        date = serializer.validated_data['date']
        day_off, created = MasterDayOff.objects.get_or_create(master=master, date=date)
        appointments_count = Appointment.objects.filter(
            master=master, date=date, status__in=ACTIVE_STATUSES
        ).count()
        return Response(
            {**day_off_data(day_off), 'appointments_count': appointments_count},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=['delete'], url_path=r'days-off/(?P<day>\d{4}-\d{2}-\d{2})')
    def remove_day_off(self, request, pk=None, day=None):
        master = self.get_object()
        date = parse_date(day)
        if not date:
            raise StaffError('Дата в формате YYYY-MM-DD')
        # Идемпотентно: снять несуществующий выходной — тоже 204
        MasterDayOff.objects.filter(master=master, date=date).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def check_can_manage_account(self, target):
        """Защита от захвата чужого аккаунта через смену пароля мастера."""
        if target.pk == self.request.user.pk:
            return
        if target.is_superuser or target.is_staff:
            raise StaffError('Этим аккаунтом можно управлять только через админку Django', status.HTTP_403_FORBIDDEN)
        profile = AdminProfile.objects.filter(user=target, company__isnull=False).first()
        if profile and (self.staff.role != 'owner' or profile.company_id != self.staff.company.id):
            raise StaffError(NOT_ENOUGH_RIGHTS_MESSAGE, status.HTTP_403_FORBIDDEN)

    @staticmethod
    def check_not_phone_username(username):
        """Логин-телефон занял бы номер клиента: регистрация на сайте и вход клиента по нему сломаются."""
        if username.startswith('+') or normalize_phone(username, PHONE_REGION) is not None:
            raise ValidationError({'username': ['Логин не должен быть номером телефона']})

    @action(detail=True, methods=['post', 'delete'])
    def access(self, request, pk=None):
        master = self.get_object()

        if request.method == 'DELETE':
            if master.user_id:
                self.check_can_manage_account(master.user)
                # Сам пользователь не удаляется — только отвязывается от мастера
                master.user = None
                master.save(update_fields=['user'])
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = MasterAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data['username']
        password = serializer.validated_data['password']
        user = master.user
        taken = User.objects.filter(username__iexact=username)

        if user is None:
            # Никогда не привязываем существующие аккаунты (в т.ч. клиентские) — только новый
            self.check_not_phone_username(username)
            if taken.exists():
                raise ValidationError({'username': ['Этот логин уже занят']})
            password_errors('password', password, User(username=username))
            with transaction.atomic():
                user = User.objects.create_user(username=username, password=password)
                master.user = user
                master.save(update_fields=['user'])
        else:
            self.check_can_manage_account(user)
            if ClientProfile.objects.filter(user=user).exists():
                # Клиентский аккаунт действует во всех салонах: сменой пароля или логина его можно
                # было бы захватить (и записи клиента в других салонах). Отвязать (DELETE) — можно.
                raise StaffError(CLIENT_ACCOUNT_MESSAGE, status.HTTP_403_FORBIDDEN)
            if username != user.username:
                self.check_not_phone_username(username)
                if taken.exclude(pk=user.pk).exists():
                    raise ValidationError({'username': ['Этот логин уже занят']})
                user.username = username
            password_errors('password', password, user)
            user.set_password(password)
            user.save()

        return Response({'username': user.username, 'has_access': True})


class DaysOffListView(APIView):
    permission_classes = [IsStaff]

    def get(self, request):
        staff = request.staff
        date_from, date_to = date_range_params(request.query_params)
        days_off = MasterDayOff.objects.filter(
            master__company=staff.company, date__gte=date_from, date__lte=date_to
        )
        if staff.role == 'master':
            days_off = days_off.filter(master=staff.master)
        elif request.query_params.get('master'):
            master_id = parse_int(request.query_params.get('master'))
            if master_id is None:
                raise StaffError('master — id мастера (целое число)')
            days_off = days_off.filter(master_id=master_id)
        return Response([day_off_data(day_off) for day_off in days_off.order_by('date', 'master_id')])


class CompanyView(APIView):
    permission_classes = [IsStaffManagerOrReadOnly]

    def get(self, request):
        return Response(StaffCompanySerializer(request.staff.company).data)

    def patch(self, request):
        serializer = StaffCompanySerializer(request.staff.company, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        company = serializer.save()
        company.refresh_from_db()
        return Response(StaffCompanySerializer(company).data)
