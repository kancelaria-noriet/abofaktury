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

from . import gmail, mailing, subiekt

logger = logging.getLogger(__name__)

from .calc import Month, calculate
from .models import Client, EmailSend, SubiektInvoice
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
    sends = {send.client_id: send for send in EmailSend.objects.filter(invoice_month=month.key).order_by('sent_at')}
    for client, result in rows:
        result.subiekt_invoice = invoices.get(client.pk)
        result.last_email = sends.get(client.pk)
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
        'gmail_connected': gmail.connection() is not None,
        'generated_at': timezone.localtime(),
    })


@login_not_required
def privacy(request):
    """Public page: the InsERT developer portal requires a privacy policy URL for the app."""
    return render(request, 'billing/privacy.html')


@login_not_required
def healthz(request):
    """Health check for the container: the app runs and the database answers. No data, no external calls."""
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
    except Exception:   # any database fault means "not healthy"
        logger.exception('Health check: the database does not answer')
        return HttpResponse('database error\n', status=503, content_type='text/plain')
    return HttpResponse('ok\n', content_type='text/plain')


def _report_pdf(request, client, result):
    html = render_to_string('billing/report_pdf.html', {'client': client, 'result': result})
    return HTML(string=html, base_url=request.build_absolute_uri('/')).write_pdf()


def report_pdf(request, month, key):
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    try:
        [(_, result)] = _results([client], month)
    except SourceError as e:
        return HttpResponse(f'Błąd odczytu arkusza: {e}', status=502, content_type='text/plain; charset=utf-8')
    pdf = _report_pdf(request, client, result)
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


class SubiektLookup:
    """The Subiekt data of one request, loaded once: the clients, the products, the documents of the month."""

    def __init__(self, month):
        self.month = month
        self._clients = self._products = self._documents = None

    def clients(self):
        if self._clients is None:
            self._clients = subiekt.list_clients()
        return self._clients

    def products(self):
        if self._products is None:
            self._products = subiekt.list_products()
        return self._products

    def documents(self):
        if self._documents is None:
            first, following = date(self.month.year, self.month.month, 1), self.month.next()
            self._documents = subiekt.documents_issued_between(first, date(following.year, following.month, 1))
        return self._documents


class Draft:
    """One planned invoice: the calculation, the request body, and what stops it or needs a confirmation."""

    def __init__(self, client, result, existing):
        self.client, self.result, self.existing = client, result, existing
        self.body, self.problem, self.other_invoices = None, None, []

    @property
    def signature(self):
        return _signature(self.body) if self.body else ''

    @property
    def needs_confirmation(self):
        return any(invoice['has_subscription'] for invoice in self.other_invoices)

    @property
    def can_create(self):
        return self.existing is None and self.body is not None


def _drafts(clients, month):
    """Plan the invoices of the clients for the month. Raise SourceError if the sheet cannot be read."""
    lookup = SubiektLookup(month)
    existing = {invoice.client_id: invoice
                for invoice in SubiektInvoice.objects.filter(invoice_month=month.key, client__in=clients)}
    drafts = []
    for client, result in _results(clients, month):
        draft = Draft(client, result, existing.get(client.pk))
        if draft.existing is None:
            try:
                subiekt_client = subiekt.find_client(lookup.clients(), client.nip)
                symbols = sorted({subiekt.PRODUCT_SYMBOLS[line.name] for line in result.lines
                                  if line.name in subiekt.PRODUCT_SYMBOLS})
                products = subiekt.find_products(lookup.products(), symbols)
                draft.body = subiekt.build_invoice(result, subiekt_client, products, settings.SUBIEKT_ISSUER,
                                                   timezone.localdate(), settings.SUBIEKT_PLACE, _bank_account())
                draft.other_invoices = subiekt.client_invoices(lookup.documents(), client.nip)
            except subiekt.SubiektError as e:
                draft.body, draft.problem = None, str(e)
        drafts.append(draft)
    return drafts


def _bank_account():
    return {'name': settings.SUBIEKT_BANK_ACCOUNT_NAME, 'number': settings.SUBIEKT_BANK_ACCOUNT_NUMBER}


def _create(draft, month, signature, confirmed):
    """Create one planned invoice in Subiekt. Return a list of (message level, text)."""
    client, result = draft.client, draft.result
    if draft.existing is not None:
        number = draft.existing.document_number or 'bez numeru'
        return [(messages.ERROR, f'{client.name}: faktura już istnieje ({number}).')]
    if draft.problem:
        logger.warning('Subiekt invoice %s %s not created: %s', client.key, month.key, draft.problem)
        return [(messages.ERROR, f'{client.name}: nie utworzono faktury: {draft.problem}')]
    if draft.signature != signature:
        logger.warning('Subiekt invoice %s %s not created: data changed since the preview', client.key, month.key)
        return [(messages.ERROR, f'{client.name}: nie utworzono faktury: dane zmieniły się od podglądu. '
                                 'Sprawdź podgląd ponownie.')]
    if draft.needs_confirmation and not confirmed:
        return [(messages.ERROR, f'{client.name}: nie utworzono faktury: klient ma już w tym miesiącu fakturę '
                                 'z abonamentem. Potwierdź w podglądzie, że chcesz wystawić kolejną.')]

    # Claim the client and month first, so that a double click cannot create two invoices.
    try:
        with transaction.atomic():
            invoice = SubiektInvoice.objects.create(client=client, invoice_month=month.key, document_id='',
                                                    net=result.net, gross=result.gross, created_at=timezone.now())
    except IntegrityError:
        return [(messages.ERROR, f'{client.name}: faktura dla tego miesiąca już istnieje '
                                 '(lub jest w trakcie tworzenia).')]
    try:
        invoice.document_id = subiekt.create_document(draft.body)
    except subiekt.SubiektError as e:
        invoice.delete()
        logger.warning('Subiekt invoice %s %s rejected by Subiekt: %s', client.key, month.key, e)
        return [(messages.ERROR, f'{client.name}: Subiekt nie utworzył faktury: {e}')]
    invoice.save(update_fields=['document_id'])

    notes = []
    try:
        document = subiekt.get_document(invoice.document_id)
    except subiekt.SubiektError as e:
        document = None
        notes.append((messages.WARNING, f'{client.name}: faktura jest utworzona, ale nie można jej odczytać: {e}'))
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
            notes.append((messages.WARNING, f'{client.name}: sumy w Subiekcie różnią się od wyliczenia aplikacji: '
                                            + '; '.join(differences)))
    return [(messages.SUCCESS, f'{client.name}: utworzono fakturę {invoice.document_number or invoice.document_id} '
                               'w Subiekcie.')] + notes


def _add_messages(request, results):
    for level, text in results:
        messages.add_message(request, level, text)


def subiekt_invoice(request, month, key):
    """Preview of the invoice for one client and month. Nothing is created here."""
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    context = {'client': client, 'month': month, 'result': None, 'body': None, 'problem': None,
               'existing': SubiektInvoice.objects.filter(client=client, invoice_month=month.key).first(),
               'connection': subiekt.connection()}
    if context['existing'] is None and context['connection'] is not None:
        try:
            [draft] = _drafts([client], month)
            context.update(result=draft.result, body=draft.body, problem=draft.problem, signature=draft.signature,
                           other_invoices=draft.other_invoices, needs_confirmation=draft.needs_confirmation)
        except SourceError as e:
            context['problem'] = f'Nie można odczytać arkusza: {e}'
    return render(request, 'billing/subiekt_invoice.html', context)


@require_POST
def subiekt_invoice_create(request, month, key):
    month = _parse_month(month)
    client = get_object_or_404(Client, key=key)
    preview = reverse('subiekt_invoice', args=[month.key, client.key])
    try:
        [draft] = _drafts([client], month)
    except SourceError as e:
        messages.error(request, f'Nie utworzono faktury. Nie można odczytać arkusza: {e}')
        return redirect(preview)
    results = _create(draft, month, request.POST.get('signature'), request.POST.get('confirm_subscription'))
    _add_messages(request, results)
    if results[0][0] == messages.ERROR:
        return redirect(preview)
    return redirect(_month_page(month, client))


def subiekt_batch(request, month):
    """Preview of the invoices of all active clients. Nothing is created here."""
    month = _parse_month(month)
    context = {'month': month, 'drafts': [], 'problem': None, 'connection': subiekt.connection()}
    if context['connection'] is not None:
        try:
            context['drafts'] = _drafts(list(Client.objects.filter(active=True)), month)
        except SourceError as e:
            context['problem'] = f'Nie można odczytać arkusza: {e}'
    return render(request, 'billing/subiekt_batch.html', context)


@require_POST
def subiekt_batch_create(request, month):
    """Create the ticked invoices, one after another."""
    month = _parse_month(month)
    keys = request.POST.getlist('create')
    clients = list(Client.objects.filter(active=True, key__in=keys))
    if not clients:
        messages.error(request, 'Nie zaznaczono żadnego klienta.')
        return redirect('subiekt_batch', month=month.key)
    try:
        drafts = _drafts(clients, month)
    except SourceError as e:
        messages.error(request, f'Nie utworzono faktur. Nie można odczytać arkusza: {e}')
        return redirect('subiekt_batch', month=month.key)
    for draft in drafts:
        key = draft.client.key
        _add_messages(request, _create(draft, month, request.POST.get(f'sig_{key}'), request.POST.get(f'confirm_{key}')))
    return redirect('month', month=month.key)


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


# --- Gmail: connection (step 3) -------------------------------------------------------------------

def gmail_page(request):
    """The connection status."""
    context = {'connection': gmail.connection(), 'from_address': settings.EMAIL_FROM,
               'test_recipient': settings.EMAIL_TEST_RECIPIENT}
    return render(request, 'billing/gmail.html', context)


def gmail_connect(request):
    """Start the OAuth login at Google (authorization code with PKCE)."""
    try:
        target = urlsplit(gmail.redirect_uri())
        if request.get_host() != target.netloc:
            # The session cookie must come back with the callback, so start on the same host.
            return redirect(f'{target.scheme}://{target.netloc}{request.path}')
        verifier, challenge = subiekt.new_pkce()
        state = secrets.token_urlsafe(32)
        url = gmail.authorize_url(state, challenge)
    except gmail.GmailError as e:
        messages.error(request, f'Nie można połączyć z Gmail: {e}')
        return redirect('gmail')
    request.session['gmail_oauth'] = {'state': state, 'verifier': verifier}
    return redirect(url)


def gmail_callback(request):
    """Google sends the browser back here after the login and the consent."""
    pending = request.session.pop('gmail_oauth', None)
    if request.GET.get('error'):
        messages.error(request, f'Google nie udzielił dostępu: {request.GET["error"]}')
    elif not pending or not secrets.compare_digest(pending['state'], request.GET.get('state', '')):
        messages.error(request, 'Nieprawidłowy lub wygasły parametr state. Połącz ponownie.')
    elif not request.GET.get('code'):
        messages.error(request, 'Google nie przekazał kodu autoryzacji. Połącz ponownie.')
    else:
        try:
            conn = gmail.exchange_code(request.GET['code'], pending['verifier'])
            messages.success(request, f'Połączono z Gmail{f" jako {conn.account}" if conn.account else ""}.')
        except gmail.GmailError as e:
            messages.error(request, f'Nie można dokończyć połączenia z Gmail: {e}')
    return redirect('gmail')


@require_POST
def gmail_disconnect(request):
    gmail.disconnect()
    messages.success(request, 'Rozłączono z Gmail. Tokeny zostały usunięte z bazy aplikacji.')
    return redirect('gmail')


# --- Email: the invoice and the report (step 3) -----------------------------------------------------

class EmailRow:
    """One planned email: the client, the invoice, the recipients, and what stops it."""

    def __init__(self, client, result, invoice, last_send, month, test):
        self.client, self.result, self.invoice, self.last_send, self.test = client, result, invoice, last_send, test
        self.recipients = client.email_list
        self.problem = None
        if test:
            self.number = (invoice.document_number if invoice and invoice.document_number
                           else settings.EMAIL_TEST_INVOICE_NUMBER)
            self.send_to = [settings.EMAIL_TEST_RECIPIENT]
        else:
            self.number = invoice.document_number if invoice else ''
            self.send_to = self.recipients
        self.subject, self.text = mailing.subject_and_text(self.number, month, result.work_month, client, test)
        self.invoice_filename = mailing.invoice_filename(self.number or 'brak')
        self.report_filename = mailing.report_filename(result.work_month)

    @property
    def can_send(self):
        return self.problem is None


def _test_mode():
    return bool(settings.EMAIL_TEST_RECIPIENT)


def _email_setup_problem():
    """A problem that stops all emails, or None."""
    if not settings.EMAIL_FROM:
        return 'brak adresu nadawcy (EMAIL_FROM)'
    if gmail.connection() is None:
        return 'Gmail nie jest połączony'
    if _test_mode() and not settings.EMAIL_TEST_INVOICE_NUMBER:
        return 'tryb testowy: brak numeru faktury testowej (EMAIL_TEST_INVOICE_NUMBER)'
    return None


def _test_invoice_id():
    """Test mode: the Subiekt ID of the test invoice (an invoice already in KSeF), found by its number."""
    try:
        return subiekt.document_id_by_number(settings.EMAIL_TEST_INVOICE_NUMBER)
    except subiekt.SubiektError as e:
        raise subiekt.SubiektError(f'tryb testowy: faktura testowa {settings.EMAIL_TEST_INVOICE_NUMBER}: {e}') from e


def _email_rows(clients, month):
    """Plan the emails of the clients. Raise SourceError if the sheet cannot be read."""
    test = _test_mode()
    invoices = {i.client_id: i for i in SubiektInvoice.objects.filter(invoice_month=month.key, client__in=clients)}
    sends = {s.client_id: s for s in EmailSend.objects.filter(invoice_month=month.key, client__in=clients,
                                                                test=False).order_by('sent_at')}
    rows = []
    for client, result in _results(clients, month):
        invoice = invoices.get(client.pk)
        row = EmailRow(client, result, invoice, sends.get(client.pk), month, test)
        if test:
            if invoice is None and not result.lines:
                row.problem = 'faktura nie ma pozycji'
        elif invoice is None or not invoice.document_id:
            row.problem = 'brak faktury utworzonej przez aplikację'
        elif not row.recipients:
            row.problem = 'brak adresu „e-mail do faktur” w konfiguracji klienta'
        else:
            try:
                document = subiekt.get_document(invoice.document_id)
            except subiekt.SubiektError as e:
                document, row.problem = None, f'nie można sprawdzić faktury w Subiekcie: {e}'
            if row.problem is None and document is None:
                row.problem = 'faktury nie ma już w Subiekcie'
            elif row.problem is None and not document.get('ksefNumber'):
                row.problem = 'faktura nie jest jeszcze wysłana do KSeF'
        rows.append(row)
    return rows


def email_batch(request, month):
    """Preview of the emails of all active clients. Nothing is sent here."""
    month = _parse_month(month)
    context = {'month': month, 'rows': [], 'problem': _email_setup_problem(), 'test': _test_mode(),
               'test_recipient': settings.EMAIL_TEST_RECIPIENT, 'from_address': settings.EMAIL_FROM}
    if context['test'] and not context['problem']:
        try:
            _test_invoice_id()
        except subiekt.SubiektError as e:
            context['problem'] = str(e)
    try:
        context['rows'] = _email_rows(list(Client.objects.filter(active=True)), month)
    except SourceError as e:
        context['problem'] = f'Nie można odczytać arkusza: {e}'
    return render(request, 'billing/email_batch.html', context)


@require_POST
def email_batch_send(request, month):
    """Send the ticked emails, one after another."""
    month = _parse_month(month)
    keys = request.POST.getlist('send')
    clients = list(Client.objects.filter(active=True, key__in=keys))
    if not clients:
        messages.error(request, 'Nie zaznaczono żadnego klienta.')
        return redirect('email_batch', month=month.key)
    problem = _email_setup_problem()
    if problem:
        messages.error(request, f'Nie wysłano e-maili: {problem}')
        return redirect('email_batch', month=month.key)
    try:
        rows = _email_rows(clients, month)
    except SourceError as e:
        messages.error(request, f'Nie wysłano e-maili. Nie można odczytać arkusza: {e}')
        return redirect('email_batch', month=month.key)
    test_pdf = None
    if _test_mode():
        try:
            test_pdf = subiekt.document_pdf(_test_invoice_id())   # once for the whole batch
        except subiekt.SubiektError as e:
            messages.error(request, f'Nie wysłano e-maili: {e}')
            return redirect('email_batch', month=month.key)

    for row in rows:
        name = row.client.name
        if row.problem:
            messages.error(request, f'{name}: nie wysłano: {row.problem}')
            continue
        if row.last_send and not row.test and not request.POST.get(f'resend_{row.client.key}'):
            messages.error(request, f'{name}: nie wysłano: e-mail był już wysłany '
                                    f'{timezone.localtime(row.last_send.sent_at):%d.%m %H:%M}. Potwierdź ponowną wysyłkę.')
            continue
        try:
            invoice_pdf = test_pdf if row.test else subiekt.document_pdf(row.invoice.document_id)
            attachments = [(row.invoice_filename, invoice_pdf),
                           (row.report_filename, _report_pdf(request, row.client, row.result))]
            message = mailing.build_message(settings.EMAIL_FROM, row.send_to, row.subject, row.text, attachments)
            message_id = gmail.send(message)
        except gmail.NotConnected as e:
            messages.error(request, f'{name}: nie wysłano: {e}. Pozostałe e-maile też nie zostały wysłane.')
            break
        except (gmail.GmailError, subiekt.SubiektError) as e:
            logger.warning('Email %s %s not sent: %s', row.client.key, month.key, e)
            messages.error(request, f'{name}: nie wysłano: {e}')
            continue
        EmailSend.objects.create(client=row.client, invoice_month=month.key, invoice=row.invoice,
                                 invoice_number=row.number, recipients=', '.join(row.send_to), subject=row.subject,
                                 message_id=message_id, test=row.test, sent_at=timezone.now())
        messages.success(request, f'{name}: wysłano e-mail do {", ".join(row.send_to)}{" (test)" if row.test else ""}.')
    return redirect('month', month=month.key)
