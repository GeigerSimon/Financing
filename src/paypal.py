from __future__ import annotations

from datetime import datetime
from pathlib import Path

import csv

from src.base import Transaction, parse_amount

PAYPAL_TRANSFER_DESCRIPTIONS = {
    "bankgutschrift auf paypal-konto",
    "von nutzer eingeleitete abbuchung",
}


def parse_paypal_transactions(csv_path: Path) -> list[Transaction]:
    """Read detailed PayPal transactions, excluding balance-transfer counterparts."""
    transactions: list[Transaction] = []
    required_fields = {
        "Datum",
        "Beschreibung",
        "Brutto",
        "Währung",
        "Transaktionscode",
        "Zugehöriger Transaktionscode",
    }

    with csv_path.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or not required_fields.issubset(reader.fieldnames):
            missing = required_fields.difference(reader.fieldnames or ())
            raise ValueError(f"PayPal CSV is missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            description = (row.get("Beschreibung") or "").strip()
            if description.lower() in PAYPAL_TRANSFER_DESCRIPTIONS:
                continue
            if (row.get("Währung") or "").strip().upper() != "EUR":
                raise ValueError(f"Unsupported PayPal currency in {csv_path}: {row.get('Währung')}")

            date = (row.get("Datum") or "").strip()
            try:
                date = _parse_paypal_date(date)
                amount = parse_amount((row.get("Brutto") or "").strip())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid PayPal transaction in {csv_path}: {row}") from exc

            payee = (row.get("Name") or "").strip() or (row.get("Absender E-Mail-Adresse") or "").strip()
            purpose_parts = [description]
            for field in (
                "Rechnungsnummer",
                "Transaktionscode",
                "Zugehöriger Transaktionscode",
            ):
                value = (row.get(field) or "").strip()
                if value:
                    purpose_parts.append(f"{field}: {value}")
            transactions.append(
                Transaction(
                    date=date,
                    booking_type="PayPal",
                    payee=payee,
                    purpose=" ".join(purpose_parts),
                    amount_eur=amount,
                )
            )
    return transactions


def load_paypal_transactions(paypal_dir: Path) -> list[Transaction]:
    if paypal_dir.exists() and not paypal_dir.is_dir():
        raise NotADirectoryError(f"PayPal path is not a directory: {paypal_dir}")
    paypal_paths = sorted(paypal_dir.iterdir()) if paypal_dir.exists() else []
    transactions: list[Transaction] = []
    for paypal_path in paypal_paths:
        if paypal_path.is_file() and paypal_path.suffix.lower() == ".csv":
            transactions.extend(parse_paypal_transactions(paypal_path))
    return transactions


def _parse_paypal_date(value: str) -> str:
    return datetime.strptime(value, "%d.%m.%Y").strftime("%d.%m.%Y")


def _is_paypal_bank_transaction(transaction: Transaction) -> bool:
    searchable = f"{transaction.payee} {transaction.purpose}".lower()
    return "paypal" in searchable


def merge_paypal_transactions(
    bank_transactions: list[Transaction],
    paypal_transactions: list[Transaction],
) -> list[Transaction]:
    """Replace bank-level PayPal entries with the detailed PayPal export."""
    bank_rows = [
        transaction
        for transaction in bank_transactions
        if not _is_paypal_bank_transaction(transaction)
    ]
    merged = [*bank_rows, *paypal_transactions]
    return sorted(merged, key=lambda transaction: _sort_date(transaction.date))


def _sort_date(value: str) -> tuple[int, int, int]:
    day, month, year = (int(part) for part in value.split("."))
    return year, month, day
