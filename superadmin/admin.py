from django.contrib import admin

from .models import SuperAdminLog


@admin.register(SuperAdminLog)
class SuperAdminLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'action', 'company', 'ip')
    list_filter = ('action',)
    readonly_fields = ('created_at', 'user', 'action', 'company', 'details', 'ip')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
