from django.contrib import admin
from .models import (
    Company, Master, Service, ServiceCategory, Appointment, MasterSchedule, ClientProfile, AdminProfile, MasterDayOff, MasterWorkDay
)

@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    # Новые салоны (самостоятельная регистрация): статус, пробный период, дата регистрации
    list_display = ('name', 'slug', 'status', 'trial_ends_at', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'slug')
    ordering = ('-created_at',)

@admin.register(Master)
class MasterAdmin(admin.ModelAdmin):
    list_display = ['company', 'name']

admin.site.register(Service)
admin.site.register(ServiceCategory)
admin.site.register(Appointment)
admin.site.register(MasterSchedule)
admin.site.register(ClientProfile)
admin.site.register(MasterDayOff)
admin.site.register(MasterWorkDay)


@admin.register(AdminProfile)
class AdminProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'company', 'role']
    # access_token устарел — вход в панель по логину и паролю
    exclude = ('access_token',)
    raw_id_fields = ('user',)
