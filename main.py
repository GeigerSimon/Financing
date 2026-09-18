r"""Extract and categorize transactions from ING PDF bank statements and PayPal exports.

Usage:
    python main.py "path\to\statement.pdf"

The category rules are stored locally in categories.json.  When a transaction
does not match a rule, the program asks for a category and keywords, then
reuses that rule for future statements.

PayPal transactions are read from the Paypal subfolder. Bank statement rows
that only represent PayPal funding are replaced by the detailed PayPal rows.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DATE_RE = re.compile(r"^(?P<date>\d{2}\.\d{2}\.\d{4})\s+")
AMOUNT_RE = re.compile(r"(?P<amount>[+-]?\s*[\d.]+,\d{2})\s*$")
MANDATE_RE = re.compile(r"(Mandat:\s*[^\s]+)", re.IGNORECASE)
RELATED_TRANSACTION_CODE_RE = re.compile(
    r"Zugehöriger Transaktionscode:\s*([^\s]+)", re.IGNORECASE
)
PAYPAL_TRANSFER_DESCRIPTIONS = {
    "bankgutschrift auf paypal-konto",
    "von nutzer eingeleitete abbuchung",
}
TRANSACTION_TYPES = (
    "Lastschrift",
    "Gehalt/Rente",
    "Dauerauftrag/Terminueberw.",
    "Kapitalertragsteuer",
    "Überweisung",
    "Ueberweisung",
    "Gutschrift",
    "Echtzeitueberweisung",
	"Echtzeitüberweisung"
)


@dataclass
class Transaction:
    date: str
    booking_type: str
    payee: str
    purpose: str
    amount_eur: float
    category: str = ""


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


def parse_amount(value: str) -> float:
    normalized = value.replace(" ", "").replace(".", "").replace(",", ".")
    return float(normalized)


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


def _parse_paypal_date(value: str) -> str:
    return datetime.strptime(value, "%d.%m.%Y").strftime("%d.%m.%Y")


def _is_paypal_bank_transaction(transaction: Transaction) -> bool:
    searchable = f"{transaction.payee} {transaction.purpose}".lower()
    return "paypal" in searchable


def merge_paypal_transactions(
    bank_transactions: Iterable[Transaction],
    paypal_transactions: Iterable[Transaction],
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


def _default_keywords(category: str, transaction: Transaction) -> list[str]:
    """Return stable identifiers that can recognize recurring direct debits."""
    keywords = []
    
    mandate_match = MANDATE_RE.search(transaction.purpose)
    if mandate_match:
        keywords.append(mandate_match.group(1))
    related_transaction_match = RELATED_TRANSACTION_CODE_RE.search(transaction.purpose)
    if related_transaction_match:
        keywords.append(related_transaction_match.group(1))
    if len(keywords) == 0:
        keywords = [transaction.payee.lower()] if transaction.payee else []
    return keywords


def _category_match_score(transaction: Transaction, keywords: list[str]) -> int:
    """Prefer merchant-specific matches over shared mandate identifiers."""
    searchable = f"{transaction.payee} {transaction.purpose}".lower()
    payee = transaction.payee.lower()
    matches = [keyword for keyword in keywords if keyword.lower() in searchable]
    if not matches:
        return 0
    if any(keyword.lower() in payee for keyword in matches):
        return 2
    return 1


def _print_available_categories(rules: dict[str, list[str]]) -> None:
    """Display categories available for the next interactive assignment."""
    print("\033[2m" + "\n" + "-" * 60 + "\033[0m")
    if not rules:
        print("\033[2mAvailable categories: none yet\033[0m")
        return

    categories = " | ".join(sorted(rules))
    print(f"\033[2mAvailable categories: {categories}\033[0m")


def load_rules(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict) or not all(
        isinstance(category, str) and isinstance(words, list)
        for category, words in data.items()
    ):
        raise ValueError(f"Invalid category file: {path}")
    return {category: [str(word).lower() for word in words] for category, words in data.items()}


def save_rules(path: Path, rules: dict[str, list[str]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(rules, file, ensure_ascii=False, indent=2)
        file.write("\n")


def categorize(
    transactions: list[Transaction],
    rules: dict[str, list[str]],
    interactive: bool,
) -> None:
    for transaction in transactions:
        matching_categories = [
            (category, _category_match_score(transaction, keywords))
            for category, keywords in rules.items()
        ]
        matching_categories = [
            (category, score) for category, score in matching_categories if score > 0
        ]
        transaction.category = (
            max(matching_categories, key=lambda item: item[1])[0]
            if matching_categories
            else ""
        )
        if transaction.category:
            continue

        if not interactive:
            transaction.category = "Uncategorized"
            continue

        _print_available_categories(rules)
        print(f"Uncategorized:")
        print(f"{"  Date:":<15}{transaction.date}")
        print(f"{"  Payee:":<15}{transaction.payee}")
        print(f"{"  Amount:":<15}{transaction.amount_eur:.2f} EUR")

        if transaction.purpose:
            print(f"{"  Purpose:":<15}{transaction.purpose}")
        category = input("  Enter Category (or 'skip'/enter to skip): ").strip()
        if not category or category.lower() == "skip":
            transaction.category = "Uncategorized"
            continue
        default_keywords = _default_keywords(category, transaction)
        if default_keywords:
            print(f"  Automatic keywords: {', '.join(default_keywords)}")
        keywords = input(
            "  Keywords for this category, comma-separated "
            "(additional; press Enter to use automatic keywords): "
        ).strip()
        additional_keywords = [
            keyword.strip() for keyword in keywords.split(",") if keyword.strip()
        ]
        learned_keywords = [
            keyword.lower() for keyword in (*default_keywords, *additional_keywords)
        ]
        existing_keywords = rules.get(category, [])
        rules[category] = list(dict.fromkeys((*existing_keywords, *learned_keywords)))
        transaction.category = category



def write_csv(path: Path, transactions: Iterable[Transaction]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "date",
                "booking_type",
                "payee",
                "purpose",
                "amount_eur",
                "category",
            ],
            delimiter=";",
        )
        writer.writeheader()
        for transaction in transactions:
            row = asdict(transaction)
            row["amount_eur"] = f"{transaction.amount_eur:.2f}".replace(".", ",")
            writer.writerow(row)


def print_summary(transactions: Iterable[Transaction]) -> None:
    totals: defaultdict[str, float] = defaultdict(float)
    for transaction in transactions:
        if transaction.amount_eur < 0:
            totals[transaction.category] += abs(transaction.amount_eur)
    print("\nSpending summary:")
    for category, total in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        print(f"\t{category}: {total:.2f} EUR")

def main() -> int:
    file_loc = Path(__file__).parent
    pdf_dir = file_loc / "Kontoauszuege"
    default_rules = file_loc / "categories.json"
    interactive = True  # Set to False to disable interactive categorization

    try:
        rules = load_rules(default_rules)
        transactions: list[Transaction] = []
        for pdf_path in pdf_dir.glob("*.pdf"):
            if not pdf_path.is_file():
                raise FileNotFoundError(f"PDF not found: {pdf_path}")
            transactions.extend(parse_transactions(extract_text(pdf_path)))
        paypal_transactions: list[Transaction] = []
        paypal_dir = file_loc / "Paypal"
        if paypal_dir.exists() and not paypal_dir.is_dir():
            raise NotADirectoryError(f"PayPal path is not a directory: {paypal_dir}")
        paypal_paths = sorted(paypal_dir.iterdir()) if paypal_dir.exists() else []
        for paypal_path in paypal_paths:
            if paypal_path.is_file() and paypal_path.suffix.lower() == ".csv":
                paypal_transactions.extend(parse_paypal_transactions(paypal_path))
        if not transactions:
            raise ValueError("No transactions found. This may be a scanned PDF requiring OCR.")
        transactions = merge_paypal_transactions(transactions, paypal_transactions)
        categorize(transactions, rules, interactive=interactive)
        save_rules(default_rules, rules)
        write_csv(file_loc / "transactions.csv", transactions)
        print(
            f"Extracted {len(transactions)} transactions "
            f"({len(paypal_transactions)} from PayPal) to {file_loc / 'transactions.csv'}"
        )
        print_summary(transactions)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())