from django.contrib import admin
from .models import Company, Master, Service, ServiceCategory, Appointment, MasterSchedule, ClientProfile

admin.site.register(Company)
admin.site.register(Master)
admin.site.register(Service)
admin.site.register(ServiceCategory)
admin.site.register(Appointment)
admin.site.register(MasterSchedule)
admin.site.register(ClientProfile)