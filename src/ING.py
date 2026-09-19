from __future__ import annotations

import re
from pathlib import Path

from src.base import Bank, Transaction, parse_amount

DATE_RE = re.compile(r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+")
AMOUNT_RE = re.compile(r"(?P<amount>[+-]?\s*[\d.]+,\d{2})\s*$")
TRANSACTION_TYPES = (
    "Lastschrift",
    "Gehalt/Rente",
    "Dauerauftrag/Terminueberw.",
    "Kapitalertragsteuer",
    "Überweisung",
    "Ueberweisung",
    "Gutschrift",
    "Echtzeitueberweisung",
    "Echtzeitüberweisung",
)


class ING(Bank):
    key = "ing"
    label = "ING (PDF statements)"
    file_pattern = "*.pdf"

    def load(self, input_dir: Path) -> list[Transaction]:
        transactions: list[Transaction] = []
        for pdf_path in self.statement_paths(input_dir):
            transactions.extend(parse_transactions(extract_text(pdf_path)))
        return transactions


def extract_text(pdf_path: Path) -> str:
    """Extract text from a text-based PDF without sending it anywhere."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "The PDF reader dependency is missing. Install it with: pip install -r requirements.txt"
        ) from exc

    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_transactions(text: str) -> list[Transaction]:
    """Parse ING's multi-line transaction layout from extracted PDF text."""
    transactions: list[Transaction] = []
    current: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue

        if current is not None and _is_transaction_boundary(line):
            transactions.append(_make_transaction(current))
            current = None
            if line.startswith("Neuer Saldo"):
                continue

        date_match = DATE_RE.match(line)
        amount_match = AMOUNT_RE.search(line)
        type_match = None
        if date_match and amount_match:
            remainder = line[date_match.end() : amount_match.start()].strip()
            for transaction_type in TRANSACTION_TYPES:
                if remainder.startswith(transaction_type):
                    type_match = transaction_type
                    break

        if date_match and amount_match and type_match:
            if current is not None:
                transactions.append(_make_transaction(current))
            remainder = line[date_match.end() : amount_match.start()].strip()
            payee = remainder[len(type_match) :].strip()
            current = {
                "date": date_match.group("date"),
                "booking_type": type_match,
                "payee": payee,
                "amount": amount_match.group("amount"),
                "purpose_lines": "",
            }
            continue

        if current is not None and not _is_statement_noise(line):
            continuation = line[date_match.end() :] if date_match else line
            current["purpose_lines"] += f" {continuation}"

    if current is not None:
        transactions.append(_make_transaction(current))
    return transactions


def _is_statement_noise(line: str) -> bool:
    noise = (
        "Girokonto Nummer",
        "Kontoauszug ",
        "Buchung Buchung",
        "Valuta",
        "Datum ",
        "Seite ",
        "Neuer Saldo",
        "Alter Saldo",
        "IBAN ",
        "BIC ",
        "Kunden-Information",
        "Bitte beachten",
        "Wir wünschen",
        "Wir danken",
        "Ihre ING",
        "ING-DiBa AG",
    )
    return line.startswith(noise) or line.startswith("34GIRO")


def _is_transaction_boundary(line: str) -> bool:
    """Avoid attaching page headers/footers to the preceding transaction."""
    return line.startswith(
        (
            "Girokonto Nummer",
            "34GIRO",
            "Herrn",
            "Neuer Saldo",
        )
    )


def _make_transaction(raw: dict[str, str]) -> Transaction:
    purpose = raw["purpose_lines"].strip()
    return Transaction(
        date=raw["date"],
        booking_type=raw["booking_type"],
        payee=raw["payee"],
        purpose=purpose,
        amount_eur=parse_amount(raw["amount"]),
    )
