from django.db import models
from django.contrib.auth.models import User
import uuid

class Company(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    address = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    logo = models.ImageField(upload_to='logos/', blank=True, null=True)
    main_photo = models.ImageField(upload_to='main_photos', blank=True, null=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

class Master(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='worker_profile')
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='masters')
    name = models.CharField(max_length=200)
    photo = models.ImageField(upload_to='masters/', blank=True, null=True)
    description = models.TextField(blank=True)
    services = models.ManyToManyField('Service', blank=True, related_name='masters')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

class ServiceCategory(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='categories')
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name


class Service(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='services')
    category = models.ForeignKey(ServiceCategory, on_delete=models.SET_NULL, null=True, related_name='categories')
    name = models.CharField(max_length=200)
    duration = models.PositiveIntegerField(help_text="duration in minutes")
    price = models.DecimalField(max_digits=8, decimal_places=2)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

class Appointment(models.Model):

    STATUS_CHOICES = [
        ('pending', 'Ожидает'),
        ('confirmed', 'Подтверждена'),
        ('cancelled', 'Отменена'),
        ('completed', 'Завершена'),
    ]

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name='appointments')
    master = models.ForeignKey(Master, on_delete=models.SET_NULL, null=True, blank=True, related_name='appointments')
    service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True, blank=True, related_name='appointments')
    client_name = models.CharField(max_length=200)
    client_phone = models.CharField(max_length=20)
    client_email = models.EmailField(blank=True)
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    notes = models.TextField(blank=True)
    reminder_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    client = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='appointments')

    def __str__(self):
        return f"{self.client_name} - {self.date} {self.start_time}"


class MasterSchedule(models.Model):
    DAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday")
    ]

    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name="schedule")
    day_of_week = models.IntegerField(choices=DAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_working = models.BooleanField(default=True)

    class Meta:
        unique_together = ['master', 'day_of_week']

    def __str__(self):
        return f"{self.master.name} - {self.get_day_of_week_display()}"


class ClientProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    phone = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    surname = models.CharField(max_length=200, blank=True)
    sms_code = models.CharField(max_length=6, blank=True)
    sms_code_expires = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.phone


class Review(models.Model):
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='reviews')
    client = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveIntegerField()
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client.username} - {self.master.name} - {self.rating}"


class AdminProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="admin_profile")
    company = models.OneToOneField(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="admins")
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=True)