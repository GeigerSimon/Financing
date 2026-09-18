r"""Extract and categorize transactions from bank statements and PayPal exports.

Usage:
    python main.py

Set CONFIG["input_type"] to choose the bank-statement format:
    "ing_pdf"          ING PDF files in input_dir (*.pdf)
    "sparkasse_csv"    Sparkasse CSV exports in input_dir (*.csv)

Add another format by writing a loader and registering it in INPUT_LOADERS.

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
from typing import Callable, Iterable


# --- Config (edit these) ---
CONFIG = {
    # "ing_pdf" or "sparkasse_csv"
    "input_type": "sparkasse_csv",
    "input_dir": "Kontoauszuege",
    "paypal_dir": "Paypal",
    "rules_file": "categories.json",
    "output_file": "transactions.csv",
    "interactive": True,  # Set to False to disable interactive categorization
}

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


def _decode_csv_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _normalize_csv_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {(key or "").strip(): (value or "").strip() for key, value in row.items()}


def _csv_field(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


def _parse_bank_date(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value}")


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


SPARKASSE_REQUIRED_FIELDS = {
    "Buchungstag",
    "Buchungstext",
    "Verwendungszweck",
    "Betrag",
}
SPARKASSE_PAYEE_FIELDS = (
    "Beguenstigter/Zahlungspflichtiger",
    "Begünstigter/Zahlungspflichtiger",
)


def parse_sparkasse_transactions(csv_path: Path) -> list[Transaction]:
    """Read Sparkasse account exports (semicolon-separated CSV)."""
    text = _decode_csv_text(csv_path)
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
    missing = SPARKASSE_REQUIRED_FIELDS.difference(fieldnames)
    if missing:
        raise ValueError(
            f"Sparkasse CSV is missing columns: {', '.join(sorted(missing))}"
        )
    if not fieldnames.intersection(SPARKASSE_PAYEE_FIELDS):
        raise ValueError(
            "Sparkasse CSV is missing column: Beguenstigter/Zahlungspflichtiger"
        )

    transactions: list[Transaction] = []
    for raw_row in reader:
        row = _normalize_csv_row(raw_row)
        info = _csv_field(row, "Info")
        if info and info.casefold() != "umsatz gebucht":
            continue

        amount_raw = _csv_field(row, "Betrag")
        date_raw = _csv_field(row, "Buchungstag")
        if not amount_raw or not date_raw:
            continue

        currency = _csv_field(row, "Waehrung", "Währung")
        if currency and currency.upper() != "EUR":
            raise ValueError(f"Unsupported Sparkasse currency in {csv_path}: {currency}")

        try:
            date = _parse_bank_date(date_raw)
            amount = parse_amount(amount_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid Sparkasse transaction in {csv_path}: {row}") from exc

        purpose_parts = [_csv_field(row, "Verwendungszweck")]
        mandate = _csv_field(row, "Mandatsreferenz")
        if mandate:
            purpose_parts.append(f"Mandat: {mandate}")
        creditor_id = _csv_field(row, "Glaeubiger ID", "Gläubiger ID")
        if creditor_id:
            purpose_parts.append(f"Gläubiger-ID: {creditor_id}")

        transactions.append(
            Transaction(
                date=date,
                booking_type=_csv_field(row, "Buchungstext"),
                payee=_csv_field(row, *SPARKASSE_PAYEE_FIELDS),
                purpose=" ".join(part for part in purpose_parts if part),
                amount_eur=amount,
            )
        )
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
        print(f"{'  Date:':<15}{transaction.date}")
        print(f"{'  Payee:':<15}{transaction.payee}")
        print(f"{'  Amount:':<15}{transaction.amount_eur:.2f} EUR")

        if transaction.purpose:
            print(f"{'  Purpose:':<15}{transaction.purpose}")
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



def write_dashboard_data(path: Path, transactions: Iterable[Transaction]) -> None:
    payload = [asdict(transaction) for transaction in transactions]
    path.write_text(
        "window.EMBEDDED_TRANSACTIONS = "
        + json.dumps(payload, ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )


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


def _require_input_dir(input_dir: Path) -> Path:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")
    return input_dir


def load_ing_pdf_transactions(input_dir: Path) -> list[Transaction]:
    input_dir = _require_input_dir(input_dir)
    pdf_paths = sorted(path for path in input_dir.glob("*.pdf") if path.is_file())
    if not pdf_paths:
        raise FileNotFoundError(f"No PDF files found in {input_dir}")
    transactions: list[Transaction] = []
    for pdf_path in pdf_paths:
        transactions.extend(parse_transactions(extract_text(pdf_path)))
    return transactions


def load_sparkasse_csv_transactions(input_dir: Path) -> list[Transaction]:
    input_dir = _require_input_dir(input_dir)
    csv_paths = sorted(path for path in input_dir.glob("*.csv") if path.is_file())
    if not csv_paths:
        raise FileNotFoundError(f"No CSV files found in {input_dir}")
    transactions: list[Transaction] = []
    for csv_path in csv_paths:
        transactions.extend(parse_sparkasse_transactions(csv_path))
    return transactions


INPUT_LOADERS: dict[str, Callable[[Path], list[Transaction]]] = {
    "ing_pdf": load_ing_pdf_transactions,
    "sparkasse_csv": load_sparkasse_csv_transactions,
}


def load_bank_transactions(input_type: str, input_dir: Path) -> list[Transaction]:
    loader = INPUT_LOADERS.get(input_type)
    if loader is None:
        supported = ", ".join(sorted(INPUT_LOADERS))
        raise ValueError(f"Unknown input_type {input_type!r}. Supported: {supported}")
    return loader(input_dir)


def main() -> int:
    file_loc = Path(__file__).parent
    input_dir = file_loc / CONFIG["input_dir"]
    default_rules = file_loc / CONFIG["rules_file"]
    paypal_dir = file_loc / CONFIG["paypal_dir"]
    output_path = file_loc / CONFIG["output_file"]
    interactive = CONFIG["interactive"]

    try:
        rules = load_rules(default_rules)
        transactions = load_bank_transactions(CONFIG["input_type"], input_dir)
        paypal_transactions: list[Transaction] = []
        if paypal_dir.exists() and not paypal_dir.is_dir():
            raise NotADirectoryError(f"PayPal path is not a directory: {paypal_dir}")
        paypal_paths = sorted(paypal_dir.iterdir()) if paypal_dir.exists() else []
        for paypal_path in paypal_paths:
            if paypal_path.is_file() and paypal_path.suffix.lower() == ".csv":
                paypal_transactions.extend(parse_paypal_transactions(paypal_path))
        if not transactions:
            raise ValueError(
                f"No transactions found in {input_dir} for input_type {CONFIG['input_type']!r}."
            )
        transactions = merge_paypal_transactions(transactions, paypal_transactions)
        categorize(transactions, rules, interactive=interactive)
        save_rules(default_rules, rules)
        write_csv(output_path, transactions)
        write_dashboard_data(file_loc / "spending-data.js", transactions)
        print(
            f"Extracted {len(transactions)} transactions "
            f"({len(paypal_transactions)} from PayPal) to {output_path}"
        )
        print(f"Open dashboard.html to explore spending.")
        print_summary(transactions)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())