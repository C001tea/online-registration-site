from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
import secrets
import uuid


def new_manage_token():
    return secrets.token_urlsafe(18)


class PublicCompanyManager(models.Manager):
    """Салоны, которые видят клиенты: подтверждённые (status=active)."""

    def get_queryset(self):
        return super().get_queryset().filter(status=Company.STATUS_ACTIVE)


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
    # Клиент может отменить запись сам (сайт, бот) не позже чем за столько часов; 0 — до начала
    cancel_hours = models.PositiveSmallIntegerField(default=0)
    # Чёрный список: штраф в % от цены услуги за неявку (0 — штрафы выключены) и за отмену
    # клиентом позже чем за late_cancel_hours ч до визита (0 — за позднюю отмену не штрафуем)
    penalty_percent = models.PositiveSmallIntegerField(default=50)
    late_cancel_hours = models.PositiveSmallIntegerField(default=3)
    # Онлайн-записи сразу «Подтверждена» (для клиентов без Telegram); иначе — «Ожидает»,
    # подтверждает клиент в боте или администратор в панели
    auto_confirm = models.BooleanField(default=False)

    # Самостоятельная регистрация: салон виден клиентам после подтверждения email владельца
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_BLOCKED = 'blocked'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Ждёт подтверждения email'),
        (STATUS_ACTIVE, 'Работает'),
        (STATUS_BLOCKED, 'Заблокирован'),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    # Конец бесплатного пробного периода (пусто — салон подключён вручную, без пробного периода)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Владелец принял условия сервиса при регистрации
    terms_accepted_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager()
    public = PublicCompanyManager()

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
        ('no_show', 'Не пришёл'),
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
    # Язык сайта при записи (ru/ro): на нём бот пишет клиенту
    lang = models.CharField(max_length=2, default='ru')
    reminder_sent = models.BooleanField(default=False)
    # Когда клиент дал согласие на обработку персональных данных (онлайн-запись)
    consent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    client = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='appointments')
    # Запись создана персоналом в панели салона (а не клиентом онлайн)
    created_by_staff = models.BooleanField(default=False)
    # За сколько минут до визита напомнить (выбор клиента): пусто — как обычно (Telegram за сутки и за час,
    # письмо за сутки), 0 — не напоминать
    reminder_minutes = models.PositiveIntegerField(null=True, blank=True)
    # Письма клиенту отправлены: подтверждение записи и напоминание
    email_confirm_sent = models.BooleanField(default=False)
    email_reminder_sent = models.BooleanField(default=False)
    # Цена услуги на момент записи (у мастера может быть своя цена); пусто — цена услуги
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    # Секрет ссылки «Управлять записью»: клиент переносит или отменяет запись без входа
    manage_token = models.CharField(max_length=32, unique=True, null=True, blank=True, editable=False)

    def save(self, *args, **kwargs):
        if not self.manage_token:
            self.manage_token = new_manage_token()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.client_name} - {self.date} {self.start_time}"


class MasterServiceTerms(models.Model):
    """Своя цена и/или длительность услуги у мастера (пусто — как у услуги)."""
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='service_terms')
    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name='master_terms')
    price = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    duration = models.PositiveIntegerField(null=True, blank=True, help_text='duration in minutes')

    class Meta:
        unique_together = ['master', 'service']

    def __str__(self):
        return f'{self.master.name} — {self.service.name}'


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
    # Обед: в это время клиентам не предлагаются слоты (оба поля или ни одного)
    break_start = models.TimeField(null=True, blank=True)
    break_end = models.TimeField(null=True, blank=True)

    class Meta:
        unique_together = ['master', 'day_of_week']

    def __str__(self):
        return f"{self.master.name} - {self.get_day_of_week_display()}"


class MasterWorkDay(models.Model):
    """Рабочий день мастера в конкретную дату: свои часы и обед вместо недельного графика.

    Порядок: выходной по дате (MasterDayOff) → рабочий день по дате → недельный график.
    """
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='work_days')
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    break_start = models.TimeField(null=True, blank=True)
    break_end = models.TimeField(null=True, blank=True)

    class Meta:
        unique_together = ['master', 'date']
        ordering = ['date']

    def __str__(self):
        return f"{self.master.name} - {self.date} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class MasterDayOff(models.Model):
    """Выходной мастера в конкретную дату (отпуск, больничный) поверх недельного графика."""
    master = models.ForeignKey(Master, on_delete=models.CASCADE, related_name='days_off')
    date = models.DateField()

    class Meta:
        unique_together = ['master', 'date']
        ordering = ['date']

    def __str__(self):
        return f"{self.master.name} - {self.date}"


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
    ROLE_CHOICES = [
        ('owner', 'Владелец'),
        ('admin', 'Администратор'),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="admin_profile")
    # У салона может быть несколько администраторов
    company = models.ForeignKey(Company, on_delete=models.SET_NULL, null=True, blank=True, related_name="admins")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='admin')
    # Устарело: доступ по токену в URL больше не используется (вход в панель — по логину и паролю)
    access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=True)

    def __str__(self):
        return f"{self.user.username} - {self.company} ({self.get_role_display()})"
