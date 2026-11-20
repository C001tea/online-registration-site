"""Раздел «Отзывы» в панели: оценка салона и мастеров, все отзывы, фильтр «низкие»."""
from django.db.models import Avg, Count
from rest_framework.response import Response
from rest_framework.views import APIView

from dashboard.permissions import IsStaffManagerOrIndependent
from retention.views import visible_master
from .models import VisitReview

MAX_ROWS = 200


def average(value):
    return round(value, 1) if value is not None else None


class ReviewsView(APIView):
    """GET ?low=1 -> {enabled, summary, masters, results}"""
    permission_classes = [IsStaffManagerOrIndependent]

    def get(self, request):
        company = request.staff.company
        master = visible_master(request)
        requested = VisitReview.objects.filter(company=company)
        if master is not None:
            requested = requested.filter(master=master)
        rated = requested.filter(rating__isnull=False)
        totals = rated.aggregate(avg=Avg('rating'), count=Count('id'))
        masters = [
            {'id': row['master_id'], 'name': row['master__name'], 'average': average(row['avg']), 'count': row['count']}
            for row in rated.filter(master__isnull=False).values('master_id', 'master__name')
            .annotate(avg=Avg('rating'), count=Count('id')).order_by('master__name')
        ]
        rows = rated.select_related('appointment', 'appointment__service', 'master').order_by('-rated_at', '-id')
        if request.query_params.get('low') == '1':
            rows = rows.filter(rating__lte=3)
        return Response({
            'enabled': company.reviews_enabled,
            'summary': {'average': average(totals['avg']), 'count': totals['count'],
                        'requested': requested.count(), 'low': rated.filter(rating__lte=3).count()},
            'masters': masters,
            'results': [{
                'id': review.id,
                'rating': review.rating,
                'comment': review.comment,
                'publish': review.publish,
                'rated_at': review.rated_at.isoformat(),
                'client_name': review.appointment.client_name,
                'phone': review.phone,
                'master': {'id': review.master_id, 'name': review.master.name} if review.master else None,
                'service': review.appointment.service.name if review.appointment.service else None,
                'date': review.appointment.date.isoformat(),
                'time': review.appointment.start_time.strftime('%H:%M'),
                'channel': review.channel,
            } for review in rows[:MAX_ROWS]],
        })
