from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from notifications import services as notifications
from .models import ClientProfile, Appointment
from .phone import normalize_phone
from .rules import cancel_rule_text, client_can_cancel
from .slots import ACTIVE_STATUSES
from .login_limit import (
    is_login_blocked, login_limit_key, register_login_failure, reset_login_failures, LOGIN_BLOCKED_MESSAGE
)
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from datetime import datetime

def get_token_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token)
    }

class RegisterView(APIView):
    def post(self, request):
        phone = request.data.get('phone')
        password = request.data.get('password')
        name = request.data.get('name')
        surname = request.data.get('surname')

        if not phone or not password or not name:
            return Response(
                {'error': 'Укажите телефон, имя и пароль'}, status=status.HTTP_400_BAD_REQUEST
            )

        phone = normalize_phone(phone)

        if not phone:
            return Response(
                {'error': 'Неверный формат номера телефона'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Логин клиента — телефон: занятым считается и пользователь с таким логином без профиля
        if ClientProfile.objects.filter(phone=phone).exists() or User.objects.filter(username=phone).exists():
            return Response(
                {'error': 'Этот номер уже занят'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=phone,
                    password=password,
                )
                ClientProfile.objects.create(user=user, phone=phone, name=name, surname=surname or '')
        except IntegrityError:
            # Одновременная регистрация того же номера
            return Response(
                {'error': 'Этот номер уже занят'},
                status=status.HTTP_400_BAD_REQUEST
            )
        tokens = get_token_for_user(user)
        return Response(tokens, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    def post(self, request):
        phone = request.data.get('phone')
        password = request.data.get('password')

        if not phone or not password:
            return Response(
                {'error': 'Укажите телефон или пароль'},
                status=status.HTTP_400_BAD_REQUEST
            )

        phone = normalize_phone(phone)
        if not phone:
            return Response(
                {'error': 'Неверный формат номера телефона'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Лимит перебора: не больше 5 неудачных попыток за 15 минут на один номер
        # (счётчик общий со входом в панель салона по этому же телефону)
        limit_key = login_limit_key(phone)
        if is_login_blocked(limit_key):
            return Response({'error': LOGIN_BLOCKED_MESSAGE}, status=status.HTTP_429_TOO_MANY_REQUESTS)

        user = User.objects.filter(username=phone).first()
        if user is None or not user.check_password(password):
            register_login_failure(limit_key)
            return Response(
                {'error': 'Неверный телефон или пароль'},
                status=status.HTTP_400_BAD_REQUEST
            )

        reset_login_failures(limit_key)
        tokens = get_token_for_user(user)
        return Response(tokens)


class MeView(APIView):
    """Профиль клиента: телефон, имя, фамилия."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = ClientProfile.objects.filter(user=request.user).first()
        if not profile:
            return Response({'error': 'Профиль клиента не найден'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'phone': profile.phone, 'name': profile.name, 'surname': profile.surname})


class MyAppointmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        appointments = Appointment.objects.filter(
            client=request.user
        ).select_related('master', 'service', 'company').order_by('-date', '-start_time')

        data = []

        # Мастер или услуга могли быть удалены (SET_NULL) — раньше это давало 500
        for apt in appointments:
            data.append({
                "id": apt.id,
                "master": apt.master.name if apt.master else None,
                "service": apt.service.name if apt.service else None,
                "date": apt.date,
                "start_time": apt.start_time,
                "end_time": apt.end_time,
                "status": apt.status,
                # Можно ли отменить самому (правило салона «не позже чем за N часов»)
                "can_cancel": client_can_cancel(apt),
            })
        return Response(data)

class CancelAppointmentsView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, appointment_id):
        try:
            appointment = Appointment.objects.get(
                id=appointment_id,
                client=request.user
            )
        except Appointment.DoesNotExist:
            return Response(
                {'error': 'Запись не найдена'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Отменить можно только предстоящую запись (не пришёл/пришёл/отменена — нельзя)
        if appointment.status not in ACTIVE_STATUSES:
            return Response(
                {'error': 'Эту запись нельзя отменить'},
                status=status.HTTP_400_BAD_REQUEST
            )

        starts_at = timezone.make_aware(datetime.combine(appointment.date, appointment.start_time))
        if starts_at <= timezone.now():
            return Response(
                {'error': 'Прошедшую запись нельзя отменить'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not client_can_cancel(appointment):
            return Response({'error': cancel_rule_text(appointment.company)}, status=status.HTTP_400_BAD_REQUEST)

        appointment.status = 'cancelled'
        appointment.save()
        notifications.appointment_cancelled_by_client(appointment)

        return Response({'message': 'Запись отменена'})
