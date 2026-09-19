r"""Extract and categorize transactions from bank statements and PayPal exports.

Usage:
    python main.py

The program asks which bank the statements belong to, then each bank adapter
normalizes its files into the same Transaction records. Category rules live in
categories.json. Unmatched rows are categorized interactively.

PayPal transactions are read from the Paypal subfolder. Bank statement rows
that only represent PayPal funding are replaced by the detailed PayPal rows.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import src  # noqa: F401  registers bank adapters
from src.base import Bank, Transaction
from src.paypal import load_paypal_transactions, merge_paypal_transactions


# --- Config (edit these) ---
CONFIG = {
    "input_dir": "Kontoauszuege",
    "paypal_dir": "Paypal",
    "rules_file": "categories.json",
    "output_file": "transactions.csv",
    "interactive": True,  # Set to False to disable interactive categorization
}

MANDATE_RE = re.compile(r"(Mandat:\s*[^\s]+)", re.IGNORECASE)
RELATED_TRANSACTION_CODE_RE = re.compile(
    r"Zugehöriger Transaktionscode:\s*([^\s]+)", re.IGNORECASE
)


def _parse_keywords(raw: str) -> list[str]:
    return [keyword.strip().lower() for keyword in raw.split(",") if keyword.strip()]


def _win_input_with_prefill(prompt: str, prefill: str) -> str:
    """Windows line editor that starts with editable default text."""
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.write(prefill)
    sys.stdout.flush()

    chars = list(prefill)
    cursor = len(chars)

    def redraw_from_cursor() -> None:
        rest = "".join(chars[cursor:])
        sys.stdout.write(rest + " ")
        sys.stdout.write("\b" * (len(rest) + 1))
        sys.stdout.flush()

    while True:
        key = msvcrt.getwch()
        if key in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars)
        if key == "\x03":
            raise KeyboardInterrupt
        if key == "\x1a":
            raise EOFError
        if key in ("\x08", "\x7f"):
            if cursor > 0:
                cursor -= 1
                del chars[cursor]
                sys.stdout.write("\b")
                redraw_from_cursor()
            continue
        if key in ("\x00", "\xe0"):
            extra = msvcrt.getwch()
            if extra == "K" and cursor > 0:
                cursor -= 1
                sys.stdout.write("\b")
                sys.stdout.flush()
            elif extra == "M" and cursor < len(chars):
                sys.stdout.write(chars[cursor])
                cursor += 1
                sys.stdout.flush()
            elif extra == "G":
                sys.stdout.write("\b" * cursor)
                cursor = 0
                sys.stdout.flush()
            elif extra == "O":
                sys.stdout.write("".join(chars[cursor:]))
                cursor = len(chars)
                sys.stdout.flush()
            elif extra == "S" and cursor < len(chars):
                del chars[cursor]
                redraw_from_cursor()
            continue
        chars.insert(cursor, key)
        sys.stdout.write("".join(chars[cursor:]))
        cursor += 1
        leftover = len(chars) - cursor
        if leftover:
            sys.stdout.write("\b" * leftover)
        sys.stdout.flush()


def input_with_prefill(prompt: str, prefill: str = "") -> str:
    """Ask for input with default text already on the line so it can be edited."""
    if not prefill:
        return input(prompt)
    if not sys.stdin.isatty():
        entered = input(prompt)
        return entered if entered.strip() else prefill

    if sys.platform == "win32":
        return _win_input_with_prefill(prompt, prefill)

    try:
        import readline
    except ImportError:
        readline = None

    if readline is not None:
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return input(prompt)
        finally:
            readline.set_startup_hook(None)

    entered = input(f"{prompt}[{prefill}] ")
    return entered if entered.strip() else prefill


def pick_bank() -> Bank:
    banks = Bank.available()
    if not banks:
        raise RuntimeError("No banks are registered. Add a Bank subclass under src/.")

    print("Select your bank:")
    for index, bank_cls in enumerate(banks, start=1):
        print(f"  {index}) {bank_cls.label}")

    while True:
        choice = input("Choice: ").strip()
        if choice.isdigit():
            selected = int(choice)
            if 1 <= selected <= len(banks):
                return banks[selected - 1]()
        print(f"Please enter a number between 1 and {len(banks)}.")


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
        keywords = input_with_prefill(
            "  Keywords (edit automatic keywords or add additional comma-separated): ",
            ", ".join(default_keywords),
        )
        learned_keywords = _parse_keywords(keywords)
        existing_keywords = rules.get(category, [])
        rules[category] = list(dict.fromkeys((*existing_keywords, *learned_keywords)))
        transaction.category = category


def write_dashboard_data(path: Path, transactions: list[Transaction]) -> None:
    payload = [asdict(transaction) for transaction in transactions]
    path.write_text(
        "window.EMBEDDED_TRANSACTIONS = "
        + json.dumps(payload, ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )


def write_csv(path: Path, transactions: list[Transaction]) -> None:
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


def print_summary(transactions: list[Transaction]) -> None:
    totals: defaultdict[str, float] = defaultdict(float)
    for transaction in transactions:
        if transaction.amount_eur < 0:
            totals[transaction.category] += abs(transaction.amount_eur)
    print("\nSpending summary:")
    for category, total in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        print(f"\t{category}: {total:.2f} EUR")


def main() -> int:
    file_loc = Path(__file__).parent
    input_dir = file_loc / CONFIG["input_dir"]
    default_rules = file_loc / CONFIG["rules_file"]
    paypal_dir = file_loc / CONFIG["paypal_dir"]
    output_path = file_loc / CONFIG["output_file"]
    interactive = CONFIG["interactive"]

    try:
        bank = pick_bank()
        rules = load_rules(default_rules)
        transactions = bank.load(input_dir)
        paypal_transactions = load_paypal_transactions(paypal_dir)
        if not transactions:
            raise ValueError(f"No transactions found in {input_dir} for {bank.label}.")
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
