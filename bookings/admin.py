from django.contrib import admin
from .models import (
    Company, Master, Service, ServiceCategory, Appointment, MasterSchedule, ClientProfile, AdminProfile, MasterDayOff, MasterWorkDay
)

admin.site.register(Company)

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
