from rest_framework import generics, status
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db import transaction, connection, DatabaseError
from django.shortcuts import get_object_or_404
from .i18n import client_lang
from .security import TOO_MANY_MESSAGE, client_ip, hit
from .models import Company, Master, Service, Review, Appointment
from .serializers import MasterSerializer, ServiceSerializer, AppointmentCreateSerializer, CompanySerializer, ReviewSerializer
from .slots import get_free_slots, month_overview, nearest_slots, parse_month, ACTIVE_STATUSES
from .terms import duration_of, service_ranges, terms
from datetime import datetime, timedelta
from django.db.models import Avg, Q
from django.utils import timezone
from notifications import services as notifications

# Защита онлайн-записи от спама (за час): с одного IP, на один номер, на весь салон;
# и не больше MAX_UPCOMING_PER_PHONE предстоящих онлайн-записей на номер
BOOKINGS_PER_IP_HOUR = 30
BOOKINGS_PER_PHONE_HOUR = 10
BOOKINGS_PER_SALON_HOUR = 300
MAX_UPCOMING_PER_PHONE = 3
SPAM_MESSAGE = 'Не удалось отправить запись. Обновите страницу и попробуйте ещё раз'
TOO_MANY_UPCOMING_MESSAGE = 'У вас уже есть 3 предстоящие записи в этом салоне. Чтобы записаться ещё, позвоните в салон'


class HealthView(APIView):
    """Проверка для мониторинга: приложение отвечает и база доступна."""
    authentication_classes = []

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
        except DatabaseError:
            return Response({'status': 'error', 'database': 'unavailable'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        if request.query_params.get('ip') == '1':
            # Диагностика лимитов: каким IP сервер видит этого посетителя (его собственный адрес)
            return Response({'status': 'ok', 'ip': client_ip(request)})
        return Response({'status': 'ok'})


def parse_date(value):
    try:
        return datetime.strptime(value or '', '%Y-%m-%d').date()
    except ValueError:
        return None


class MasterListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)
        # Клиенту показываем только мастеров, к которым можно записаться:
        # без активных услуг выбор мастера заканчивается пустым экраном
        masters = company.masters.filter(is_active=True, services__is_active=True).distinct()
        service_id = request.query_params.get('service')
        if service_id:
            service = get_object_or_404(Service, company=company, id=service_id, is_active=True)
            masters = masters.filter(services=service)

        serializer = MasterSerializer(masters, many=True)
        return Response(serializer.data)

class ServiceListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)
        services = company.services.filter(is_active=True)
        master_id = request.query_params.get('master_id')
        date = request.query_params.get("date")
        time = request.query_params.get("time")

        master = None
        if master_id:
            master = get_object_or_404(Master, company=company, id=master_id, is_active=True)
            services = services.filter(masters=master)

        elif date and time:
            date = parse_date(date)
            try:
                targeted_time = datetime.strptime(time, '%H:%M').time()
            except ValueError:
                targeted_time = None
            if not date or not targeted_time:
                return Response({"error": "Format: date=YYYY-MM-DD, time=HH:MM"}, status=status.HTTP_400_BAD_REQUEST)

            fitting_ids = []
            for service in services:
                for master in company.masters.filter(services=service, is_active=True):
                    if targeted_time in get_free_slots(master, date, duration_of(master, service)):
                        fitting_ids.append(service.id)
                        break
            services = services.filter(id__in=fitting_ids)

        services = list(services)
        context = {'master': master} if master else {'ranges': service_ranges(services)}
        return Response(ServiceSerializer(services, many=True, context=context).data)


class AppointmentCreateView(generics.CreateAPIView):
    serializer_class = AppointmentCreateSerializer

    def get_company(self):
        if not hasattr(self, '_company'):
            self._company = get_object_or_404(Company.public, slug=self.kwargs['slug'])
        return self._company

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['company'] = self.get_company()
        return context

    def perform_create(self, serializer):
        data = serializer.validated_data
        service = data['service']
        phone = data['client_phone']
        # Один номер не может занять много времени салона: N предстоящих онлайн-записей и попыток в час
        if hit(f'book:phone:{phone}', BOOKINGS_PER_PHONE_HOUR, 3600):
            raise Throttled(detail={'error': TOO_MANY_MESSAGE})
        upcoming = Appointment.objects.filter(
            company=self.get_company(), client_phone=phone, status__in=ACTIVE_STATUSES,
            date__gte=timezone.localdate(), created_by_staff=False,
        ).count()
        if upcoming >= MAX_UPCOMING_PER_PHONE:
            raise ValidationError({'non_field_errors': [TOO_MANY_UPCOMING_MESSAGE]})

        # Блокируем мастера, чтобы два одновременных запроса не заняли одно время
        with transaction.atomic():
            master = Master.objects.select_for_update().get(pk=data['master'].pk)
            duration, price = terms(master, service)
            if data['start_time'] not in get_free_slots(master, data['date'], duration):
                raise ValidationError({'non_field_errors': ['Это время уже заняли. Выберите другое.']})

            end_dt = datetime.combine(data['date'], data['start_time']) + timedelta(minutes=duration)
            client = self.request.user if self.request.user.is_authenticated else None

            appointment = serializer.save(
                company=self.get_company(),
                master=master,
                status='confirmed' if self.get_company().auto_confirm else 'pending',
                end_time=end_dt.time(),
                price=price,
                client=client,
                lang=client_lang(self.request),
            )
            notifications.appointment_created(appointment, self.request.user)
        self.appointment = appointment

    def create(self, request, *args, **kwargs):
        # Защита от спама: поле-ловушка (человек его не видит и не заполняет) и лимиты
        if request.data.get('website'):
            return Response({'error': SPAM_MESSAGE}, status=status.HTTP_400_BAD_REQUEST)
        company = self.get_company()
        if (hit(f'book:ip:{client_ip(request)}', BOOKINGS_PER_IP_HOUR, 3600)
                or hit(f'book:company:{company.id}', BOOKINGS_PER_SALON_HOUR, 3600)):
            return Response({'error': TOO_MANY_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        response = super().create(request, *args, **kwargs)
        # Кнопка «Подтвердить в Telegram» на экране после записи
        response.data['telegram_url'] = notifications.client_url(self.appointment)
        # Экран после записи: «салон подтвердит» или «запись подтверждена»
        response.data['status'] = self.appointment.status
        # Ссылка «Управлять записью» (перенос, отмена, календарь) — без входа
        response.data['manage_token'] = self.appointment.manage_token
        return response



class SelectData(APIView):
    """Свободное время салона без привязки к услуге (окна по 30 минут)."""

    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)
        date = parse_date(request.query_params.get('date'))
        if not date:
            return Response({"error": "Date format: YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

        all_free_points = set()
        for master in company.masters.filter(is_active=True):
            for slot in get_free_slots(master, date, 30):
                all_free_points.add(slot.strftime('%H:%M'))

        return Response(sorted(all_free_points))

class SlotListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)

        master_id = request.query_params.get('master')
        service_id = request.query_params.get('service')
        date_str = request.query_params.get('date')

        if not all([master_id, service_id, date_str]):
            return Response({'error': 'Specify master, service and date'}, status=status.HTTP_400_BAD_REQUEST)

        if not str(master_id).isdecimal() or not str(service_id).isdecimal():
            return Response({'error': 'Specify master, service and date'}, status=status.HTTP_400_BAD_REQUEST)
        master = get_object_or_404(Master, id=master_id, company=company)
        service = get_object_or_404(Service, id=service_id, company=company)

        date = parse_date(date_str)
        if not date:
            return Response({"error": "Date format: YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)

        # Скрытый мастер/услуга или мастер не оказывает услугу — записаться нельзя
        bookable = master.is_active and service.is_active and master.services.filter(id=service.id).exists()
        free_slots = [slot.strftime('%H:%M') for slot in get_free_slots(master, date, duration_of(master, service))] if bookable else []

        return Response({
            'date': date_str,
            'master': master_id,
            'service': service_id,
            'slots': free_slots
        })

class CompanyInfoView(generics.RetrieveAPIView):
    serializer_class = CompanySerializer
    lookup_field = 'slug'
    queryset = Company.public.all()

class ReviewView(APIView):

    def get(self, request, slug, master_id):
        company = get_object_or_404(Company.public, slug=slug)
        master = get_object_or_404(Master, id=master_id, company=company)

        reviews = Review.objects.filter(master=master).order_by('-created_at')
        serializer = ReviewSerializer(reviews, many=True)

        avg_rating = reviews.aggregate(Avg('rating'))['rating__avg']

        return Response(
            {
                'master': master.name,
                'avg_rating': round(avg_rating, 1) if avg_rating else None,
                'reviews_count': reviews.count(),
                'reviews': serializer.data
            }
        )

    def post(self, request, slug, master_id):

        if not request.user.is_authenticated:
            return Response(
                {'error': 'Необходима авторизация'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        company = get_object_or_404(Company.public, slug=slug)
        master = get_object_or_404(Master, id=master_id, company=company)

        if master.user_id == request.user.id:
            return Response(
                {'error': 'Нельзя оставить отзыв самому себе'},
                status=status.HTTP_403_FORBIDDEN
            )

        if Review.objects.filter(master=master, client=request.user).exists():
            return Response(
                {'error': 'Вы уже оставили отзыв на этого мастера'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Отзыв может оставить только клиент, который уже был у мастера.
        # Запись, созданную персоналом, считаем визитом только после отметки «пришёл»
        visited = Appointment.objects.filter(master=master, client=request.user).filter(
            Q(status='completed')
            | Q(date__lt=timezone.localdate(), status__in=ACTIVE_STATUSES, created_by_staff=False)
        ).exists()
        if not visited:
            return Response(
                {'error': 'Отзыв можно оставить после визита к мастеру'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = ReviewSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(master=master, client=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class MasterServices(APIView):
    def get(self, request, slug, master_id):
        company = get_object_or_404(Company.public, slug=slug)
        master = get_object_or_404(Master, id=master_id, company=company, is_active=True)
        services = master.services.filter(is_active=True)
        return Response(ServiceSerializer(services, many=True, context={'master': master}).data)

# Календарь с кружками: не дальше стольких месяцев вперёд
MAX_CALENDAR_MONTHS_AHEAD = 6


def bookable_masters(company, params):
    """Мастера для календаря/ближайших окон с учётом ?service= и ?master= (None — не найдено)."""
    masters = company.masters.filter(is_active=True, services__is_active=True).distinct()
    service = None
    if params.get('service'):
        if not str(params['service']).isdecimal():
            return None, None
        service = Service.objects.filter(company=company, id=params['service'], is_active=True).first()
        if not service:
            return None, None
        masters = masters.filter(services=service)
    if params.get('master'):
        if not str(params['master']).isdecimal():
            return None, None
        masters = masters.filter(id=params['master'])
    return masters, service


class CalendarView(APIView):
    """Загруженность дней месяца для календаря записи: рабочий ли день, доля занятого времени,
    сколько свободных времён начала (для услуги — с её длительностью)."""

    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)
        period = parse_month(request.query_params.get('month'))
        if not period:
            return Response({'error': 'Month format: YYYY-MM'}, status=status.HTTP_400_BAD_REQUEST)
        today = timezone.localdate()
        months_ahead = (period[0].year - today.year) * 12 + period[0].month - today.month
        if months_ahead < 0 or months_ahead > MAX_CALENDAR_MONTHS_AHEAD:
            return Response({'month': request.query_params['month'], 'days': []})
        masters, service = bookable_masters(company, request.query_params)
        if masters is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        days = month_overview(masters, period[0], period[1], (lambda m: duration_of(m, service)) if service else None)
        return Response({'month': request.query_params['month'], 'days': days})


class NearestSlotsView(APIView):
    """Ближайшие свободные окна каждого мастера (до 14 дней вперёд, первые 4 времени)."""

    def get(self, request, slug):
        company = get_object_or_404(Company.public, slug=slug)
        masters, service = bookable_masters(company, request.query_params)
        if masters is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        result = []
        for master in masters:
            nearest = nearest_slots(master, duration_of(master, service) if service else 30)
            result.append({
                'master': master.id,
                'date': nearest[0].isoformat() if nearest else None,
                'slots': [slot.strftime('%H:%M') for slot in nearest[1]] if nearest else [],
            })
        return Response(result)
