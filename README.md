# Financing

Extract and categorize transactions from German bank statements and PayPal exports. Everything runs locally: PDFs are parsed on your machine, and category rules stay in a local `categories.json`.

## What it does

1. Asks which bank the statements belong to.
2. Loads bank statements from `Kontoauszuege/` through that bank's adapter, which normalizes them into the same transaction format.
3. Optionally merges detailed PayPal rows from `Paypal/`, replacing the coarse bank-level PayPal funding entries.
4. Assigns categories using saved keyword rules. Unmatched rows are categorized interactively, and those rules are reused next time.
5. Writes `transactions.csv` and prints a spending summary (outgoing amounts by category).

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
Kontoauszuege/     bank statements (*.pdf or *.csv, depending on the bank)
Paypal/            optional PayPal CSV exports
```

Those folders, `categories.json`, and `transactions.csv` are gitignored so statement data stays out of version control.

### Bank formats

Run `python main.py` and pick a bank. Each adapter lives in `src/` and maps its files onto the shared `Transaction` model:

| Bank        | Adapter              | Files in `Kontoauszuege/` | Notes |
|-------------|----------------------|---------------------------|--------|
| Sparkasse   | `src/Sparkasse.py`   | `*.csv`                   | Semicolon-separated Sparkasse export. Looks for `Buchungstag`, `Buchungstext`, `Verwendungszweck`, `Betrag`, and a payee column. Only booked rows (`Umsatz gebucht`) are kept. |
| ING         | `src/ING.py`         | `*.pdf`                   | Text-based ING account statements. Image-only PDFs will not work. |

To add another bank, create a new file under `src/`, subclass `Bank` from `src/base.py`, implement `load()` so it returns `Transaction` objects, and import the module in `src/__init__.py`.

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

You will be asked which bank to use. After that the pipeline is the same for every bank.

For each unmatched transaction you are asked for:

1. A **category** (or `skip` / Enter to leave it uncategorized).
2. **Keywords**, prefilled with the automatic suggestion (mandate ID, related PayPal transaction code, or payee). Edit the line in place, press Enter to keep it, or add extra terms separated by commas.

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
