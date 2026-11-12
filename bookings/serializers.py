from django.utils import timezone
from rest_framework import serializers
from .models import Company, Service, ServiceCategory, Master, Appointment, Review
from django.db.models import Avg
from .phone import normalize_phone
from .slots import get_free_slots
from .terms import duration_of, terms

class MasterSerializer(serializers.ModelSerializer):
    reviews_count = serializers.SerializerMethodField()
    avg_rating = serializers.SerializerMethodField()
    company = serializers.ReadOnlyField(source='company.name')

    class Meta:
        model = Master
        fields = ['id', 'name', 'photo', 'description', 'is_active', 'avg_rating', 'reviews_count', 'company']

    def get_reviews_count(self, obj):
        return obj.reviews.count()

    def get_avg_rating(self, obj):
        result = obj.reviews.aggregate(Avg('rating'))['rating__avg']
        return round(result, 1) if result else None

class ServiceSerializer(serializers.ModelSerializer):
    """Услуга для клиента. context['ranges'] — разброс цены и длительности по мастерам («от – до»);
    context['master'] — цена и длительность именно у этого мастера."""
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = Service
        fields = ['id', 'name', 'category', 'category_name', 'duration', 'price']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        master = self.context.get('master')
        if master is not None:
            duration, price = terms(master, instance)
            data['duration'], data['price'] = duration, str(price)
            data.update(price_min=str(price), price_max=str(price), duration_min=duration, duration_max=duration)
        else:
            data.update(self.context.get('ranges', {}).get(instance.id, {
                'price_min': data['price'], 'price_max': data['price'],
                'duration_min': data['duration'], 'duration_max': data['duration']}))
        return data


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ['id', 'name', 'slug', 'address', 'phone', 'logo', 'main_photo', 'latitude', 'longitude', 'description',
                  'cancel_hours', 'penalty_percent', 'late_cancel_hours', 'auto_confirm', 'booking_accent']

# За сколько минут до визита клиент может попросить напоминание (0 — не напоминать)
REMINDER_CHOICES = [0, 60, 120, 180, 360, 720, 1440, 2880]


class AppointmentCreateSerializer(serializers.ModelSerializer):
    # master не обязателен: без него назначается первый свободный мастер услуги
    master = serializers.PrimaryKeyRelatedField(queryset=Master.objects.all(), required=False, allow_null=True)
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.all())
    # Согласие на обработку персональных данных (галочка на сайте); False — отказ
    consent = serializers.BooleanField(write_only=True, required=False)
    # Напоминание: 0 — не нужно; пусто — как обычно
    reminder_minutes = serializers.ChoiceField(choices=REMINDER_CHOICES, required=False, allow_null=True)

    class Meta:
        model = Appointment
        fields = ['master', 'service', 'client_name', 'client_phone', 'client_email', 'date', 'start_time', 'notes',
                  'consent', 'reminder_minutes']

    def validate_consent(self, value):
        if not value:
            raise serializers.ValidationError('Нужно согласие на обработку персональных данных')
        return value

    def create(self, validated_data):
        if validated_data.pop('consent', False):
            validated_data['consent_at'] = timezone.now()
        return super().create(validated_data)

    def validate_client_phone(self, value):
        phone = normalize_phone(value)
        if not phone:
            raise serializers.ValidationError('Неверный формат номера телефона')
        return phone

    def validate(self, data):
        company = self.context['company']
        service = data['service']
        master = data.get('master')
        date = data['date']
        start_time = data['start_time']

        if service.company_id != company.id or not service.is_active:
            raise serializers.ValidationError({'service': 'Услуга не найдена.'})

        if master:
            if master.company_id != company.id or not master.is_active:
                raise serializers.ValidationError({'master': 'Мастер не найден.'})
            if not master.services.filter(id=service.id).exists():
                raise serializers.ValidationError('Мастер не оказывает эту услугу.')
            if start_time not in get_free_slots(master, date, duration_of(master, service)):
                raise serializers.ValidationError('Это время недоступно. Выберите другое.')
        else:
            candidates = company.masters.filter(services=service, is_active=True)
            master = next(
                (m for m in candidates if start_time in get_free_slots(m, date, duration_of(m, service))),
                None
            )
            if not master:
                raise serializers.ValidationError('На это время нет свободных мастеров.')
            data['master'] = master

        return data


class VisitCreateSerializer(serializers.Serializer):
    """Визит из нескольких услуг подряд: items — [{service, master?}] по порядку."""
    items = serializers.ListField(child=serializers.DictField(), min_length=1)
    date = serializers.DateField()
    start_time = serializers.TimeField()
    client_name = serializers.CharField(max_length=100)
    client_phone = serializers.CharField(max_length=20)
    client_email = serializers.EmailField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    consent = serializers.BooleanField(required=False)
    reminder_minutes = serializers.ChoiceField(choices=REMINDER_CHOICES, required=False, allow_null=True)

    def validate_client_phone(self, value):
        phone = normalize_phone(value)
        if not phone:
            raise serializers.ValidationError('Неверный формат номера телефона')
        return phone

    def validate(self, data):
        if not data.get('consent'):
            raise serializers.ValidationError({'consent': ['Нужно согласие на обработку персональных данных']})
        return data


class ReviewSerializer(serializers.ModelSerializer):
    client_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = ['id', 'client_name', 'rating', 'comment', 'created_at']
        read_only_fields = ['id', 'client_name', 'created_at']

    def validate_rating(self, value):
        if not 1 <= value <= 5:
            raise serializers.ValidationError('Оценка должна быть от 1 до 5')
        return value

    def get_client_name(self, obj):
        # У пользователей, созданных не через регистрацию (например, админа), профиля нет
        profile = getattr(obj.client, 'profile', None)
        if not profile:
            return obj.client.username
        return f"{profile.name} {profile.surname}".strip()

