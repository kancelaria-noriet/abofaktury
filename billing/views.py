import secrets
from decimal import Decimal
from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST
from weasyprint import HTML

from . import subiekt

from .calc import Month, calculate
from .models import Client
from .source import SourceError, load_entries


def _current_month():
    today = timezone.localdate()
    return Month(today.year, today.month)


def _parse_month(text):
    try:
        return Month.parse(text)
    except ValueError:
        raise Http404('Nieprawidłowy miesiąc')


def _results(clients, month):
    """Read the source and calculate each client. Raise SourceError if the whole source fails."""
    work_month = month.previous()
    entries, errors = load_entries(clients, work_month.year)
    rows = []
    for client in clients:
        client_entries = entries.get(client.key, {})
        result = calculate(client, month, client_entries.get(work_month.key, []), client_entries)
        if client.key in errors:
            result.warnings.insert(0, f'Arkusz: {errors[client.key]}')
        rows.append((client, result))
    return rows


def index(request):
    return redirect('month', month=_current_month().key)


def go_to_month(request):
    """Target of the month selector form (?m=YYYY-MM)."""
    try:
        month = Month.parse(request.GET.get('m', ''))
    except ValueError:
        month = _current_month()
    return redirect('month', month=month.key)


def month_view(request, month):
    month = _parse_month(month)
    source_error = None
    try:
        rows = _results(list(Client.objects.filter(active=True)), month)
    except SourceError as e:
        rows, source_error = [], str(e)
    totals = {
        name: sum((getattr(result, name) for _, result in rows), Decimal('0'))
        for name in ['hours_total', 'overtime_hours', 'net', 'vat', 'gross']
    }
    totals['warnings'] = sum(len(result.warnings) for _, result in rows)
    return render(request, 'billing/month.html', {
        'month': month,
        'work_month': month.previous(),
        'rows': rows,
        'totals': totals,
        'source_error': source_error,
        'subiekt': subiekt.connection(),
        'generated_at': timezone.localtime(),
    })


@login_not_required
def privacy(request):
    """Public page: the InsERT developer portal requires a privacy policy URL for the app."""
    return render(request, 'billing/privacy.html')


def report_pdf(request, month, key):
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    try:
        [(_, result)] = _results([client], month)
    except SourceError as e:
        return HttpResponse(f'Błąd odczytu arkusza: {e}', status=502, content_type='text/plain; charset=utf-8')
    html = render_to_string('billing/report_pdf.html', {'client': client, 'result': result})
    pdf = HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()
    response = HttpResponse(pdf, content_type='application/pdf')
    filename = f'raport_{client.key}_{result.work_month.key}.pdf'
    response['Content-Disposition'] = f'inline; filename="{filename}"'
    return response


# --- Subiekt 123: connection (step 1, read-only) ------------------------------------------------

def subiekt_page(request):
    """Connection status, and on request the check of the client NIPs in Subiekt."""
    context = {'connection': subiekt.connection(), 'rows': None, 'error': None, 'total': None}
    if context['connection'] and request.GET.get('sprawdz'):
        try:
            subiekt_clients = subiekt.list_clients()
            context['total'] = len(subiekt_clients)
            context['rows'] = subiekt.match_clients(Client.objects.filter(active=True), subiekt_clients)
        except subiekt.SubiektError as e:
            context['error'] = str(e)
        context['connection'] = subiekt.connection()   # a failed refresh can remove it
    return render(request, 'billing/subiekt.html', context)


def subiekt_connect(request):
    """Start the OAuth login at InsERT (authorization code with PKCE)."""
    try:
        target = urlsplit(subiekt.redirect_uri())
        if request.get_host() != target.netloc:
            # The session cookie must come back with the callback, so start on the same host.
            return redirect(f'{target.scheme}://{target.netloc}{request.path}')
        verifier, challenge = subiekt.new_pkce()
        state = secrets.token_urlsafe(32)
        url = subiekt.authorize_url(state, challenge)
    except subiekt.SubiektError as e:
        messages.error(request, f'Nie można połączyć z Subiektem: {e}')
        return redirect('subiekt')
    request.session['subiekt_oauth'] = {'state': state, 'verifier': verifier}
    return redirect(url)


def subiekt_callback(request):
    """InsERT sends the browser back here after the login and the consent."""
    pending = request.session.pop('subiekt_oauth', None)
    if request.GET.get('error'):
        detail = request.GET.get('error_description') or request.GET['error']
        messages.error(request, f'InsERT nie udzielił dostępu: {detail}')
    elif not pending or not secrets.compare_digest(pending['state'], request.GET.get('state', '')):
        messages.error(request, 'Nieprawidłowy lub wygasły parametr state. Połącz ponownie.')
    elif not request.GET.get('code'):
        messages.error(request, 'InsERT nie przekazał kodu autoryzacji. Połącz ponownie.')
    else:
        try:
            conn = subiekt.exchange_code(request.GET['code'], pending['verifier'])
            messages.success(request, f'Połączono z Subiektem{f" jako {conn.account}" if conn.account else ""}.')
        except subiekt.SubiektError as e:
            messages.error(request, f'Nie można dokończyć połączenia: {e}')
    return redirect('subiekt')


@require_POST
def subiekt_disconnect(request):
    subiekt.disconnect()
    messages.success(request, 'Rozłączono z Subiektem. Tokeny zostały usunięte z bazy aplikacji.')
    return redirect('subiekt')
