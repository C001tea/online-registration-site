from rest_framework import generics, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.shortcuts import get_object_or_404
from .models import Company, Master, Appointment, Service, MasterSchedule, Review
from .serializers import MasterSerializer, ServiceSerializer, AppointmentCreateSerializer, CompanySerializer, ReviewSerializer
from datetime import datetime, timedelta
from django.db.models import Avg



def get_free_slots(date, master, duration_time):


    try:
        schedule = MasterSchedule.objects.get(master=master, day_of_week=date.weekday(), is_working=True)
    except MasterSchedule.DoesNotExist:
        return []

    duration = timedelta(minutes=duration_time)

    busy = Appointment.objects.filter(
        master=master,
        date=date,
        status__in=['pending', 'confirmed']
    ).values_list('start_time', 'end_time')

    slots = []
    current = datetime.combine(date, schedule.start_time)
    end = datetime.combine(date, schedule.end_time)
    while duration + current <= end:
        slots.append(current.time())
        current += timedelta(minutes=30)

    free_slots = []
    for slot in slots:
        slot_start = datetime.combine(date, slot)
        slot_end = slot_start + duration

        is_busy = any(datetime.combine(date, busy_start) < slot_end and datetime.combine(date, busy_end) > slot_start
                      for busy_start, busy_end in busy)

        if not is_busy:
            free_slots.append(slot)

    return free_slots


class MasterListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company, slug=slug)
        masters = Master.objects.all()
        service_id = request.query_params.get('service')
        if service_id:
            service = get_object_or_404(Service, company=company, id=service_id)
            masters = masters.filter(services=service)

        serializer = MasterSerializer(masters, many=True)
        return Response(serializer.data)

class ServiceListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company, slug=slug)
        services = company.services.all()
        master_id = request.query_params.get('master_id')
        date = request.query_params.get("date")
        time = request.query_params.get("time")

        if master_id:
            master = get_object_or_404(Master, company=company, id=master_id)
            services = services.filter(masters=master)

        elif date and time:
            date = datetime.strptime(date, "%Y-%m-%d").date()
            targeted_time = datetime.strptime(time, '%H:%M').time()

            fitting_ids = []
            for service in services:
                for master in company.masters.filter(services=service):
                    free = get_free_slots(date, master, service.duration)
                    if targeted_time in free:
                        fitting_ids.append(service.id)
                        break
            services = services.filter(id__in=fitting_ids)

        serializer = ServiceSerializer(services, many=True)

        return Response(serializer.data)


class AppointmentCreateView(generics.CreateAPIView):
    serializer_class = AppointmentCreateSerializer

    def perform_create(self, serializer):
        company = get_object_or_404(Company, slug=self.kwargs['slug'])

        service = serializer.validated_data['service']
        start_time = serializer.validated_data['start_time']
        date = serializer.validated_data['date']

        start_dt = datetime.combine(date, start_time)
        end_dt = start_dt + timedelta(minutes=service.duration)

        client = self.request.user if self.request.user.is_authenticated else None

        serializer.save(company=company, status='pending', end_time=end_dt.time(), client=client)



class SelectData(APIView):
    def get(self, request, slug):
        all_free_points = set()
        company = get_object_or_404(Company, slug=slug)
        masters = Master.objects.filter(company=company)
        date = request.query_params.get('date')
        try:
            date = datetime.strptime(date, '%Y-%m-%d').date()
        except ValueError:
            return Response({"error": "Date format: YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)
        duration = timedelta(minutes=30)

        for master in masters.prefetch_related("appointments").all():
            try:
                schedule = MasterSchedule.objects.get(master=master, day_of_week=date.weekday(), is_working=True)
            except MasterSchedule.DoesNotExist:
                continue

            busy = Appointment.objects.filter(
                master=master,
                date=date,
                status__in=['pending', 'confirmed']
            ).values_list('start_time', 'end_time')

            slots = []
            current = datetime.combine(date, schedule.start_time)
            end = datetime.combine(date, schedule.end_time)
            while duration + current <= end:
                slots.append(current.time())
                current += timedelta(minutes=30)

            for slot in slots:
                slot_start = datetime.combine(date, slot)
                slot_end = slot_start + duration

                is_busy = any(datetime.combine(date, busy_start) < slot_end and datetime.combine(date, busy_end) > slot_start
                              for busy_start, busy_end in busy)

                if not is_busy:
                    all_free_points.add(slot.strftime('%H:%M'))

        free_points = sorted(all_free_points)
        return Response(free_points)

class SlotListView(APIView):
    def get(self, request, slug):
        company = get_object_or_404(Company, slug=slug)

        master_id = request.query_params.get('master')
        service_id = request.query_params.get('service')
        date_str = request.query_params.get('date')

        if not all([master_id, service_id, date_str]):
            return Response({'error': 'Specify master, service and date'}, status=status.HTTP_400_BAD_REQUEST)


        master = get_object_or_404(Master, id=master_id, company=company)
        service = get_object_or_404(Service, id=service_id, company=company)

        try:
            date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({"error": "Date format: YYYY-MM-DD"}, status=status.HTTP_400_BAD_REQUEST)


        day_of_week = date.weekday()

        try:
            schedule = MasterSchedule.objects.get(master=master, day_of_week=day_of_week, is_working=True)
        except MasterSchedule.DoesNotExist:
            return Response({'date': date_str, 'slots': []})

        slots = []
        current = datetime.combine(date, schedule.start_time)
        end = datetime.combine(date, schedule.end_time)
        duration = timedelta(minutes=service.duration)
        while duration + current <= end:
            slots.append(current.time())
            current += timedelta(minutes=30)

        busy = Appointment.objects.filter(
            master=master,
            date=date,
            status__in=['pending', 'confirmed']
        ).values_list('start_time', 'end_time')

        free_slots = []
        for slot in slots:
            slot_start = datetime.combine(date, slot)
            slot_end = slot_start + duration

            is_busy = False
            for busy_start, busy_end in busy:
                busy_start_dt = datetime.combine(date, busy_start)
                busy_end_dt = datetime.combine(date, busy_end)

                if busy_end_dt > slot_start and slot_end > busy_start_dt:
                    is_busy = True
                    break

            if not is_busy:
                free_slots.append(slot.strftime('%H:%M'))

        return Response({
            'date': date_str,
            'master': master_id,
            'service': service_id,
            'slots': free_slots
        })

class CompanyInfoView(generics.RetrieveAPIView):
    serializer_class = CompanySerializer
    lookup_field = 'slug'
    queryset = Company.objects.all()

class ReviewView(APIView):

    def get(self, request, slug, master_id):
        company = get_object_or_404(Company, slug=slug)
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

        company = get_object_or_404(Company, slug=slug)
        master = get_object_or_404(Master, id=master_id, company=company)

        if Review.objects.filter(master=master, client=request.user).exists():
            return Response(
                {'error': 'Вы уже оставили отзыв на этого мастера'}
            )

        serializer = ReviewSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(master=master, client=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class MasterServices(generics.ListAPIView):
    serializer_class = ServiceSerializer

    def get_queryset(self):
        company = get_object_or_404(Company, slug=self.kwargs['slug'])
        master = get_object_or_404(Master, id=self.kwargs['master_id'], company=company)
        return master.services.all()