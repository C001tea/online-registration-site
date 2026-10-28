from decimal import Decimal

from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.models import Appointment, Master
from dashboard.errors import StaffError
from dashboard.permissions import IsStaff, IsStaffManager, IsStaffOwner, NOT_ENOUGH_RIGHTS_MESSAGE
from dashboard.views import date_range_params, parse_date
from . import services
from .models import METHOD_CHOICES, CashOperation, Payment, SalaryPayout, SalaryScheme

MAX_PERIOD_DAYS = 366


def period(request):
    date_from, date_to = date_range_params(request.query_params)
    if (date_to - date_from).days >= MAX_PERIOD_DAYS:
        raise StaffError('Период — не больше года')
    return date_from, date_to


def first_error(serializer):
    """Первое сообщение валидации в формате панели: {"error": "..."}."""
    for field, errors in serializer.errors.items():
        message = errors[0] if isinstance(errors, list) else errors
        raise StaffError(str(message), field=field)


class CheckoutSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=METHOD_CHOICES, error_messages={'invalid_choice': 'Выберите способ оплаты'})
    discount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=Decimal('0'))


class OperationSerializer(serializers.Serializer):
    date = serializers.DateField()
    kind = serializers.ChoiceField(choices=CashOperation.KIND_CHOICES)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=METHOD_CHOICES, default='cash')
    category = serializers.CharField(max_length=100)
    comment = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')


class SchemeSerializer(serializers.Serializer):
    percent = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=Decimal('0'), max_value=Decimal('100'))
    fixed_per_day = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0'))


class PayoutSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal('0.01'))
    method = serializers.ChoiceField(choices=METHOD_CHOICES, default='cash')

    def validate(self, data):
        if data['date_to'] < data['date_from']:
            raise serializers.ValidationError({'date_to': 'Конец периода раньше начала'})
        return data


def validated(serializer_class, data):
    serializer = serializer_class(data=data)
    if not serializer.is_valid():
        first_error(serializer)
    return serializer.validated_data


# --- Оплата визита ---

class CheckoutView(APIView):
    """POST — принять оплату (владелец, админ); DELETE — отменить оплату (только владелец)."""
    permission_classes = [IsStaffManager]

    def get_appointment(self, request, pk):
        return get_object_or_404(
            Appointment.objects.select_related('service', 'master', 'company'), pk=pk, company=request.staff.company
        )

    def post(self, request, pk):
        data = validated(CheckoutSerializer, request.data)
        appointment = self.get_appointment(request, pk)
        payment = services.checkout(appointment, method=data['method'], discount=data['discount'], user=request.user)
        return Response(services.payment_row(payment), status=status.HTTP_201_CREATED)

    def delete(self, request, pk):
        if request.staff.role != 'owner':
            raise StaffError(NOT_ENOUGH_RIGHTS_MESSAGE, status_code=status.HTTP_403_FORBIDDEN)
        appointment = self.get_appointment(request, pk)
        deleted, _ = Payment.objects.filter(appointment=appointment).delete()
        if not deleted:
            raise StaffError('Визит не оплачен', status_code=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Отчёт и касса (владелец) ---

class SummaryView(APIView):
    permission_classes = [IsStaffOwner]

    def get(self, request):
        date_from, date_to = period(request)
        return Response(services.summary(request.staff.company, date_from, date_to))


class CashDayView(APIView):
    permission_classes = [IsStaffOwner]

    def get(self, request):
        day = parse_date(request.query_params.get('date'))
        if not day:
            raise StaffError('Укажите date в формате YYYY-MM-DD')
        return Response(services.cash_day(request.staff.company, day))


class OperationsView(APIView):
    permission_classes = [IsStaffOwner]

    def get(self, request):
        date_from, date_to = period(request)
        ops = CashOperation.objects.filter(company=request.staff.company, date__gte=date_from, date__lte=date_to)
        return Response({
            'operations': [services.operation_row(o) for o in ops.select_related('salary_payout').order_by('-date', '-created_at')],
            'income_categories': services.INCOME_CATEGORIES,
            'expense_categories': services.EXPENSE_CATEGORIES,
        })

    def post(self, request):
        data = validated(OperationSerializer, request.data)
        operation = CashOperation.objects.create(company=request.staff.company, created_by=request.user, **data)
        return Response(services.operation_row(operation), status=status.HTTP_201_CREATED)


class OperationDetailView(APIView):
    permission_classes = [IsStaffOwner]

    def delete(self, request, pk):
        operation = get_object_or_404(CashOperation, pk=pk, company=request.staff.company)
        if hasattr(operation, 'salary_payout'):
            raise StaffError('Это выплата зарплаты — отмените её в разделе «Зарплаты»')
        operation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Зарплаты ---

def company_master(request, master_id):
    return get_object_or_404(Master, pk=master_id, company=request.staff.company)


class SalariesView(APIView):
    """Начисления всех мастеров за период (владелец)."""
    permission_classes = [IsStaffOwner]

    def get(self, request):
        date_from, date_to = period(request)
        company = request.staff.company
        with_payments = Payment.objects.filter(company=company, date__gte=date_from, date__lte=date_to).values('master')
        masters = Master.objects.filter(company=company).filter(
            is_active=True) | Master.objects.filter(company=company, id__in=with_payments)
        rows = [services.salary_row(m, date_from, date_to) for m in masters.distinct().order_by('name')]
        return Response(rows)


class SchemeView(APIView):
    permission_classes = [IsStaffOwner]

    def put(self, request, master_id):
        master = company_master(request, master_id)
        data = validated(SchemeSerializer, request.data)
        SalaryScheme.objects.update_or_create(master=master, defaults=data)
        scheme = services.scheme_of(master)
        return Response({'percent': str(scheme.percent), 'fixed_per_day': str(scheme.fixed_per_day)})


class PayoutsView(APIView):
    permission_classes = [IsStaffOwner]

    def post(self, request, master_id):
        master = company_master(request, master_id)
        data = validated(PayoutSerializer, request.data)
        payout = services.pay_salary(master, user=request.user, **data)
        return Response({'id': payout.id, 'amount': str(payout.amount)}, status=status.HTTP_201_CREATED)


class PayoutDetailView(APIView):
    permission_classes = [IsStaffOwner]

    def delete(self, request, pk):
        payout = get_object_or_404(SalaryPayout, pk=pk, company=request.staff.company)
        payout.operation.delete()  # выплата удаляется вместе с расходом по кассе
        return Response(status=status.HTTP_204_NO_CONTENT)


class MySalaryView(APIView):
    """Зарплата мастера за период — сам мастер видит только свою."""
    permission_classes = [IsStaff]

    def get(self, request):
        master = request.staff.master
        if master is None:
            raise StaffError('У вашего аккаунта нет профиля мастера', status_code=status.HTTP_404_NOT_FOUND)
        date_from, date_to = period(request)
        row = services.salary_row(master, date_from, date_to)
        row['visits_list'] = services.salary_visits(master, date_from, date_to)
        return Response(row)
