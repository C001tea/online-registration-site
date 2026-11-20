"""Раздел «Пора записаться» в панели: кому пора снова на услугу и кто уходит."""
from collections import Counter

from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.phone import normalize_phone
from dashboard.errors import StaffError
from dashboard.permissions import IsStaffManagerOrIndependent
from dashboard.serializers import PHONE_REGION
from . import rhythm
from .models import RhythmMessage

MAX_ROWS = 300
MANUAL_CHANNELS = ('whatsapp', 'viber', 'phone')


def visible_master(request):
    """Владелец и админ видят весь салон, мастер (работает сам) — только своих клиентов."""
    staff = request.staff
    return None if staff.is_manager else staff.master


def sort_key(stage):
    if stage == 'leaving':
        # Сначала те, кто приносил больше: за них стоит бороться в первую очередь
        return lambda item: (-(item.average_check or 0), item.due)
    if stage == 'lost':
        return lambda item: (-item.last_visit.toordinal(), item.phone)
    return lambda item: (item.due, item.phone)


class RhythmView(APIView):
    """GET ?stage=soon|leaving|lost -> {enabled, counts, stats, results}"""
    permission_classes = [IsStaffManagerOrIndependent]

    def get(self, request):
        company = request.staff.company
        master = visible_master(request)
        today = timezone.localdate()
        stage = request.query_params.get('stage')
        if stage not in rhythm.STAGES:
            stage = 'soon'
        items = rhythm.client_rhythms(company, master, today)
        counts = Counter(item.stage for item in items)
        rows = sorted((item for item in items if item.stage == stage), key=sort_key(stage))[:MAX_ROWS]
        return Response({
            'enabled': company.rhythm_enabled,
            'counts': {name: counts.get(name, 0) for name in rhythm.STAGES},
            'stats': rhythm.returned_stats(company, master, since=today.replace(day=1)),
            'results': [item.to_dict(company, today) for item in rows],
        })


class MarkSerializer(serializers.Serializer):
    phone = serializers.CharField(max_length=30)
    group = serializers.CharField(max_length=20)
    channel = serializers.ChoiceField(choices=MANUAL_CHANNELS)


class RhythmMarkView(APIView):
    """POST {phone, group, channel: whatsapp|viber|phone} — сотрудник позвал клиента сам."""
    permission_classes = [IsStaffManagerOrIndependent]

    def post(self, request):
        serializer = MarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        phone = normalize_phone(data['phone'].strip(), PHONE_REGION)
        company = request.staff.company
        item = next((i for i in rhythm.client_rhythms(company, visible_master(request))
                     if i.phone == phone and i.group == data['group']), None)
        if not item:
            raise StaffError('Клиент не найден', status_code=404)
        if item.opted_out:
            raise StaffError('Клиент попросил не присылать такие сообщения')
        message = RhythmMessage.objects.create(
            company=company, phone=item.phone, group=item.group, cycle=item.last_visit,
            step=len(item.messages) + 1, channel=data['channel'], user=request.user)
        return Response({'channel': message.channel, 'step': message.step,
                         'sent_at': message.sent_at.isoformat()}, status=201)
