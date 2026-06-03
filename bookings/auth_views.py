from django.contrib.auth.models import User
from phonenumbers.phonenumberutil import NumberParseException
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from .models import ClientProfile, Appointment
import phonenumbers
from rest_framework.permissions import IsAuthenticated

def get_token_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token)
    }

def normalize_phone(phone):
    try:
        parsed = phonenumbers.parse(phone, None)
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except NumberParseException:
        return None

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

        if ClientProfile.objects.filter(phone=phone).exists():
            return Response(
                {'error': 'Этот номер уже занят'},
                status=status.HTTP_400_BAD_REQUEST
            )

        user = User.objects.create_user(
            username=phone,
            password=password,
        )
        ClientProfile.objects.create(user=user, phone=phone, name=name, surname=surname or '')
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

        try:
            user = User.objects.get(username=phone)
        except User.DoesNotExist:
            return Response(
                {'error': 'Неверный телефон или пароль'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not user.check_password(password):
            return Response(
                {'error': 'Неверный телефон или пароль'},
                status=status.HTTP_400_BAD_REQUEST
            )

        tokens = get_token_for_user(user)
        return Response(tokens)

class MyAppointmentView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        appointments = Appointment.objects.filter(
            client=request.user
        ).order_by('-date', '-start_time')

        data = []

        for apt in appointments:
            data.append({
                "id": apt.id,
                "master": apt.master.name,
                "service": apt.service.name,
                "date": apt.date,
                "start_time": apt.start_time,
                "status": apt.status
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

        if appointment.status in ['cancellled', 'completed']:
            return Response(
                {'error': 'Эту запись нельзя отменить'},
                status=status.HTTP_400_BAD_REQUEST
            )

        appointment.status = 'cancelled'
        appointment.save()

        return Response({'message': 'Запись отменена'})
