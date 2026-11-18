from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase

from billing.calc import Month, calculate, parse_entry


def make_client(**overrides):
    values = dict(
        subscription_net=Decimal('2000.00'),
        included_hours=Decimal('5.0'),
        overtime_rate_net=Decimal('400.00'),
        overtime_period_months=1,
        nip='1111111111',
        address='Testowa 1\n00-001 Warszawa',
    )
    values.update(overrides)
    return SimpleNamespace(**values)


OCT = Month(2026, 10)


class MonthTests(TestCase):
    def test_parse_and_key(self):
        self.assertEqual(Month.parse('2026-10'), OCT)
        self.assertEqual(OCT.key, '2026-10')
        self.assertEqual(OCT.label, 'październik 2026')

    def test_parse_rejects_bad_input(self):
        for text in ['2026-13', '2026-00', '2026-1', 'abc', '']:
            with self.assertRaises(ValueError):
                Month.parse(text)

    def test_previous_and_next_cross_the_year(self):
        self.assertEqual(Month(2026, 1).previous(), Month(2025, 12))
        self.assertEqual(Month(2025, 12).next(), Month(2026, 1))


class ParseEntryTests(TestCase):
    def test_valid_hours(self):
        self.assertEqual(parse_entry({'description': 'a', 'hours': 1.5}).hours, Decimal('1.5'))
        self.assertEqual(parse_entry({'description': 'a', 'hours': '2,5'}).hours, Decimal('2.5'))

    def test_zero_hours_is_valid(self):
        entry = parse_entry({'description': 'rabat', 'hours': 0})
        self.assertEqual(entry.hours, Decimal('0'))
        self.assertIsNone(entry.warning)

    def test_missing_hours_gives_warning(self):
        entry = parse_entry({'description': 'spotkanie', 'hours': None})
        self.assertIsNone(entry.hours)
        self.assertIn('Brak liczby godzin', entry.warning)

    def test_invalid_hours_give_warning(self):
        for raw in ['3?', -1, True]:
            entry = parse_entry({'description': 'x', 'hours': raw})
            self.assertIsNone(entry.hours)
            self.assertIn('Nieprawidłowa liczba godzin', entry.warning)

    def test_expense(self):
        entry = parse_entry({'description': 'opłata', 'hours': None, 'expense': '113.16'})
        self.assertEqual(entry.expense, Decimal('113.16'))
        self.assertIsNone(entry.warning)

    def test_invalid_expense_gives_warning(self):
        entry = parse_entry({'description': 'opłata', 'expense': 'sto'})
        self.assertIsNone(entry.expense)
        self.assertIn('Nieprawidłowa kwota kosztu', entry.warning)


class CalculateTests(TestCase):
    def test_no_overtime_below_included_hours(self):
        result = calculate(make_client(), OCT, [{'description': 'a', 'hours': 3}])
        self.assertEqual(result.work_month, Month(2026, 9))
        self.assertEqual(result.hours_total, Decimal('3'))
        self.assertEqual(result.overtime_hours, Decimal('0'))
        self.assertEqual(len(result.lines), 1)
        self.assertEqual(result.net, Decimal('2000.00'))
        self.assertEqual(result.vat, Decimal('460.00'))
        self.assertEqual(result.gross, Decimal('2460.00'))
        self.assertEqual(result.warnings, [])

    def test_overtime_line(self):
        result = calculate(make_client(), OCT, [
            {'description': 'a', 'hours': 4},
            {'description': 'b', 'hours': 2.5},
        ])
        self.assertEqual(result.overtime_hours, Decimal('1.5'))
        overtime = result.lines[1]
        self.assertEqual(overtime.unit, 'godzina')
        self.assertEqual(overtime.net, Decimal('600.00'))
        self.assertEqual(overtime.name, 'usługa prawna - nadgodziny')
        self.assertEqual(result.lines[0].name, 'usługa prawna - abonament')
        self.assertEqual(result.net, Decimal('2600.00'))
        self.assertEqual(result.gross, Decimal('3198.00'))

    def test_no_subscription_gives_only_the_hours_line(self):
        client = make_client(subscription_net=Decimal('0.00'), included_hours=Decimal('0'))
        result = calculate(client, OCT, [{'description': 'a', 'hours': 3}])
        self.assertEqual(len(result.lines), 1)
        line = result.lines[0]
        self.assertEqual(line.name, 'usługa prawna')
        self.assertEqual(line.quantity, Decimal('3'))
        self.assertEqual(result.net, Decimal('1200.00'))

    def test_rate_zero_bills_only_the_subscription(self):
        client = make_client(subscription_net=Decimal('750.00'), included_hours=Decimal('0'),
                             overtime_rate_net=Decimal('0.00'))
        result = calculate(client, OCT, [{'description': 'a', 'hours': 3}])
        self.assertEqual(result.hours_total, Decimal('3'))
        self.assertEqual(result.overtime_hours, Decimal('0'))
        self.assertEqual([line.name for line in result.lines], ['usługa prawna - abonament'])
        self.assertEqual(result.net, Decimal('750.00'))

    def test_period_of_6_months_bills_no_overtime_inside_the_period(self):
        client = make_client(included_hours=Decimal('18'), overtime_period_months=6)
        months = {'2026-07': [{'description': 'a', 'hours': 30}]}
        result = calculate(client, OCT, [{'description': 'b', 'hours': 25}], months)  # work: Sep
        self.assertFalse(result.settlement)
        self.assertEqual(result.overtime_hours, Decimal('0'))
        self.assertEqual([line.name for line in result.lines], ['usługa prawna - abonament'])
        self.assertEqual([row.month for row in result.period_rows],
                         [Month(2026, 7), Month(2026, 8), Month(2026, 9)])
        self.assertEqual(result.period_hours, Decimal('55'))
        self.assertEqual(result.period_included_so_far, Decimal('54'))
        self.assertEqual(result.period_balance, Decimal('53'))   # 108 - 55
        self.assertEqual(result.settlement_invoice_month, Month(2027, 1))

    def test_period_of_6_months_settles_with_the_invoice_after_the_period(self):
        client = make_client(included_hours=Decimal('18'), overtime_period_months=6,
                             overtime_rate_net=Decimal('300.00'))
        months = {f'2026-{m:02d}': [{'description': 'a', 'hours': 20}] for m in range(7, 12)}
        result = calculate(client, Month(2027, 1), [{'description': 'b', 'hours': 10}], months)
        self.assertTrue(result.settlement)
        self.assertEqual(result.period_hours, Decimal('110'))     # 5 * 20 + 10
        self.assertEqual(result.overtime_hours, Decimal('2'))     # 110 - 6 * 18
        overtime = result.lines[1]
        self.assertEqual(overtime.name, 'usługa prawna - nadgodziny')
        self.assertEqual(overtime.net, Decimal('600.00'))

    def test_period_hours_offset_each_other(self):
        client = make_client(included_hours=Decimal('18'), overtime_period_months=6)
        months = {'2026-01': [{'description': 'a', 'hours': 30}]}   # 12 h over in January
        result = calculate(client, Month(2026, 7), [{'description': 'b', 'hours': 5}], months)
        self.assertEqual(result.period_hours, Decimal('35'))
        self.assertEqual(result.overtime_hours, Decimal('0'))

    def test_no_subscription_and_no_hours_gives_no_lines(self):
        client = make_client(subscription_net=Decimal('0.00'), included_hours=Decimal('0'))
        result = calculate(client, OCT, [])
        self.assertEqual(result.lines, [])
        self.assertEqual(result.net, Decimal('0.00'))

    def test_expense_line_has_vat_23(self):
        result = calculate(make_client(), OCT, [
            {'description': 'opłata kancelaryjna', 'hours': None, 'expense': '20.00'},
        ])
        expense = result.lines[-1]
        self.assertEqual(expense.name, 'usługa prawna - opłata')
        self.assertEqual(expense.note, 'opłata kancelaryjna')
        self.assertEqual(expense.vat, Decimal('4.60'))
        self.assertEqual(expense.gross, Decimal('24.60'))
        self.assertEqual(result.net, Decimal('2020.00'))
        self.assertEqual(result.vat, Decimal('464.60'))

    def test_invalid_hours_count_as_zero_with_warning(self):
        result = calculate(make_client(), OCT, [
            {'description': 'a', 'hours': 6},
            {'description': 'b', 'hours': '3?'},
        ])
        self.assertEqual(result.hours_total, Decimal('6'))
        self.assertEqual(result.overtime_hours, Decimal('1'))
        self.assertEqual(len(result.warnings), 1)

    def test_no_entries_gives_warning(self):
        result = calculate(make_client(), OCT, [])
        self.assertEqual(result.warnings, ['Brak wpisów za wrzesień 2026'])
        self.assertEqual(result.net, Decimal('2000.00'))

    def test_missing_nip_gives_warning(self):
        result = calculate(make_client(nip=''), OCT, [{'description': 'a', 'hours': 1}])
        self.assertEqual(result.warnings, ['Brak NIP w konfiguracji klienta'])

    def test_missing_address_gives_no_warning(self):
        result = calculate(make_client(address=''), OCT, [{'description': 'a', 'hours': 1}])
        self.assertEqual(result.warnings, [])

    def test_rounding_half_up(self):
        client = make_client(subscription_net=Decimal('0.50'), included_hours=Decimal('0'),
                             overtime_rate_net=Decimal('0.00'))
        result = calculate(client, OCT, [])
        self.assertEqual(result.vat, Decimal('0.12'))  # 0.115 rounds half up

    def test_january_invoice_uses_december_of_previous_year(self):
        result = calculate(make_client(), Month(2026, 1), [])
        self.assertEqual(result.work_month, Month(2025, 12))
