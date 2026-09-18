# Financing

Extract and categorize transactions from German bank statements and PayPal exports. Everything runs locally: PDFs are parsed on your machine, and category rules stay in a local `categories.json`.

## What it does

1. Loads bank statements from `Kontoauszuege/` (ING PDFs or Sparkasse CSVs).
2. Optionally merges detailed PayPal rows from `Paypal/`, replacing the coarse bank-level PayPal funding entries.
3. Assigns categories using saved keyword rules. Unmatched rows are categorized interactively, and those rules are reused next time.
4. Writes `transactions.csv` and prints a spending summary (outgoing amounts by category).

## Requirements

- Python 3.11+
- [pypdf](https://pypi.org/project/pypdf/) (only needed for ING PDFs)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On macOS/Linux, activate with `source .venv/bin/activate`.

You can also install the package itself (`pip install -e .`), which provides a `financing` command.

## Input files

Place statements next to `main.py`:

```
Kontoauszuege/     bank statements (*.pdf or *.csv)
Paypal/            optional PayPal CSV exports
```

Those folders, `categories.json`, and `transactions.csv` are gitignored so statement data stays out of version control.

### Bank formats

Set `CONFIG["input_type"]` in `main.py`:

| `input_type`     | Files in `Kontoauszuege/` | Notes |
|------------------|---------------------------|--------|
| `sparkasse_csv`  | `*.csv`                   | Semicolon-separated Sparkasse export. Looks for `Buchungstag`, `Buchungstext`, `Verwendungszweck`, `Betrag`, and a payee column. Only booked rows (`Umsatz gebucht`) are kept. |
| `ing_pdf`        | `*.pdf`                   | Text-based ING account statements. Image-only PDFs will not work. |

To add another format, write a loader that returns `Transaction` objects and register it in `INPUT_LOADERS`.

### PayPal

Drop German PayPal activity CSVs into `Paypal/`. Required columns:

- `Datum`
- `Beschreibung`
- `Brutto`
- `Währung` (EUR only)
- `Transaktionscode`
- `Zugehöriger Transaktionscode`

Balance-transfer rows (`Bankgutschrift auf PayPal-Konto`, `Von Nutzer eingeleitete Abbuchung`) are skipped. Bank statement rows that mention PayPal are then replaced by these detailed PayPal transactions.

## Configuration

Edit the `CONFIG` dict at the top of `main.py`:

```python
CONFIG = {
    "input_type": "sparkasse_csv",  # or "ing_pdf"
    "input_dir": "Kontoauszuege",
    "paypal_dir": "Paypal",
    "rules_file": "categories.json",
    "output_file": "transactions.csv",
    "interactive": True,  # False: unmatched rows become "Uncategorized"
}
```

## Usage

```bash
python main.py
```

For each unmatched transaction you are asked for:

1. A **category** (or `skip` / Enter to leave it uncategorized).
2. Optional extra **keywords**. Mandate IDs, related PayPal transaction codes, or the payee are suggested automatically.

Rules are stored in `categories.json` as category → keyword lists. Matching prefers payee hits over shared identifiers such as mandate numbers.

Set `"interactive": False` to run unattended; unmatched rows are labeled `Uncategorized`.

## Output

`transactions.csv` is semicolon-separated UTF-8 with BOM, with columns:

| Column         | Description |
|----------------|-------------|
| `date`         | Booking date (`DD.MM.YYYY`) |
| `booking_type` | Bank booking text, or `PayPal` |
| `payee`        | Counterparty |
| `purpose`      | Purpose / memo |
| `amount_eur`   | Amount in EUR (comma decimal; negative = outgoing) |
| `category`     | Assigned category |

After writing the file, the program prints how many rows were extracted and a spending summary of outgoing amounts by category.
