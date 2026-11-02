"""Самостоятельная регистрация салона, подтверждение email, сброс пароля, чек-лист настройки.

Регистрация: владелец сам создаёт салон (адрес, email, пароль) и сразу попадает в панель;
сайт записи виден клиентам после подтверждения email (status=active). Пробный период —
settings.TRIAL_DAYS. Включается SIGNUP_ENABLED и только при настроенной почте.

Защита: лимиты по IP и email, поле-ловушка для ботов, подтверждение email; ответ на «забыли
пароль» одинаковый для любого email (по нему нельзя узнать, кто зарегистрирован); после
сброса пароля прежние входы перестают действовать (токены привязаны к паролю, см. tokens.py).
"""
import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import AdminProfile, Company, MasterSchedule, MasterWorkDay, Master, Service
from bookings.phone import normalize_phone
from bookings.security import TOO_MANY_MESSAGE, client_ip, hit
from notifications.models import StaffTelegram
from .errors import StaffError
from .permissions import IsStaff, IsStaffManager, get_staff
from .salons import create_salon, login_taken, slug_error, slugify_name, suggest_slug
from .serializers import PHONE_REGION, staff_me
from .tokens import get_staff_tokens

logger = logging.getLogger(__name__)

VERIFY_SALT = 'staff-verify-email'
VERIFY_MAX_AGE = 7 * 24 * 3600
SIGNUPS_PER_IP_HOUR = 5
SLUG_CHECKS_PER_IP_HOUR = 120
RESETS_PER_IP_HOUR = 10
RESETS_PER_EMAIL_HOUR = 3
RESENDS_PER_USER_HOUR = 5
CONFIRMS_PER_IP_HOUR = 30
SIGNUP_CLOSED_MESSAGE = 'Регистрация новых салонов пока закрыта'
RESET_SENT_MESSAGE = 'Если такой email зарегистрирован, мы отправили на него ссылку для сброса пароля'
BAD_LINK_MESSAGE = 'Ссылка устарела или уже использована. Запросите новую'


def public_url(company):
    return settings.SALON_URL_TEMPLATE.format(slug=company.slug)


def throttled(key, limit):
    if hit(key, limit, 3600):
        raise StaffError(TOO_MANY_MESSAGE, status.HTTP_429_TOO_MANY_REQUESTS)


def send(subject, body, to):
    """Письмо; сбой почты не ломает регистрацию — письмо можно запросить ещё раз."""
    try:
        send_mail(subject, body, None, [to])
        return True
    except Exception:  # noqa: BLE001 — любой сбой SMTP; ошибка уходит в лог и Sentry
        logger.exception('Не удалось отправить письмо «%s»', subject)
        return False


def send_verification(user, company):
    token = signing.dumps({'u': user.pk, 'e': user.email.lower()}, salt=VERIFY_SALT)
    link = f'{settings.PANEL_URL}/admin-panel/verify-email?token={token}'
    return send(
        f'Подтвердите email — {company.name}',
        f'Здравствуйте!\n\n'
        f'Вы зарегистрировали салон «{company.name}» в TimePoint. Подтвердите email — после этого '
        f'клиенты увидят ваш сайт онлайн-записи:\n\n{link}\n\n'
        f'Ссылка действует 7 дней. Если вы не регистрировались — просто не отвечайте на это письмо.\n\n'
        f'TimePoint — онлайн-запись для салонов',
        user.email,
    )


# --- Регистрация ---

class SignupConfigView(APIView):
    """GET — открыта ли регистрация и работает ли «Забыли пароль?» (для страниц входа и регистрации)."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'signup': settings.SIGNUP_ENABLED, 'password_reset': settings.EMAIL_CONFIGURED,
                         'trial_days': settings.TRIAL_DAYS})


class SlugCheckView(APIView):
    """GET ?slug=… или ?name=… — свободен ли адрес салона; по названию — подсказка адреса."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        throttled(f'signup:slug:{client_ip(request)}', SLUG_CHECKS_PER_IP_HOUR)
        slug = (request.query_params.get('slug') or '').strip().lower()
        if not slug:
            slug = slugify_name(request.query_params.get('name', ''))
        problem = slug_error(slug)
        taken = problem is not None and 'занят' in problem
        return Response({'slug': slug, 'available': problem is None, 'error': problem,
                         'suggestion': suggest_slug(slug) if taken else None})


class SignupSerializer(serializers.Serializer):
    salon_name = serializers.CharField(max_length=200)
    slug = serializers.CharField(max_length=30)
    email = serializers.EmailField(max_length=150)
    password = serializers.CharField(trim_whitespace=False, max_length=128)
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    accept_terms = serializers.BooleanField()

    def validate_slug(self, value):
        value = value.strip().lower()
        problem = slug_error(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate_email(self, value):
        value = value.strip().lower()
        if login_taken(value):
            raise serializers.ValidationError('Этот email уже зарегистрирован. Войдите или восстановите пароль')
        return value

    def validate_phone(self, value):
        if not value:
            return ''
        phone = normalize_phone(value, PHONE_REGION)
        if not phone:
            raise serializers.ValidationError('Проверьте номер телефона')
        return phone

    def validate_accept_terms(self, value):
        if not value:
            raise serializers.ValidationError('Нужно принять условия сервиса')
        return value

    def validate(self, attrs):
        try:
            validate_password(attrs['password'], User(username=attrs.get('email', ''), email=attrs.get('email', '')))
        except DjangoValidationError as exc:
            raise serializers.ValidationError({'password': list(exc.messages)})
        return attrs


class SignupView(APIView):
    """POST — создать салон и войти. Салон скрыт от клиентов до подтверждения email."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.SIGNUP_ENABLED:
            raise StaffError(SIGNUP_CLOSED_MESSAGE, status.HTTP_403_FORBIDDEN)
        # Поле-ловушка: человек его не видит и не заполняет
        if request.data.get('website'):
            raise StaffError('Не удалось зарегистрироваться. Обновите страницу и попробуйте ещё раз')
        throttled(f'signup:ip:{client_ip(request)}', SIGNUPS_PER_IP_HOUR)
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        company, user = create_salon(
            name=data['salon_name'].strip(), slug=data['slug'], owner_login=data['email'], email=data['email'],
            password=data['password'], phone=data['phone'], pending=True,
        )
        email_sent = send_verification(user, company)
        return Response({**get_staff_tokens(user), 'me': staff_me(user, get_staff(user)),
                         'email_sent': email_sent}, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    """POST {token} — подтвердить email из письма: салон становится виден клиентам."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        throttled(f'signup:verify:{client_ip(request)}', CONFIRMS_PER_IP_HOUR)
        try:
            data = signing.loads(str(request.data.get('token', '')), salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE)
        except signing.BadSignature:
            raise StaffError(BAD_LINK_MESSAGE)
        user = User.objects.filter(pk=data.get('u')).first()
        if user is None or user.email.lower() != data.get('e'):
            raise StaffError(BAD_LINK_MESSAGE)
        profile = AdminProfile.objects.select_related('company').filter(user=user, role='owner').first()
        if profile is None or profile.company is None:
            raise StaffError(BAD_LINK_MESSAGE)
        company = profile.company
        if company.status == Company.STATUS_PENDING:
            company.status = Company.STATUS_ACTIVE
            company.save(update_fields=['status'])
        elif company.status == Company.STATUS_BLOCKED:
            raise StaffError('Салон заблокирован. Напишите в поддержку', status.HTTP_403_FORBIDDEN)
        return Response({'verified': True, 'slug': company.slug, 'public_url': public_url(company)})


class ResendVerificationView(APIView):
    """POST — отправить письмо подтверждения ещё раз (владелец салона, который ждёт подтверждения)."""
    permission_classes = [IsStaff]

    def post(self, request):
        company = request.staff.company
        if request.staff.role != 'owner' or company.status != Company.STATUS_PENDING or not request.user.email:
            raise StaffError('Подтверждение не требуется')
        throttled(f'signup:resend:{request.user.pk}', RESENDS_PER_USER_HOUR)
        if not send_verification(request.user, company):
            raise StaffError('Не удалось отправить письмо. Попробуйте через несколько минут',
                             status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({'sent': True, 'email': request.user.email})


# --- Сброс пароля ---

class PasswordResetView(APIView):
    """POST {email} — ссылка для сброса пароля на email. Ответ одинаковый, есть ли такой email."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.EMAIL_CONFIGURED:
            raise StaffError('Восстановление пароля пока недоступно. Обратитесь к владельцу салона',
                             status.HTTP_503_SERVICE_UNAVAILABLE)
        email = str(request.data.get('email', '')).strip().lower()
        if not email or '@' not in email or len(email) > 150:
            raise StaffError('Укажите email')
        throttled(f'reset:ip:{client_ip(request)}', RESETS_PER_IP_HOUR)
        # Лимит на адрес молча: не раскрываем, есть ли такой пользователь
        if not hit(f'reset:email:{email}', RESETS_PER_EMAIL_HOUR, 3600):
            for user in User.objects.filter(email__iexact=email, is_active=True):
                # Только персонал салона: клиенты сайта восстанавливают вход через Telegram
                if user.has_usable_password() and is_staff_user(user):
                    uid = urlsafe_base64_encode(force_bytes(user.pk))
                    token = default_token_generator.make_token(user)
                    link = f'{settings.PANEL_URL}/admin-panel/reset-password?uid={uid}&token={token}'
                    send('Сброс пароля — TimePoint',
                         f'Здравствуйте!\n\nКто-то (надеемся, вы) запросил сброс пароля для входа в панель салона '
                         f'(логин {user.username}). Новый пароль можно задать по ссылке:\n\n{link}\n\n'
                         f'Ссылка действует 2 часа и сработает один раз. Если вы не запрашивали сброс — '
                         f'ничего не делайте, пароль останется прежним.\n\nTimePoint',
                         user.email)
        return Response({'message': RESET_SENT_MESSAGE})


def is_staff_user(user):
    return (AdminProfile.objects.filter(user=user, company__isnull=False).exists()
            or Master.objects.filter(user=user, is_active=True).exists())


class PasswordResetConfirmView(APIView):
    """POST {uid, token, password} — новый пароль по ссылке из письма. Прежние входы сбрасываются."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        throttled(f'reset:confirm:{client_ip(request)}', CONFIRMS_PER_IP_HOUR)
        try:
            pk = force_str(urlsafe_base64_decode(str(request.data.get('uid', ''))))
            user = User.objects.filter(pk=int(pk), is_active=True).first()
        except (ValueError, TypeError, OverflowError):
            user = None
        token = str(request.data.get('token', ''))
        if user is None or not default_token_generator.check_token(user, token):
            raise StaffError(BAD_LINK_MESSAGE)
        password = str(request.data.get('password', ''))
        try:
            validate_password(password, user)
        except DjangoValidationError as exc:
            raise StaffError(' '.join(exc.messages))
        # Смена пароля делает недействительными и ссылку (токен зависит от хеша), и старые входы
        user.set_password(password)
        user.save(update_fields=['password'])
        return Response({'message': 'Пароль изменён. Войдите с новым паролем', 'username': user.username})


# --- Чек-лист настройки ---

class OnboardingView(APIView):
    """GET — что уже настроено в салоне и ссылка на сайт записи (владелец и администратор)."""
    permission_classes = [IsStaffManager]

    def get(self, request):
        company = request.staff.company
        masters = Master.objects.filter(company=company, is_active=True, services__is_active=True).distinct()
        has_schedule = (MasterSchedule.objects.filter(master__in=masters, is_working=True).exists()
                        or MasterWorkDay.objects.filter(master__in=masters).exists())
        staff_users = list(AdminProfile.objects.filter(company=company).values_list('user_id', flat=True))
        steps = [
            ('email', company.status == Company.STATUS_ACTIVE),
            ('salon', bool(company.address and company.phone)),
            ('services', Service.objects.filter(company=company, is_active=True).exists()),
            ('masters', masters.exists()),
            ('schedule', has_schedule),
            ('telegram', StaffTelegram.objects.filter(user_id__in=staff_users).exists()),
        ]
        done = {key: value for key, value in steps}
        return Response({
            'steps': [{'key': key, 'done': value} for key, value in steps],
            'done': sum(done.values()),
            'total': len(steps),
            # Клиенты могут записаться: салон подтверждён, есть мастер с услугой и рабочие дни
            'ready': done['email'] and done['masters'] and done['schedule'],
            'public_url': public_url(company),
            'status': company.status,
            'trial_ends_at': company.trial_ends_at.isoformat() if company.trial_ends_at else None,
        })
