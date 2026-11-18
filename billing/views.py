import hashlib
import json
import logging
import secrets
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from weasyprint import HTML

from . import subiekt

logger = logging.getLogger(__name__)

from .calc import Month, calculate
from .models import Client, SubiektInvoice
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
    invoices = {invoice.client_id: invoice for invoice in SubiektInvoice.objects.filter(invoice_month=month.key)}
    for client, result in rows:
        result.subiekt_invoice = invoices.get(client.pk)
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


# --- Subiekt 123: invoices (step 2) --------------------------------------------------------------

def _month_page(month, client):
    return f"{reverse('month', args=[month.key])}#klient-{client.key}"


def _signature(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _existing_invoices(client, month):
    """The client's invoices in Subiekt issued in the invoice month (also those not made by the app)."""
    first = date(month.year, month.month, 1)
    following = month.next()
    return subiekt.client_invoices(client.nip, first, date(following.year, following.month, 1))


def _invoice_draft(client, month):
    """Calculate the invoice and build the Subiekt request body.

    Return (result, body, problem). Raise SourceError if the sheet cannot be read.
    """
    [(_, result)] = _results([client], month)
    try:
        subiekt_client = subiekt.find_client(subiekt.list_clients(), client.nip)
        symbols = sorted({subiekt.PRODUCT_SYMBOLS[line.name] for line in result.lines
                          if line.name in subiekt.PRODUCT_SYMBOLS})
        products = subiekt.find_products(subiekt.list_products(), symbols)
        body = subiekt.build_invoice(result, subiekt_client, products, settings.SUBIEKT_ISSUER,
                                     timezone.localdate(), settings.SUBIEKT_PLACE)
    except subiekt.SubiektError as e:
        return result, None, str(e)
    return result, body, None


def subiekt_invoice(request, month, key):
    """Preview of the invoice for one client and month. Nothing is created here."""
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    context = {'client': client, 'month': month, 'result': None, 'body': None, 'problem': None,
               'existing': SubiektInvoice.objects.filter(client=client, invoice_month=month.key).first(),
               'connection': subiekt.connection()}
    if context['existing'] is None and context['connection'] is not None:
        try:
            result, body, problem = _invoice_draft(client, month)
        except SourceError as e:
            result, body, problem = None, None, f'Nie można odczytać arkusza: {e}'
        context.update(result=result, body=body, problem=problem, signature=_signature(body) if body else '')
        if body:
            try:
                context['other_invoices'] = _existing_invoices(client, month)
            except subiekt.SubiektError as e:
                context['problem'] = f'nie można sprawdzić innych faktur klienta: {e}'
                context['body'] = None
            context['needs_confirmation'] = any(i['has_subscription'] for i in context.get('other_invoices', []))
    return render(request, 'billing/subiekt_invoice.html', context)


@require_POST
def subiekt_invoice_create(request, month, key):
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    preview = reverse('subiekt_invoice', args=[month.key, client.key])
    try:
        result, body, problem = _invoice_draft(client, month)
    except SourceError as e:
        messages.error(request, f'Nie utworzono faktury. Nie można odczytać arkusza: {e}')
        return redirect(preview)
    if problem:
        logger.warning('Subiekt invoice %s %s not created: %s', client.key, month.key, problem)
        messages.error(request, f'Nie utworzono faktury: {problem}')
        return redirect(preview)
    if _signature(body) != request.POST.get('signature'):
        logger.warning('Subiekt invoice %s %s not created: data changed since the preview', client.key, month.key)
        messages.error(request, 'Nie utworzono faktury: dane zmieniły się od podglądu. Sprawdź podgląd ponownie.')
        return redirect(preview)
    try:
        other_invoices = _existing_invoices(client, month)
    except subiekt.SubiektError as e:
        messages.error(request, f'Nie utworzono faktury: nie można sprawdzić innych faktur klienta: {e}')
        return redirect(preview)
    if any(i['has_subscription'] for i in other_invoices) and not request.POST.get('confirm_subscription'):
        messages.error(request, 'Nie utworzono faktury: klient ma już w tym miesiącu fakturę z abonamentem. '
                                'Potwierdź w podglądzie, że chcesz wystawić kolejną.')
        return redirect(preview)

    # Claim the client and month first, so that a double click cannot create two invoices.
    try:
        with transaction.atomic():
            invoice = SubiektInvoice.objects.create(client=client, invoice_month=month.key, document_id='',
                                                    net=result.net, gross=result.gross, created_at=timezone.now())
    except IntegrityError:
        messages.error(request, 'Faktura dla tego klienta i miesiąca już istnieje (lub jest w trakcie tworzenia).')
        return redirect(_month_page(month, client))
    try:
        invoice.document_id = subiekt.create_document(body)
    except subiekt.SubiektError as e:
        invoice.delete()
        logger.warning('Subiekt invoice %s %s rejected by Subiekt: %s', client.key, month.key, e)
        messages.error(request, f'Subiekt nie utworzył faktury: {e}')
        return redirect(preview)
    invoice.save(update_fields=['document_id'])

    try:
        document = subiekt.get_document(invoice.document_id)
    except subiekt.SubiektError as e:
        document = None
        messages.warning(request, f'Faktura jest utworzona, ale nie można jej odczytać: {e}')
    if document:
        invoice.document_number = document.get('documentNumber') or ''
        invoice.save(update_fields=['document_number'])
        differences = [
            f'{label}: Subiekt {document.get(field)}, aplikacja {expected}'
            for label, field, expected in [('netto', 'totalNet', result.net), ('brutto', 'totalGross', result.gross),
                                           ('do zapłaty', 'leftToPay', result.to_pay)]
            if document.get(field) is not None and abs(Decimal(str(document[field])) - expected) > Decimal('0.01')
        ]
        if differences:
            messages.warning(request, 'Sumy w Subiekcie różnią się od wyliczenia aplikacji: ' + '; '.join(differences))
    messages.success(request, f'Utworzono fakturę {invoice.document_number or invoice.document_id} w Subiekcie. '
                              'Sprawdź ją w Subiekcie i wyślij do KSeF.')
    return redirect(_month_page(month, client))


@require_POST
def subiekt_invoice_delete(request, month, key):
    """Delete the invoice in Subiekt, but only while it has no KSeF number."""
    month = _parse_month(month)
    invoice = get_object_or_404(SubiektInvoice, client__key=key, invoice_month=month.key)
    back = redirect(_month_page(month, invoice.client))
    label = invoice.document_number or invoice.document_id or 'bez numeru'
    document = None
    if invoice.document_id:
        try:
            document = subiekt.get_document(invoice.document_id)
            if document and document.get('ksefNumber'):
                messages.error(request, f'Faktura {label} ma numer KSeF. Aplikacja jej nie usuwa; '
                                        'w razie potrzeby wystaw korektę w Subiekcie.')
                return back
            if document:
                subiekt.delete_document(invoice.document_id)
        except subiekt.SubiektError as e:
            logger.warning('Subiekt invoice %s not deleted: %s', label, e)
            messages.error(request, f'Nie usunięto faktury {label}: {e}')
            return back
    invoice.delete()
    if document:
        messages.success(request, f'Usunięto fakturę {label} z Subiektu.')
    else:
        messages.success(request, f'Faktury {label} nie ma w Subiekcie. Usunięto wpis w aplikacji.')
    return back
