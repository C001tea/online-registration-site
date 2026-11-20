from django.contrib import admin

from .models import VisitReview


@admin.register(VisitReview)
class VisitReviewAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'company', 'master', 'rating', 'publish', 'channel', 'sent_at')
    list_filter = ('rating', 'channel')
    raw_id_fields = ('appointment',)
