from django.urls import path, register_converter

from . import views


class MonthConverter:
    regex = r'\d{4}-\d{2}'

    def to_python(self, value):
        return value

    def to_url(self, value):
        return str(value)


register_converter(MonthConverter, 'month')

urlpatterns = [
    path('', views.index, name='index'),
    path('privacy', views.privacy, name='privacy'),
    path('healthz', views.healthz, name='healthz'),
    path('subiekt/', views.subiekt_page, name='subiekt'),
    path('gmail/', views.gmail_page, name='gmail'),
    path('gmail/connect', views.gmail_connect, name='gmail_connect'),
    path('gmail/disconnect', views.gmail_disconnect, name='gmail_disconnect'),
    path('subiekt/connect', views.subiekt_connect, name='subiekt_connect'),
    path('subiekt/callback', views.subiekt_callback, name='subiekt_callback'),
    path('subiekt/disconnect', views.subiekt_disconnect, name='subiekt_disconnect'),
    path('miesiac/', views.go_to_month, name='go_to_month'),
    path('<month:month>/', views.month_view, name='month'),
    path('<month:month>/<slug:key>/raport.pdf', views.report_pdf, name='report_pdf'),
    path('<month:month>/subiekt/', views.subiekt_batch, name='subiekt_batch'),
    path('<month:month>/email/', views.email_batch, name='email_batch'),
    path('<month:month>/email/wyslij', views.email_batch_send, name='email_batch_send'),
    path('<month:month>/subiekt/utworz', views.subiekt_batch_create, name='subiekt_batch_create'),
    path('<month:month>/<slug:key>/subiekt/', views.subiekt_invoice, name='subiekt_invoice'),
    path('<month:month>/<slug:key>/subiekt/utworz', views.subiekt_invoice_create, name='subiekt_invoice_create'),
    path('<month:month>/<slug:key>/subiekt/usun', views.subiekt_invoice_delete, name='subiekt_invoice_delete'),
]
