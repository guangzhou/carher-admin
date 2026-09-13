#!/usr/bin/env python3
"""Shared fail-closed contract for the approved additive DDL ledger."""

from __future__ import annotations

import re
from typing import NoReturn


IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
TYPE = (
    rf'(?:{IDENT}\s*\.\s*)?{IDENT}'
    r'(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?'
    r'(?:\s*\[\s*\])?'
)
ADD_COLUMN = re.compile(
    rf'^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?'
    rf'(?:{IDENT}\s*\.\s*)?{IDENT}\s+'
    rf'ADD\s+COLUMN(?:\s+IF\s+NOT\s+EXISTS)?\s+'
    rf'{IDENT}\s+{TYPE}'
    r'(?:\s+DEFAULT\s+(?P<default>.+?))?'
    r'(?:\s+(?P<not_null>NOT\s+NULL))?\s*;?$',
    re.I | re.S,
)
FORBIDDEN_TOKENS = re.compile(
    r'\b(?:UNIQUE|PRIMARY\s+KEY|REFERENCES|CHECK|CONSTRAINT|GENERATED|IDENTITY|'
    r'COLLATE|EXCLUDE|DROP|TRUNCATE|RENAME|ALTER\s+COLUMN)\b',
    re.I | re.S,
)
SAFE_DEFAULTS = (
    re.compile(r'^NULL$', re.I),
    re.compile(r'^(?:TRUE|FALSE)$', re.I),
    re.compile(r'^[-+]?\d+(?:\.\d+)?$'),
    re.compile(r"^'(?:''|[^'])*'(?:::(?:" + IDENT + r'\s*\.\s*)?' + IDENT + r')?$', re.I),
    re.compile(r"^'\{\}'(?:::(?:json|jsonb))?$", re.I),
    re.compile(r'^ARRAY\s*\[\s*\](?:::(?:' + IDENT + r')\s*\[\s*\])?$', re.I),
)


def invalid(message: str) -> NoReturn:
    raise ValueError(message)


def validate_add_column(sql: str) -> str:
    """Return normalized SQL only for the reviewed, non-volatile subset."""

    if not isinstance(sql, str) or not sql.strip() or "\x00" in sql:
        invalid("SQL is empty or contains NUL")
    normalized = re.sub(r"\s+", " ", sql.strip())
    if normalized.count(";") > 1 or (";" in normalized and not normalized.endswith(";")):
        invalid("multiple SQL statements are not allowed")
    if "--" in normalized or "/*" in normalized or "*/" in normalized:
        invalid("SQL comments are not allowed")
    if FORBIDDEN_TOKENS.search(normalized):
        invalid("column constraints and non-additive clauses are not allowed")
    match = ADD_COLUMN.fullmatch(normalized)
    if match is None:
        invalid("statement is outside the approved ADD COLUMN grammar")
    default = match.group("default")
    if default is not None:
        default = default.strip()
        if "(" in default or ")" in default or not any(pattern.fullmatch(default) for pattern in SAFE_DEFAULTS):
            invalid("default expression is not an approved immutable literal")
    return normalized.removesuffix(";")
