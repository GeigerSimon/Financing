from __future__ import annotations

import csv
from pathlib import Path

from src.base import (
    Bank,
    Transaction,
    csv_field,
    decode_csv_text,
    normalize_csv_row,
    parse_amount,
    parse_bank_date,
)

REQUIRED_FIELDS = {
    "Buchungstag",
    "Buchungstext",
    "Verwendungszweck",
    "Betrag",
}
PAYEE_FIELDS = (
    "Beguenstigter/Zahlungspflichtiger",
    "Begünstigter/Zahlungspflichtiger",
)


class Sparkasse(Bank):
    key = "sparkasse"
    label = "Sparkasse (CSV export)"
    file_pattern = "*.csv"

    def load(self, input_dir: Path) -> list[Transaction]:
        transactions: list[Transaction] = []
        for csv_path in self.statement_paths(input_dir):
            transactions.extend(parse_sparkasse_transactions(csv_path))
        return transactions


def parse_sparkasse_transactions(csv_path: Path) -> list[Transaction]:
    """Read Sparkasse account exports (semicolon-separated CSV)."""
    text = decode_csv_text(csv_path)
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"Sparkasse CSV is empty: {csv_path}")

    header_index = 0
    for index, line in enumerate(lines[:15]):
        if "Buchungstag" in line and "Betrag" in line:
            header_index = index
            break
    else:
        raise ValueError(
            f"Sparkasse CSV is missing a Buchungstag/Betrag header: {csv_path}"
        )

    reader = csv.DictReader(lines[header_index:], delimiter=";")
    fieldnames = {(name or "").strip() for name in (reader.fieldnames or [])}
    missing = REQUIRED_FIELDS.difference(fieldnames)
    if missing:
        raise ValueError(
            f"Sparkasse CSV is missing columns: {', '.join(sorted(missing))}"
        )
    if not fieldnames.intersection(PAYEE_FIELDS):
        raise ValueError(
            "Sparkasse CSV is missing column: Beguenstigter/Zahlungspflichtiger"
        )

    transactions: list[Transaction] = []
    for raw_row in reader:
        row = normalize_csv_row(raw_row)
        info = csv_field(row, "Info")
        if info and info.casefold() != "umsatz gebucht":
            continue

        amount_raw = csv_field(row, "Betrag")
        date_raw = csv_field(row, "Buchungstag")
        if not amount_raw or not date_raw:
            continue

        currency = csv_field(row, "Waehrung", "Währung")
        if currency and currency.upper() != "EUR":
            raise ValueError(f"Unsupported Sparkasse currency in {csv_path}: {currency}")

        try:
            date = parse_bank_date(date_raw)
            amount = parse_amount(amount_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Sparkasse transaction in {csv_path}: {row}") from exc

        purpose_parts = [csv_field(row, "Verwendungszweck")]
        mandate = csv_field(row, "Mandatsreferenz")
        if mandate:
            purpose_parts.append(f"Mandat: {mandate}")
        creditor_id = csv_field(row, "Glaeubiger ID", "Gläubiger ID")
        if creditor_id:
            purpose_parts.append(f"Gläubiger-ID: {creditor_id}")

        transactions.append(
            Transaction(
                date=date,
                booking_type=csv_field(row, "Buchungstext"),
                payee=csv_field(row, *PAYEE_FIELDS),
                purpose=" ".join(part for part in purpose_parts if part),
                amount_eur=amount,
            )
        )
    return transactions
