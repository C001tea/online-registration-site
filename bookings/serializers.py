from rest_framework import serializers
from .models import Company, Service, ServiceCategory, Master, Appointment, Review
from datetime import datetime, timedelta
from django.db.models import Avg

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
        fields = ['id', 'name', 'slug', 'address', 'phone', 'logo', 'latitude', 'longitude', 'description']

class AppointmentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Appointment
        fields = ['master', 'service', 'client_name', 'client_phone', 'client_email', 'date', 'start_time', 'notes']

    def validate(self, data):
        master = data['master']
        date = data['date']
        start_time = data['start_time']
        service = data['service']

        start_dt = datetime.combine(date, start_time)
        end_dt = start_dt + timedelta(minutes=service.duration)

        conflict = Appointment.objects.filter(
            master=master,
            date=date,
            status__in=['pending', 'confirmed'],
            start_time__lt=end_dt.time(),
            end_time__gt=start_dt.time()
        ).exists()

        if conflict:
            raise serializers.ValidationError(
                'Окошко уже занято.'
            )

        return data


class ReviewSerializer(serializers.ModelSerializer):
    client_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = ['id', 'client_name', 'rating', 'comment', 'created_at']
        read_only_fields = ['id', 'client_name', 'created_at']

    def get_client_name(self, obj):
        return f"{obj.client.profile.name} {obj.client.profile.surname}"

