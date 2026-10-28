from django.contrib import admin

from .models import AppointmentTelegram, ClientTelegram, OutgoingMessage, StaffTelegram


@admin.register(AppointmentTelegram)
class AppointmentTelegramAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'chat_id', 'reminder_24h_sent', 'reminder_1h_sent')
    raw_id_fields = ('appointment',)


@admin.register(StaffTelegram)
class StaffTelegramAdmin(admin.ModelAdmin):
    list_display = ('user', 'chat_id', 'linked_at')


@admin.register(ClientTelegram)
class ClientTelegramAdmin(admin.ModelAdmin):
    list_display = ('user', 'chat_id', 'linked_at')


@admin.register(OutgoingMessage)
class OutgoingMessageAdmin(admin.ModelAdmin):
    list_display = ('id', 'chat_id', 'created_at', 'sent_at', 'attempts', 'error')
    list_filter = ('sent_at',)
