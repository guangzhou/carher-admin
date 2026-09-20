#!/usr/bin/env python3
"""Prepare and publish a local PDF-invoice rollup to a Feishu Docx.

The command is intentionally fail-closed around money values.  It can inspect
PDFs and suggest text candidates, but every invoice must have an explicit
amount before a publish is allowed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse
from xml.etree import ElementTree

try:
    from PIL import Image, ImageDraw, ImageOps
except ImportError:  # pragma: no cover - optional preview dependency
    Image = ImageDraw = ImageOps = None


CENT = Decimal("0.01")
AMOUNT_RE = re.compile(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?(?![\d.])")
EXPLICIT_AMOUNT_RE = re.compile(r"(?:\d+(?:\.\d{1,2})?|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)")
KEYWORD_RE = re.compile(r"价税合计|小写|合计|金额|价税", re.IGNORECASE)
RETRYABLE_RE = re.compile(r"(?:\b429\b|rate.?limit|too many requests|temporar(?:y|ily))", re.IGNORECASE)


class InvoiceError(RuntimeError):
    """A user-actionable validation or remote-operation error."""


class CommandError(InvoiceError):
    def __init__(self, argv: Sequence[str], returncode: int, stdout: str, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        command = " ".join(str(part) for part in argv)
        detail = (stderr or stdout).strip()
        super().__init__(f"command failed ({returncode}): {command}\n{detail}")


@dataclass
class Invoice:
    index: int
    file: str
    size_bytes: int
    sha256: str
    pages: int | None = None
    amount: str | None = None
    amount_source: str | None = None
    text_candidates: list[str] | None = None


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json_dump(value) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_command(
    argv: Sequence[str],
    *,
    timeout: float = 60,
    input_text: str | None = None,
) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [str(part) for part in argv],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise InvoiceError(f"required command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise InvoiceError(f"command timed out after {timeout}s: {argv[0]}") from exc
    if result.returncode:
        raise CommandError(argv, result.returncode, result.stdout, result.stderr)
    return result.stdout, result.stderr


def parse_json_output(stdout: str, *, command_name: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise InvoiceError(f"{command_name} returned non-JSON output: {stdout[:500]}") from exc
    if not isinstance(value, dict):
        raise InvoiceError(f"{command_name} returned an unexpected JSON shape")
    if value.get("ok") is False:
        error = value.get("error") or value
        raise InvoiceError(f"{command_name} failed: {json.dumps(error, ensure_ascii=False)}")
    return value


def run_lark_json(argv: Sequence[str], *, timeout: float = 120) -> dict[str, Any]:
    stdout, _ = run_command(argv, timeout=timeout)
    return parse_json_output(stdout, command_name=" ".join(argv[:3]))


def run_with_retries(
    operation: Any,
    *,
    label: str,
    attempts: int = 5,
) -> Any:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (CommandError, InvoiceError) as exc:
            text = str(exc)
            if attempt >= attempts or not RETRYABLE_RE.search(text):
                raise
            delay = min(8.0, 1.5 * (2 ** (attempt - 1)))
            log(f"{label}: transient failure, retry {attempt + 1}/{attempts} in {delay:.1f}s")
            time.sleep(delay)
    raise AssertionError("unreachable")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def optional_command(name: str) -> str | None:
    return shutil.which(name)


def pdf_pages(path: Path) -> int | None:
    binary = optional_command("pdfinfo")
    if not binary:
        return None
    try:
        stdout, _ = run_command([binary, str(path)], timeout=20)
    except InvoiceError:
        return None
    match = re.search(r"^Pages:\s*(\d+)\s*$", stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def text_candidates(path: Path) -> list[str]:
    binary = optional_command("pdftotext")
    if not binary:
        return []
    try:
        stdout, _ = run_command([binary, "-layout", str(path), "-"], timeout=30)
    except InvoiceError:
        return []
    values: list[str] = []
    for line in stdout.splitlines():
        if not KEYWORD_RE.search(line):
            continue
        for match in AMOUNT_RE.findall(line):
            try:
                amount = normalize_amount(match)
            except InvoiceError:
                continue
            if amount not in values:
                values.append(amount)
    return values


def normalize_amount(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise InvoiceError("amount is required")
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "")
    original = str(value).strip().replace("￥", "").replace("¥", "")
    if not EXPLICIT_AMOUNT_RE.fullmatch(original):
        raise InvoiceError(f"invalid amount: {value!r}")
    try:
        amount = Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise InvoiceError(f"invalid amount: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise InvoiceError(f"amount must be a finite non-negative number: {value!r}")
    return f"{amount:.2f}"


def display_amount(value: str) -> str:
    return f"{Decimal(value):,.2f}"


def amount_total(invoices: Iterable[Invoice]) -> str:
    total = sum((Decimal(invoice.amount or "0") for invoice in invoices), Decimal("0"))
    return f"{total.quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def sorted_pdf_paths(input_dir: Path, exclude_patterns: Sequence[str] = ()) -> tuple[list[Path], list[str]]:
    if not input_dir.is_dir():
        raise InvoiceError(f"input directory does not exist: {input_dir}")
    candidates = sorted(
        (path for path in input_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.relative_to(input_dir).as_posix().casefold(),
    )
    excluded: list[str] = []
    paths: list[Path] = []
    for path in candidates:
        relative = path.relative_to(input_dir).as_posix()
        basename = path.name
        if any(
            fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(basename, pattern)
            for pattern in exclude_patterns
        ):
            excluded.append(relative)
        else:
            paths.append(path)
    if not paths:
        raise InvoiceError(f"no PDF files found under: {input_dir}")
    names = [path.name for path in paths]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise InvoiceError(f"duplicate PDF basenames are ambiguous: {', '.join(duplicates)}")
    return paths, excluded


def inspect_directory(input_dir: Path, exclude_patterns: Sequence[str] = ()) -> dict[str, Any]:
    paths, excluded = sorted_pdf_paths(input_dir, exclude_patterns)
    invoices: list[Invoice] = []
    for index, path in enumerate(paths, start=1):
        relative = path.relative_to(input_dir).as_posix()
        candidates = text_candidates(path)
        invoices.append(
            Invoice(
                index=index,
                file=relative,
                size_bytes=path.stat().st_size,
                sha256=sha256_file(path),
                pages=pdf_pages(path),
                text_candidates=candidates,
            )
        )
    return {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_dir": str(input_dir.resolve()),
        "invoice_count": len(invoices),
        "exclude_patterns": list(exclude_patterns),
        "excluded_files": excluded,
        "invoices": [asdict(invoice) for invoice in invoices],
        "amounts_complete": False,
        "total": None,
    }


def load_manifest(path: Path, *, input_dir_override: Path | None = None) -> tuple[dict[str, Any], Path, list[Invoice]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InvoiceError(f"could not read manifest {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("invoices"), list):
        raise InvoiceError("manifest must be an object with an invoices array")
    if input_dir_override is not None:
        input_dir_value = input_dir_override
    else:
        raw_input_dir = raw.get("input_dir")
        if not raw_input_dir:
            raise InvoiceError("manifest is missing input_dir; pass --input-dir")
        input_dir_value = Path(str(raw_input_dir))
    input_dir = input_dir_value.expanduser().resolve()
    invoices: list[Invoice] = []
    for expected_index, item in enumerate(raw["invoices"], start=1):
        if not isinstance(item, dict) or not item.get("file"):
            raise InvoiceError(f"manifest invoice {expected_index} is malformed")
        relative = Path(str(item["file"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise InvoiceError(f"invoice path must be relative to input_dir: {relative}")
        source = (input_dir / relative).resolve()
        try:
            source.relative_to(input_dir)
        except ValueError as exc:
            raise InvoiceError(f"invoice path escapes input_dir: {relative}") from exc
        if not source.is_file():
            raise InvoiceError(f"invoice file does not exist: {source}")
        actual_size = source.stat().st_size
        actual_sha = sha256_file(source)
        if item.get("size_bytes") is not None and int(item["size_bytes"]) != actual_size:
            raise InvoiceError(f"invoice changed since manifest was created: {relative} (size mismatch)")
        if item.get("sha256") and str(item["sha256"]) != actual_sha:
            raise InvoiceError(f"invoice changed since manifest was created: {relative} (sha256 mismatch)")
        index = int(item.get("index", expected_index))
        if index != expected_index:
            raise InvoiceError("manifest invoices must have contiguous indexes starting at 1")
        amount_value = item.get("amount")
        amount = normalize_amount(amount_value) if amount_value not in (None, "") else None
        candidates = item.get("text_candidates")
        if not isinstance(candidates, list):
            candidates = []
        invoices.append(
            Invoice(
                index=index,
                file=relative.as_posix(),
                size_bytes=actual_size,
                sha256=actual_sha,
                pages=int(item["pages"]) if item.get("pages") is not None else pdf_pages(source),
                amount=amount,
                amount_source=str(item.get("amount_source")) if item.get("amount_source") else None,
                text_candidates=[str(value) for value in candidates],
            )
        )
    if not invoices:
        raise InvoiceError("manifest contains no invoices")
    if raw.get("amounts_complete") is True:
        for invoice in invoices:
            if not invoice.amount:
                raise InvoiceError("manifest marks amounts_complete but an invoice has no amount")
    return raw, input_dir, invoices


def apply_amounts(invoices: list[Invoice], values: Sequence[str]) -> None:
    if len(values) != len(invoices):
        raise InvoiceError(f"received {len(values)} amounts for {len(invoices)} invoices")
    for invoice, value in zip(invoices, values):
        invoice.amount = normalize_amount(value)
        invoice.amount_source = "explicit-cli-order"


def require_complete_amounts(invoices: Sequence[Invoice]) -> None:
    missing = [str(invoice.index) for invoice in invoices if not invoice.amount]
    if missing:
        raise InvoiceError(
            "every invoice needs an explicit amount before publish; missing indexes: "
            + ", ".join(missing)
        )


def xml_text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def build_document_xml(title: str, invoices: Sequence[Invoice], note: str) -> tuple[str, str]:
    require_complete_amounts(invoices)
    total = amount_total(invoices)
    expression = " + ".join(display_amount(invoice.amount or "0") for invoice in invoices)
    row_count = len(invoices)
    rows: list[str] = []
    for invoice in invoices:
        amount = display_amount(invoice.amount or "0")
        source = (
            f'<figure view-type="Preview"><source name="{xml_text(Path(invoice.file).name)}" '
            f'mime="application/pdf" size="{invoice.size_bytes}"/></figure>'
        )
        explanation = f"<td rowspan=\"{row_count}\"><p>{xml_text(note)}</p></td>" if invoice.index == 1 else ""
        rows.append(
            f"<tr><td>{invoice.index}</td><td>{amount}</td>"
            f"<td><p>发票{invoice.index}：{amount}</p>{source}</td>{explanation}</tr>"
        )
    xml = (
        f"<title>{xml_text(title)}</title>"
        f"<h1>一：总发票金额</h1>"
        f'<callout emoji="💰" background-color="light-green" border-color="green">'
        f"<p>本批次共 {row_count} 张发票，发票合计：<b>{display_amount(total)} 元</b>。</p>"
        f"</callout>"
        f"<p>{xml_text(expression)} = <b>{display_amount(total)}</b></p>"
        f"<h1>二：发票汇总</h1>"
        f"<table><colgroup><col width=\"100\"/><col width=\"140\"/>"
        f"<col width=\"620\"/><col width=\"160\"/></colgroup>"
        f"<thead><tr><th background-color=\"light-gray\">发票序号</th>"
        f"<th background-color=\"light-gray\">金额（元）</th>"
        f"<th background-color=\"light-gray\">发票附件</th>"
        f"<th background-color=\"light-gray\">说明</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    return xml, total


def render_contact_sheet(input_dir: Path, invoices: Sequence[Invoice], output: Path) -> str | None:
    pdftoppm = optional_command("pdftoppm")
    if not pdftoppm:
        return "pdftoppm not found; contact sheet was not generated"
    if Image is None or ImageDraw is None or ImageOps is None:
        return "Pillow is not installed; contact sheet was not assembled"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="feishu-invoice-preview-") as temp_dir:
        preview_paths: list[Path] = []
        for invoice in invoices:
            source = input_dir / invoice.file
            target = Path(temp_dir) / f"{invoice.index:03d}"
            try:
                run_command(
                    [pdftoppm, "-f", "1", "-singlefile", "-png", "-r", "150", str(source), str(target)],
                    timeout=45,
                )
            except InvoiceError as exc:
                return f"could not render {invoice.file}: {exc}"
            preview_paths.append(Path(f"{target}.png"))
        try:
            tile_width, tile_height = 420, 300
            gutter, label_height = 16, 28
            columns = 3
            rows = (len(preview_paths) + columns - 1) // columns
            sheet = Image.new(
                "RGB",
                (columns * tile_width + (columns + 1) * gutter, rows * (tile_height + label_height) + (rows + 1) * gutter),
                "white",
            )
            draw = ImageDraw.Draw(sheet)
            for offset, preview in enumerate(preview_paths):
                with Image.open(preview) as image:
                    thumbnail = ImageOps.contain(image.convert("RGB"), (tile_width, tile_height))
                column, row = offset % columns, offset // columns
                x = gutter + column * (tile_width + gutter)
                y = gutter + row * (tile_height + label_height + gutter) + label_height
                sheet.paste(thumbnail, (x + (tile_width - thumbnail.width) // 2, y + (tile_height - thumbnail.height) // 2))
                draw.text((x, y - label_height + 5), f"Invoice {invoices[offset].index}", fill="black")
            sheet.save(output, format="PNG")
        except (OSError, ValueError) as exc:
            return f"could not assemble contact sheet: {exc}"
    return None


def extract_source_blocks(xml: str) -> list[dict[str, str]]:
    root = parse_xml_fragment(xml)
    blocks: list[dict[str, str]] = []
    for source in root.iter("source"):
        attributes = {key: value for key, value in source.attrib.items()}
        if not attributes.get("id"):
            continue
        blocks.append(attributes)
    return blocks


def parse_xml_fragment(xml: str) -> ElementTree.Element:
    """Parse DocxXML, which commonly contains several top-level blocks."""
    cleaned = re.sub(r"<\?xml[^>]*\?>", "", xml, count=1).strip()
    try:
        return ElementTree.fromstring(f"<fragment>{cleaned}</fragment>")
    except ElementTree.ParseError as exc:
        raise InvoiceError(f"Feishu returned invalid document XML: {exc}") from exc


def cell_text(cell: ElementTree.Element) -> str:
    return "".join(cell.itertext()).strip()


def verify_document_xml(xml: str, invoices: Sequence[Invoice]) -> dict[str, Any]:
    require_complete_amounts(invoices)
    root = parse_xml_fragment(xml)
    sources = [element for element in root.iter("source") if element.attrib.get("name")]
    expected_names = [Path(invoice.file).name for invoice in invoices]
    actual_names = [str(element.attrib["name"]) for element in sources]
    if actual_names != expected_names:
        raise InvoiceError(f"attachment order/name mismatch: expected {expected_names}, got {actual_names}")
    missing_tokens = [name for name, source in zip(expected_names, sources) if not source.attrib.get("token")]
    if missing_tokens:
        raise InvoiceError("attachments without bound file tokens: " + ", ".join(missing_tokens))
    tables = list(root.iter("table"))
    if not tables:
        raise InvoiceError("document has no invoice table")
    rows = list(tables[-1].iter("tr"))
    body_rows = rows[1:] if rows and list(rows[0].iter("th")) else rows
    if len(body_rows) != len(invoices):
        raise InvoiceError(f"invoice row count mismatch: expected {len(invoices)}, got {len(body_rows)}")
    actual_amounts: list[str] = []
    for row in body_rows:
        cells = list(row.findall("td"))
        if len(cells) < 2:
            raise InvoiceError("invoice table row has fewer than two cells")
        actual_amounts.append(normalize_amount(cell_text(cells[1])))
    expected_amounts = [invoice.amount or "" for invoice in invoices]
    if actual_amounts != expected_amounts:
        raise InvoiceError(f"invoice amounts mismatch: expected {expected_amounts}, got {actual_amounts}")
    return {
        "attachments": True,
        "amounts": True,
        "invoice_count": len(invoices),
        "total": amount_total(invoices),
    }


def resolve_reference(reference_url: str, identity: str) -> dict[str, Any]:
    result = run_lark_json(
        [
            "lark-cli",
            "wiki",
            "+node-get",
            "--node-token",
            reference_url,
            "--as",
            identity,
            "--format",
            "json",
        ]
    )
    data = result.get("data")
    if not isinstance(data, dict):
        raise InvoiceError("wiki +node-get did not return node data")
    if data.get("obj_type") not in (None, "docx", "doc"):
        raise InvoiceError(f"reference must resolve to a Docx/document, got {data.get('obj_type')}")
    return data


def create_node(
    *,
    title: str,
    identity: str,
    space_id: str | None,
    parent_node_token: str | None,
) -> dict[str, Any]:
    argv = ["lark-cli", "wiki", "+node-create", "--title", title, "--obj-type", "docx", "--as", identity, "--format", "json"]
    if parent_node_token:
        argv.extend(["--parent-node-token", parent_node_token])
    elif space_id:
        argv.extend(["--space-id", space_id])
    else:
        raise InvoiceError("create target needs --space-id or --parent-node-token")
    result = run_with_retries(lambda: run_lark_json(argv), label="create Feishu node")
    data = result.get("data")
    if not isinstance(data, dict) or not data.get("obj_token"):
        raise InvoiceError("wiki +node-create did not return obj_token")
    return data


def overwrite_document(doc_id: str, xml: str, identity: str) -> None:
    # Passing large XML inline works, but @file avoids shell argument limits.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", dir="/tmp", delete=False) as handle:
        handle.write(xml)
        content_file = Path(handle.name)
    try:
        run_with_retries(
            lambda: run_lark_json(
                [
                    "lark-cli",
                    "docs",
                    "+update",
                    "--api-version",
                    "v2",
                    "--doc",
                    doc_id,
                    "--command",
                    "overwrite",
                    "--content",
                    f"@{content_file}",
                    "--as",
                    identity,
                    "--format",
                    "json",
                ]
            ),
            label="write document skeleton",
        )
    finally:
        content_file.unlink(missing_ok=True)


def fetch_document(doc_id: str, identity: str) -> str:
    result = run_with_retries(
        lambda: run_lark_json(
            [
                "lark-cli",
                "docs",
                "+fetch",
                "--api-version",
                "v2",
                "--doc",
                doc_id,
                "--doc-format",
                "xml",
                "--detail",
                "full",
                "--format",
                "json",
                "--as",
                identity,
            ]
        ),
        label="fetch document",
    )
    document = (result.get("data") or {}).get("document")
    if not isinstance(document, dict) or not isinstance(document.get("content"), str):
        raise InvoiceError("docs +fetch did not return document content")
    return document["content"]


def extract_doc_id(value: str) -> str:
    """Accept a Docx token or a URL while keeping API paths token-only."""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value.strip().rstrip("/").split("/")[-1]
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[-1] in {"docx", "wiki"}:
        raise InvoiceError(f"could not extract a document id from: {value}")
    return parts[-1]


def stage_invoice(source: Path, stage_dir: Path) -> Path:
    # lark-cli accepts /tmp and cwd paths but intentionally rejects arbitrary
    # out-of-tree paths; staging also prevents shell quoting surprises.
    target = stage_dir / source.name
    shutil.copy2(source, target)
    return target


def upload_media(doc_id: str, block_id: str, staged_file: Path, identity: str) -> str:
    result = run_with_retries(
        lambda: run_lark_json(
            [
                "lark-cli",
                "docs",
                "+media-upload",
                "--doc-id",
                doc_id,
                "--parent-node",
                block_id,
                "--parent-type",
                "docx_file",
                "--file",
                str(staged_file),
                "--as",
                identity,
                "--format",
                "json",
            ]
        ),
        label=f"upload {staged_file.name}",
    )
    token = (result.get("data") or {}).get("file_token")
    if not token:
        raise InvoiceError(f"media upload returned no file_token for {staged_file.name}")
    return str(token)


def bind_media(doc_id: str, block_id: str, file_token: str, identity: str) -> None:
    body = json.dumps(
        {"requests": [{"block_id": block_id, "replace_file": {"token": file_token}}]},
        ensure_ascii=False,
    )
    run_with_retries(
        lambda: run_lark_json(
            [
                "lark-cli",
                "api",
                "PATCH",
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/batch_update",
                "--data",
                body,
                "--as",
                identity,
                "--format",
                "json",
            ]
        ),
        label=f"bind {block_id}",
    )


def publish(
    *,
    manifest_path: Path,
    input_dir_override: Path | None,
    title: str,
    note: str,
    reference_url: str | None,
    space_id: str | None,
    parent_node_token: str | None,
    identity: str,
    apply: bool,
) -> dict[str, Any]:
    raw, input_dir, invoices = load_manifest(manifest_path, input_dir_override=input_dir_override)
    require_complete_amounts(invoices)
    xml, total = build_document_xml(title, invoices, note)
    if not apply:
        return {
            "ok": True,
            "dry_run": True,
            "title": title,
            "invoice_count": len(invoices),
            "total": total,
            "files": [invoice.file for invoice in invoices],
            "next": "rerun with --apply after reviewing amounts and document XML",
        }
    if reference_url:
        reference = resolve_reference(reference_url, identity)
        parent_node_token = parent_node_token or str(reference.get("parent_node_token") or "") or None
        space_id = space_id or str(reference.get("space_id") or "") or None
    node = create_node(
        title=title,
        identity=identity,
        space_id=space_id,
        parent_node_token=parent_node_token,
    )
    doc_id = str(node["obj_token"])
    overwrite_document(doc_id, xml, identity)
    initial_xml = fetch_document(doc_id, identity)
    source_blocks = extract_source_blocks(initial_xml)
    if len(source_blocks) != len(invoices):
        raise InvoiceError(f"document skeleton has {len(source_blocks)} source blocks, expected {len(invoices)}")
    # Keep staged files under lark-cli's allowlisted /tmp root.
    with tempfile.TemporaryDirectory(prefix="feishu-invoices-", dir="/tmp") as temp_dir:
        stage_dir = Path(temp_dir)
        for invoice, source_block in zip(invoices, source_blocks):
            source = (input_dir / invoice.file).resolve()
            staged = stage_invoice(source, stage_dir)
            file_token = upload_media(doc_id, source_block["id"], staged, identity)
            bind_media(doc_id, source_block["id"], file_token, identity)
    verified_xml = fetch_document(doc_id, identity)
    verification = verify_document_xml(verified_xml, invoices)
    document_url = canonical_document_url(
        doc_id,
        reference_url=reference_url,
        wiki_url=str(node.get("url") or ""),
    )
    return {
        "ok": True,
        "dry_run": False,
        "title": title,
        "invoice_count": len(invoices),
        "total": total,
        "document_id": doc_id,
        "node_token": node.get("node_token"),
        "document_url": document_url,
        "wiki_url": node.get("url"),
        "verification": verification,
        "source_manifest": str(manifest_path),
        "reference": reference_url,
        "raw_manifest_schema": raw.get("schema_version", 1),
    }


def canonical_document_url(doc_id: str, *, reference_url: str | None, wiki_url: str) -> str:
    """Keep the tenant host when one was supplied; otherwise use a generic link."""
    for candidate in (reference_url, wiki_url):
        if not candidate:
            continue
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/docx/{doc_id}"
    return f"https://feishu.cn/docx/{doc_id}"


def command_inspect(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir).expanduser().resolve()
    manifest = inspect_directory(input_dir, args.exclude)
    if args.contact_sheet:
        invoices = [Invoice(**item) for item in manifest["invoices"]]
        warning = render_contact_sheet(input_dir, invoices, Path(args.contact_sheet).expanduser().resolve())
        if warning:
            manifest["contact_sheet_warning"] = warning
        else:
            manifest["contact_sheet"] = str(Path(args.contact_sheet).expanduser().resolve())
    if args.out:
        write_json(Path(args.out).expanduser().resolve(), manifest)
    print(json_dump(manifest))
    return 0


def command_prepare(args: argparse.Namespace) -> int:
    raw, input_dir, invoices = load_manifest(
        Path(args.manifest).expanduser().resolve(),
        input_dir_override=Path(args.input_dir).expanduser().resolve() if args.input_dir else None,
    )
    if args.amounts:
        apply_amounts(invoices, [value for value in args.amounts.split(",") if value.strip()])
    require_complete_amounts(invoices)
    title = args.title or f"{input_dir.name} 发票汇总 {datetime.now().strftime('%Y%m%d')}"
    xml, total = build_document_xml(title, invoices, args.note)
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    enriched = dict(raw)
    enriched["input_dir"] = str(input_dir)
    enriched["amounts_complete"] = True
    enriched["total"] = total
    enriched["title"] = title
    enriched["invoices"] = [asdict(invoice) for invoice in invoices]
    write_json(out_dir / "manifest.enriched.json", enriched)
    (out_dir / "document.xml").write_text(xml + "\n", encoding="utf-8")
    print(json_dump({"ok": True, "manifest": str(out_dir / "manifest.enriched.json"), "xml": str(out_dir / "document.xml"), "invoice_count": len(invoices), "total": total, "title": title}))
    return 0


def command_publish(args: argparse.Namespace) -> int:
    result = publish(
        manifest_path=Path(args.manifest).expanduser().resolve(),
        input_dir_override=Path(args.input_dir).expanduser().resolve() if args.input_dir else None,
        title=args.title,
        note=args.note,
        reference_url=args.reference_url,
        space_id=args.space_id,
        parent_node_token=args.parent_node_token,
        identity=args.as_identity,
        apply=args.apply,
    )
    print(json_dump(result))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    _, _, invoices = load_manifest(
        Path(args.manifest).expanduser().resolve(),
        input_dir_override=Path(args.input_dir).expanduser().resolve() if args.input_dir else None,
    )
    require_complete_amounts(invoices)
    doc_id = extract_doc_id(args.doc)
    xml = fetch_document(doc_id, args.as_identity)
    print(json_dump({"ok": True, "document": args.doc, "verification": verify_document_xml(xml, invoices)}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="enumerate PDFs and create an amount-review manifest")
    inspect_parser.add_argument("--input-dir", required=True)
    inspect_parser.add_argument("--out", help="write manifest JSON to this path")
    inspect_parser.add_argument("--contact-sheet", help="optional first-page contact sheet output path")
    inspect_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="exclude a relative path or basename glob (repeatable), e.g. '_*.pdf'",
    )
    inspect_parser.set_defaults(handler=command_inspect)

    prepare_parser = subparsers.add_parser("prepare", help="validate amounts and render the DocxXML payload")
    prepare_parser.add_argument("--manifest", required=True)
    prepare_parser.add_argument("--input-dir", help="override input_dir in manifest")
    prepare_parser.add_argument("--amounts", help="comma-separated amounts in manifest order")
    prepare_parser.add_argument("--title")
    prepare_parser.add_argument("--note", default="发票汇总")
    prepare_parser.add_argument("--out-dir", required=True)
    prepare_parser.set_defaults(handler=command_prepare)

    publish_parser = subparsers.add_parser("publish", help="create a new Feishu Docx, upload, bind, and verify")
    publish_parser.add_argument("--manifest", required=True)
    publish_parser.add_argument("--input-dir", help="override input_dir in manifest")
    publish_parser.add_argument("--title", required=True)
    publish_parser.add_argument("--note", default="发票汇总")
    publish_parser.add_argument("--reference-url", help="existing Feishu wiki/doc URL; new doc uses its space/parent")
    publish_parser.add_argument("--space-id")
    publish_parser.add_argument("--parent-node-token")
    publish_parser.add_argument("--as", dest="as_identity", choices=("user", "bot"), default="user")
    publish_parser.add_argument("--apply", action="store_true", help="perform remote writes; omitted means dry-run")
    publish_parser.set_defaults(handler=command_publish)

    verify_parser = subparsers.add_parser("verify", help="re-fetch a published document and verify rows/attachments")
    verify_parser.add_argument("--doc", required=True, help="Docx document id or URL")
    verify_parser.add_argument("--manifest", required=True)
    verify_parser.add_argument("--input-dir", help="override input_dir in manifest")
    verify_parser.add_argument("--as", dest="as_identity", choices=("user", "bot"), default="user")
    verify_parser.set_defaults(handler=command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except InvoiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
