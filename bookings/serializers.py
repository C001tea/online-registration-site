from rest_framework import serializers
from .models import Company, Service, ServiceCategory, Master, Appointment, Review
from django.db.models import Avg
from .phone import normalize_phone
from .slots import get_free_slots

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
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = Service
        fields = ['id', 'name', 'category', 'category_name', 'duration', 'price']


class CompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = Company
        fields = ['id', 'name', 'slug', 'address', 'phone', 'logo', 'main_photo', 'latitude', 'longitude', 'description']

class AppointmentCreateSerializer(serializers.ModelSerializer):
    # master не обязателен: без него назначается первый свободный мастер услуги
    master = serializers.PrimaryKeyRelatedField(queryset=Master.objects.all(), required=False, allow_null=True)
    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.all())

    class Meta:
        model = Appointment
        fields = ['master', 'service', 'client_name', 'client_phone', 'client_email', 'date', 'start_time', 'notes']

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
            if start_time not in get_free_slots(master, date, service.duration):
                raise serializers.ValidationError('Это время недоступно. Выберите другое.')
        else:
            candidates = company.masters.filter(services=service, is_active=True)
            master = next(
                (m for m in candidates if start_time in get_free_slots(m, date, service.duration)),
                None
            )
            if not master:
                raise serializers.ValidationError('На это время нет свободных мастеров.')
            data['master'] = master

        return data


class ReviewSerializer(serializers.ModelSerializer):
    client_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = ['id', 'client_name', 'rating', 'comment', 'created_at']
        read_only_fields = ['id', 'client_name', 'created_at']

    def get_client_name(self, obj):
        return f"{obj.client.profile.name} {obj.client.profile.surname}"

