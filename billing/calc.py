"""Invoice calculation for one client and one invoice month.

Pure Python, no Django imports. The invoice for month M contains the subscription for M,
the overtime of M-1 and each expense of M-1 as a separate line.

A client can settle the overtime every N months (N divides 12, the periods start in January). Then
the overtime of the whole period is on the invoice for the month after the period ends, and the
hours of the period offset each other.

Demo assumptions: prices are net, VAT is 23 % on every line (expenses too).
"""

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

VAT_RATE = Decimal('0.23')
CENT = Decimal('0.01')

MONTH_NAMES = [
    'styczeń', 'luty', 'marzec', 'kwiecień', 'maj', 'czerwiec',
    'lipiec', 'sierpień', 'wrzesień', 'październik', 'listopad', 'grudzień',
]

_MONTH_RE = re.compile(r'^(\d{4})-(\d{2})$')

KZP_VAT_ONLY = 'KZP - tylko VAT'
KZP_FULL = 'KZP - pełna kwota'
_KZP_START = re.compile(r'^KZP\b', re.IGNORECASE)
_KZP_ANYWHERE = re.compile(r'\bKZP\b', re.IGNORECASE)
_KZP_VAT_ONLY = re.compile(r'\b(tylko|jedynie)\s+VAT\b', re.IGNORECASE)


def money(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, order=True)
class Month:
    year: int
    month: int

    @classmethod
    def parse(cls, text):
        """Parse "YYYY-MM". Raise ValueError for bad input."""
        match = _MONTH_RE.match(text)
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise ValueError(f'Invalid month: {text!r}')
        return cls(int(match.group(1)), int(match.group(2)))

    @property
    def key(self):
        return f'{self.year:04d}-{self.month:02d}'

    @property
    def label(self):
        return f'{MONTH_NAMES[self.month - 1]} {self.year}'

    def previous(self):
        if self.month == 1:
            return Month(self.year - 1, 12)
        return Month(self.year, self.month - 1)

    def next(self):
        if self.month == 12:
            return Month(self.year + 1, 1)
        return Month(self.year, self.month + 1)

    def __str__(self):
        return self.key


@dataclass
class Entry:
    description: str
    hours: Decimal | None = None      # valid hours, None for an expense or an invalid value
    raw_hours: object = None          # the value as it is in the source
    expense: Decimal | None = None
    warning: str | None = None
    cell: str = ''                    # A1 reference in the sheet, empty for the demo file
    kzp: str | None = None            # KZP_VAT_ONLY or KZP_FULL for a KZP row with an amount
    cost: bool = False                # a cost row (KOSZT column or KZP): not in the hours report


@dataclass
class Line:
    name: str
    quantity: Decimal
    unit: str
    unit_net: Decimal
    paid: Decimal = Decimal('0.00')   # the part already paid ("dodaj płatność" in Subiekt)
    note: str = ''                    # shown next to the name, for example the KZP type

    @property
    def net(self):
        return money(self.quantity * self.unit_net)

    @property
    def vat(self):
        return money(self.net * VAT_RATE)

    @property
    def gross(self):
        return self.net + self.vat


@dataclass
class PeriodRow:
    month: Month
    hours: Decimal
    included: Decimal


@dataclass
class Result:
    invoice_month: Month
    work_month: Month
    entries: list[Entry]
    hours_total: Decimal
    included_hours: Decimal
    overtime_hours: Decimal               # the overtime billed on this invoice
    lines: list[Line]
    warnings: list[str] = field(default_factory=list)
    period: int = 1                       # the overtime settlement period in months
    period_rows: list[PeriodRow] = field(default_factory=list)   # first month of the period .. work month

    @property
    def settlement(self):
        """True if this invoice settles the overtime of the period."""
        return self.work_month.month % self.period == 0

    @property
    def hour_entries(self):
        """The entries of the hours report: all except the costs (those are invoice lines)."""
        return [entry for entry in self.entries if not entry.cost]

    @property
    def period_hours(self):
        return sum((row.hours for row in self.period_rows), Decimal('0'))

    @property
    def period_included_so_far(self):
        return sum((row.included for row in self.period_rows), Decimal('0'))

    @property
    def period_included(self):
        return self.included_hours * self.period

    @property
    def period_balance(self):
        """Included hours of the whole period minus the hours so far (negative: overtime so far)."""
        return self.period_included - self.period_hours

    @property
    def period_excess(self):
        """Hours above the included hours of the whole period so far (0 if none)."""
        return max(Decimal('0'), -self.period_balance)

    @property
    def period_first(self):
        return self.period_rows[0].month

    @property
    def period_last(self):
        first = self.period_first
        return Month(first.year, first.month + self.period - 1)

    @property
    def settlement_invoice_month(self):
        return self.period_last.next()

    @property
    def net(self):
        return sum((line.net for line in self.lines), Decimal('0.00'))

    @property
    def vat(self):
        return sum((line.vat for line in self.lines), Decimal('0.00'))

    @property
    def gross(self):
        return sum((line.gross for line in self.lines), Decimal('0.00'))

    @property
    def paid(self):
        return sum((line.paid for line in self.lines), Decimal('0.00'))

    @property
    def to_pay(self):
        return self.gross - self.paid


def _to_decimal(value):
    """Return a Decimal for a number or a numeric string, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip().replace(',', '.'))
        except InvalidOperation:
            return None
    return None


def _warning(entry, text):
    if entry.cell:
        text = f'{text} ({entry.cell})'
    entry.warning = f'{entry.warning}; {text}' if entry.warning else text


def parse_entry(raw):
    """Turn one source entry (a dict) into an Entry, with a warning if the entry is unclear."""
    description = str(raw.get('description') or '').strip()
    raw_hours = raw.get('hours')
    raw_expense = raw.get('expense')
    entry = Entry(description=description, raw_hours=raw_hours, cell=raw.get('cell') or '')

    # KZP (PLAN.md 4.2): the description starts with "KZP", the net amount is in the KOSZT column.
    if _KZP_START.match(description):
        entry.cost = True
        amount = _to_decimal(raw_expense) if raw_expense is not None else None
        if raw_expense is None:
            _warning(entry, f'KZP bez kwoty w kolumnie KOSZT: „{description}”')
        elif amount is None or amount < 0:
            _warning(entry, f'Nieprawidłowa kwota KZP „{raw_expense}”: „{description}”')
        else:
            entry.expense = money(amount)
            entry.kzp = KZP_VAT_ONLY if _KZP_VAT_ONLY.search(description) else KZP_FULL
        return entry
    if _KZP_ANYWHERE.search(description):
        _warning(entry, f'Czy to KZP? Opis nie zaczyna się od „KZP”: „{description}”')

    if raw_expense is not None:
        entry.cost = True
        amount = _to_decimal(raw_expense)
        if amount is None or amount < 0:
            _warning(entry, f'Nieprawidłowa kwota kosztu „{raw_expense}”: „{description}”')
        else:
            entry.expense = money(amount)
        return entry

    if raw_hours is None or raw_hours == '':
        _warning(entry, f'Brak liczby godzin: „{description}”')
        return entry

    hours = _to_decimal(raw_hours)
    if hours is None or hours < 0:
        _warning(entry, f'Nieprawidłowa liczba godzin „{raw_hours}”: „{description}”')
    else:
        entry.hours = hours
    return entry


def period_months(work_month, period):
    """The months of the settlement period of `work_month`, from its first month to `work_month`."""
    first = (work_month.month - 1) // period * period + 1
    return [Month(work_month.year, m) for m in range(first, work_month.month + 1)]


def _valid_hours(raw_entries):
    entries = [parse_entry(raw) for raw in raw_entries]
    return sum((e.hours for e in entries if e.hours is not None), Decimal('0'))


def calculate(client, invoice_month, raw_entries, all_months=None):
    """Calculate the invoice of `client` for `invoice_month`.

    `client` needs: subscription_net, included_hours, overtime_rate_net, overtime_period_months,
    nip.
    `raw_entries` are the source entries of the work month (invoice_month - 1).
    `all_months` maps "YYYY-MM" to the source entries of the client. The calculation uses it only
    for the earlier months of a settlement period longer than 1 month.
    """
    work_month = invoice_month.previous()
    period = int(client.overtime_period_months)
    if 12 % period:
        raise ValueError(f'Invalid overtime period: {period}')
    entries = [parse_entry(raw) for raw in raw_entries]
    warnings = []

    # The invoice takes the buyer's name and address from Subiekt; the app needs only the NIP to find it.
    if not client.nip:
        warnings.append('Brak NIP w konfiguracji klienta')
    if not entries:
        warnings.append(f'Brak wpisów za {work_month.label}')
    warnings.extend(entry.warning for entry in entries if entry.warning)

    hours_total = sum((e.hours for e in entries if e.hours is not None), Decimal('0'))
    included = Decimal(client.included_hours)
    rate = Decimal(client.overtime_rate_net)

    period_rows = [
        PeriodRow(
            month=m,
            hours=hours_total if m == work_month else _valid_hours((all_months or {}).get(m.key, [])),
            included=included,
        )
        for m in period_months(work_month, period)
    ]
    period_hours = sum((row.hours for row in period_rows), Decimal('0'))

    # A rate of 0 zł means: the client pays only the subscription, the hours are never billed.
    # The overtime is billed only when the work month closes the settlement period.
    if rate > 0 and work_month.month % period == 0:
        overtime = max(Decimal('0'), period_hours - included * period)
    else:
        overtime = Decimal('0')

    # A client with a subscription of 0 zł pays only for the hours: no subscription line, and the
    # hours line gets a different name.
    subscription = Decimal(client.subscription_net)
    lines = []
    if subscription > 0:
        lines.append(Line(
            name='usługa prawna - abonament',
            quantity=Decimal('1'),
            unit='szt',
            unit_net=subscription,
        ))
    if overtime > 0:
        lines.append(Line(
            name='usługa prawna - nadgodziny' if subscription > 0 else 'usługa prawna',
            quantity=overtime,
            unit='godzina',
            unit_net=rate,
        ))
    for entry in entries:
        if entry.kzp is not None:
            lines.append(Line(
                name='usługa prawna - koszty zastępstwa',
                quantity=Decimal('1'),
                unit='szt',
                unit_net=entry.expense,
                # "tylko VAT": the net amount is marked as paid, only the VAT is to be paid.
                paid=entry.expense if entry.kzp == KZP_VAT_ONLY else Decimal('0.00'),
                note=entry.kzp,
            ))
        elif entry.expense is not None:
            # A cost: the sheet amount is net, VAT 23 % (decision 2026-11-16). The description from the
            # sheet is shown next to the line, so the operator can see which cost it is.
            lines.append(Line(
                name='usługa prawna - opłata',
                quantity=Decimal('1'),
                unit='szt',
                unit_net=entry.expense,
                note=entry.description,
            ))

    return Result(
        invoice_month=invoice_month,
        work_month=work_month,
        entries=entries,
        hours_total=hours_total,
        included_hours=included,
        overtime_hours=overtime,
        lines=lines,
        warnings=warnings,
        period=period,
        period_rows=period_rows,
    )
