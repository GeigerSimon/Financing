from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar


@dataclass
class Transaction:
    date: str
    booking_type: str
    payee: str
    purpose: str
    amount_eur: float
    category: str = ""


class Bank(ABC):
    """Maps one bank's statement format onto the shared Transaction model.

    Subclass this, set ``key`` / ``label`` / ``file_pattern``, implement
    ``load``, and import the module from ``src/__init__.py`` so it is
    registered automatically.
    """

    key: ClassVar[str]
    label: ClassVar[str]
    file_pattern: ClassVar[str]

    _registry: ClassVar[dict[str, type[Bank]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if ABC in cls.__bases__:
            return
        if not getattr(cls, "key", None) or not getattr(cls, "label", None):
            raise TypeError(f"{cls.__name__} must define key and label")
        if not getattr(cls, "file_pattern", None):
            raise TypeError(f"{cls.__name__} must define file_pattern")
        Bank._registry[cls.key] = cls

    @classmethod
    def available(cls) -> list[type[Bank]]:
        return sorted(cls._registry.values(), key=lambda bank: bank.label.lower())

    @abstractmethod
    def load(self, input_dir: Path) -> list[Transaction]:
        """Read every statement in ``input_dir`` and return normalized rows."""

    def statement_paths(self, input_dir: Path) -> list[Path]:
        input_dir = require_input_dir(input_dir)
        paths = sorted(
            path for path in input_dir.glob(self.file_pattern) if path.is_file()
        )
        if not paths:
            raise FileNotFoundError(
                f"No files matching {self.file_pattern} found in {input_dir}"
            )
        return paths


def parse_amount(value: str) -> float:
    normalized = value.replace(" ", "").replace(".", "").replace(",", ".")
    return float(normalized)


def decode_csv_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_csv_row(row: dict[str | None, str | None]) -> dict[str, str]:
    return {(key or "").strip(): (value or "").strip() for key, value in row.items()}


def csv_field(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


def parse_bank_date(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%d.%m.%Y")
        except ValueError:
            continue
    raise ValueError(f"Invalid date: {value}")


def require_input_dir(input_dir: Path) -> Path:
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input path is not a directory: {input_dir}")
    return input_dir
