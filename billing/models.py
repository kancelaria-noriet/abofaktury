from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Client(models.Model):
    """A subscription client and the terms that the invoice calculation uses."""

    key = models.SlugField(
        'klucz',
        unique=True,
        help_text='Identyfikator klienta w źródle danych (arkuszu).',
    )
    name = models.CharField('nazwa', max_length=200)
    sheet_tab = models.CharField(
        'zakładka w arkuszu',
        max_length=100,
        blank=True,
        help_text='Nazwa zakładki z godzinami. {rok} zastępuje rok, np. „Klient_{rok}”. '
                  'Faktura za styczeń czyta zakładkę poprzedniego roku.',
    )
    nip = models.CharField('NIP', max_length=20, blank=True)
    address = models.TextField('adres', blank=True)
    subscription_net = models.DecimalField(
        'abonament netto (PLN)',
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))],
    )
    included_hours = models.DecimalField(
        'godziny w abonamencie',
        max_digits=5,
        decimal_places=1,
        validators=[MinValueValidator(Decimal('0'))],
    )
    overtime_rate_net = models.DecimalField(
        'stawka za nadgodzinę netto (PLN)',
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0'))],
    )
    overtime_period_months = models.PositiveSmallIntegerField(
        'rozliczanie nadgodzin',
        choices=[
            (1, 'co miesiąc'),
            (2, 'co 2 miesiące'),
            (3, 'co 3 miesiące'),
            (4, 'co 4 miesiące'),
            (6, 'co 6 miesięcy'),
            (12, 'co 12 miesięcy'),
        ],
        default=1,
        help_text='Okresy liczone od stycznia (np. co 6 miesięcy: styczeń-czerwiec i lipiec-grudzień). '
                  'Nadgodziny z okresu są na fakturze za miesiąc po jego końcu. '
                  'Abonament i raport godzin są co miesiąc.',
    )
    invoice_emails = models.CharField(
        'e-mail do faktur',
        max_length=500,
        blank=True,
        help_text='Adresy, na które aplikacja wysyła fakturę i raport. Kilka adresów oddziel przecinkami.',
    )
    send_hour_reports = models.BooleanField(
        'wysyłaj raporty godzin w PDF',
        default=True,
        help_text='Bez zaznaczenia e-mail z fakturą nigdy nie ma raportu godzin. '
                  'Pusty raport (brak godzin w miesiącu) nie jest wysyłany nigdy.',
    )
    active = models.BooleanField('aktywny', default=True)

    class Meta:
        ordering = ['name']
        verbose_name = 'klient'
        verbose_name_plural = 'klienci'

    def __str__(self):
        return self.name

    @property
    def email_list(self):
        return [address.strip() for address in self.invoice_emails.replace(';', ',').split(',') if address.strip()]


class SubiektConnection(models.Model):
    """The OAuth tokens for the Subiekt 123 API. There is at most one row (pk=1).

    The tokens are secrets: this model is not in the admin.
    """

    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    account = models.CharField('konto', max_length=200, blank=True)
    connected_at = models.DateTimeField('połączono')

    class Meta:
        verbose_name = 'połączenie z Subiektem'


class SubiektInvoice(models.Model):
    """An invoice that the app created in Subiekt 123: one for each client and invoice month."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='subiekt_invoices')
    invoice_month = models.CharField('miesiąc faktury', max_length=7)   # YYYY-MM
    document_id = models.CharField('ID dokumentu w Subiekcie', max_length=64)
    document_number = models.CharField('numer', max_length=50, blank=True)
    net = models.DecimalField('netto (aplikacja)', max_digits=12, decimal_places=2)
    gross = models.DecimalField('brutto (aplikacja)', max_digits=12, decimal_places=2)
    created_at = models.DateTimeField('utworzono')

    class Meta:
        verbose_name = 'faktura w Subiekcie'
        verbose_name_plural = 'faktury w Subiekcie'
        constraints = [
            models.UniqueConstraint(fields=['client', 'invoice_month'], name='one_invoice_per_client_month'),
        ]

    def __str__(self):
        return f'{self.document_number or self.document_id} ({self.client}, {self.invoice_month})'


class GmailConnection(models.Model):
    """The OAuth tokens for the Gmail API (scope gmail.send). At most one row (pk=1); not in the admin."""

    access_token = models.TextField()
    refresh_token = models.TextField()
    expires_at = models.DateTimeField()
    account = models.CharField('konto', max_length=200, blank=True)
    connected_at = models.DateTimeField('połączono')


class EmailSend(models.Model):
    """One sent email with the invoice and the report (also the test emails)."""

    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='email_sends')
    invoice_month = models.CharField('miesiąc faktury', max_length=7)   # YYYY-MM
    invoice = models.ForeignKey(SubiektInvoice, on_delete=models.SET_NULL, null=True, blank=True)
    invoice_number = models.CharField('numer faktury', max_length=50)
    recipients = models.CharField('odbiorcy', max_length=500)
    subject = models.CharField('temat', max_length=300)
    message_id = models.CharField('ID wiadomości Gmail', max_length=100, blank=True)
    test = models.BooleanField('test', default=False)
    sent_at = models.DateTimeField('wysłano')

    class Meta:
        ordering = ['-sent_at']
        verbose_name = 'wysłany e-mail'
        verbose_name_plural = 'wysłane e-maile'
