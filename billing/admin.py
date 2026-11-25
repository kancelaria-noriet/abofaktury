from django.contrib import admin

from .models import Client

admin.site.site_header = 'abofaktury'
admin.site.site_title = 'abofaktury'
admin.site.index_title = 'Konfiguracja'
admin.site.site_url = '/'


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ['name', 'nip', 'sheet_tab', 'subscription_net', 'included_hours', 'overtime_rate_net',
                    'overtime_period_months', 'send_hour_reports', 'active']
    list_filter = ['active']
    search_fields = ['name', 'nip', 'key']
