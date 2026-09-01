"""G2O/G2H P0 policy and bounded document inspection helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import re
import zipfile
from os import PathLike
from pathlib import Path
from typing import Iterable

from agent_governance.contracts import GateDecision


MAX_FILE_BYTES = 40 * 1024 * 1024
MAX_BATCH_FILES = 3
MAX_BATCH_BYTES = 120 * 1024 * 1024
MAX_UNPACKED_BYTES = 250 * 1024 * 1024
MAX_COMPRESSION_RATIO = 10
MAX_STATIC_OOXML_PART_BYTES = 2 * 1024 * 1024
MAX_SHEETS = 20
MAX_ROWS = 100_000
MAX_NONEMPTY_CELLS = 1_000_000
MAX_WORD_PAGES = 200
MAX_PPT_SLIDES = 100
MAX_IMAGES = 100
ALLOWED_EXTENSIONS = {".xlsx", ".docx", ".csv", ".pptx"}
OOXML_EXTENSIONS = {".xlsx", ".docx", ".pptx"}
# A normal Word file contains two vendor-generated style catalogues.  They
# are highly repetitive XML (often >20:1) even for a one-paragraph document,
# so counting them in the generic ZIP-bomb ratio rejects a valid D1 input.
# They are never executable and remain individually size-capped, DLP-scanned
# and included in the absolute 250 MB decompression cap.  User-controlled
# document, spreadsheet, relationship and media parts remain under the 10:1
# ratio, so this exception cannot be used to smuggle a large payload.
_DOCX_STATIC_STYLE_PARTS = {"word/styles.xml", "word/stylesWithEffects.xml"}

_SECRET = re.compile(r"(?i)(?:api[_-]?key|app[_-]?secret|token|password|authorization|bearer)\s*[:= ]\s*[\w.-]{8,}")
_CN_ID = re.compile(r"\b\d{17}[\dXx]\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


class OfficePolicyError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AssetInspection:
    filename: str
    extension: str
    detected_mime: str
    byte_size: int
    sha256: str
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    image_count: int = 0


def detect_mime(filename: str, content: bytes) -> str:
    extension = _extension(filename)
    if extension == ".csv":
        return "text/csv"
    if extension in OOXML_EXTENSIONS:
        if not content.startswith(b"PK\x03\x04"):
            raise OfficePolicyError("OFFICE_MAGIC_MISMATCH")
        if extension == ".xlsx":
            return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if extension == ".docx":
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    raise OfficePolicyError("OFFICE_UNSUPPORTED_TYPE")


def inspect_asset(filename: str, content: bytes, *, declared_size: int | None = None) -> AssetInspection:
    extension = _extension(filename)
    byte_size = int(declared_size if declared_size is not None else len(content))
    if not filename or extension not in ALLOWED_EXTENSIONS:
        raise OfficePolicyError("OFFICE_UNSUPPORTED_TYPE")
    if byte_size > MAX_FILE_BYTES:
        raise OfficePolicyError("OFFICE_FILE_TOO_LARGE")
    if _contains_strong_sensitive(content):
        raise OfficePolicyError("OFFICE_STRONG_SENSITIVE_DATA")
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        raise OfficePolicyError("OFFICE_OLE_FORBIDDEN")
    mime = detect_mime(filename, content)
    compressed = uncompressed = images = 0
    if extension in OOXML_EXTENSIONS:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                names = {entry.filename for entry in infos}
                if any(entry.flag_bits & 0x1 for entry in infos) or "EncryptedPackage" in names:
                    raise OfficePolicyError("OFFICE_ENCRYPTED_FORBIDDEN")
                required_part = {".xlsx": "xl/workbook.xml", ".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}[extension]
                if "[Content_Types].xml" not in names or required_part not in names:
                    raise OfficePolicyError("OFFICE_MAGIC_MISMATCH")
                if any("vbaProject.bin" in name or name.endswith(".bin") and "vba" in name.lower() for name in names):
                    raise OfficePolicyError("OFFICE_MACRO_FORBIDDEN")
                if any("externalLink" in name or "dde" in name.lower() or "/embeddings/" in name.lower() for name in names):
                    raise OfficePolicyError("OFFICE_EXTERNAL_REFERENCE_FORBIDDEN")
                compressed = sum(max(0, entry.compress_size) for entry in infos)
                uncompressed = sum(max(0, entry.file_size) for entry in infos)
                ratio_infos = infos
                if extension == ".docx":
                    static_parts = [entry for entry in infos if entry.filename in _DOCX_STATIC_STYLE_PARTS]
                    if any(entry.file_size > MAX_STATIC_OOXML_PART_BYTES for entry in static_parts):
                        raise OfficePolicyError("OFFICE_ARCHIVE_LIMIT_EXCEEDED")
                    ratio_infos = [entry for entry in infos if entry.filename not in _DOCX_STATIC_STYLE_PARTS]
                images = sum(1 for name in names if name.startswith("word/media/") or name.startswith("ppt/media/") or name.startswith("xl/media/"))
                ratio_compressed = sum(max(0, entry.compress_size) for entry in ratio_infos)
                ratio_uncompressed = sum(max(0, entry.file_size) for entry in ratio_infos)
                if uncompressed > MAX_UNPACKED_BYTES or (ratio_compressed and ratio_uncompressed / ratio_compressed > MAX_COMPRESSION_RATIO):
                    raise OfficePolicyError("OFFICE_ARCHIVE_LIMIT_EXCEEDED")
                if images > MAX_IMAGES:
                    raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")
                # Structural limits are enforced before promotion/worker
                # scheduling.  They intentionally use ZIP part names and
                # explicit Word page-break markers rather than invoking an
                # Office parser in the Web ingress process.
                if extension == ".xlsx" and sum(1 for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)) > MAX_SHEETS:
                    raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")
                if extension == ".pptx" and sum(1 for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)) > MAX_PPT_SLIDES:
                    raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")
                if extension == ".docx" and _count_entry_token(archive, "word/document.xml", b"w:type=\"page\"") + 1 > MAX_WORD_PAGES:
                    raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")
                # OOXML is a ZIP container.  Inspect text-bearing XML parts
                # before the asset is promoted so secrets or ID/card samples do
                # not bypass DLP simply because the outer file is compressed.
                for entry in infos:
                    if not entry.filename.endswith((".xml", ".rels")):
                        continue
                    # Do not create an implicit 4 MB DLP blind spot.  Read a
                    # ZIP entry in bounded chunks; the archive-level limits
                    # above still cap total input, while every text-bearing
                    # part is checked before a parser/model sees it.
                    _inspect_ooxml_text_part(archive, entry)
        except zipfile.BadZipFile as exc:
            raise OfficePolicyError("OFFICE_INVALID_ARCHIVE") from exc
    return AssetInspection(filename=filename, extension=extension, detected_mime=mime, byte_size=byte_size,
                           sha256=hashlib.sha256(content).hexdigest(), compressed_bytes=compressed,
                           uncompressed_bytes=uncompressed, image_count=images)


def _byte_size(content: bytes | str | PathLike[str]) -> int:
    return int(Path(content).stat().st_size) if isinstance(content, (str, PathLike)) else len(content)


def validate_batch_metadata(files: Iterable[tuple[str, bytes | str | PathLike[str]]], *, declared_sizes: Iterable[int] | None = None) -> tuple[list[tuple[str, bytes | str | PathLike[str]]], list[int]]:
    """Validate count and declared/on-disk sizes without loading a whole batch."""
    materialized = list(files)
    sizes = list(declared_sizes) if declared_sizes is not None else [_byte_size(content) for _name, content in materialized]
    if len(sizes) != len(materialized):
        raise OfficePolicyError("OFFICE_BATCH_SIZE_LIMIT_EXCEEDED")
    if not materialized or len(materialized) > MAX_BATCH_FILES:
        raise OfficePolicyError("OFFICE_BATCH_FILE_LIMIT_EXCEEDED")
    total = sum(int(size) for size in sizes)
    if total > MAX_BATCH_BYTES:
        raise OfficePolicyError("OFFICE_BATCH_SIZE_LIMIT_EXCEEDED")
    return materialized, [int(size) for size in sizes]


def inspect_batch(files: Iterable[tuple[str, bytes]], *, declared_sizes: Iterable[int] | None = None) -> list[AssetInspection]:
    materialized, sizes = validate_batch_metadata(files, declared_sizes=declared_sizes)
    # All checks run before storage promotion, therefore any error is atomic.
    return [inspect_asset(name, content, declared_size=int(size)) for (name, content), size in zip(materialized, sizes)]


def validate_content_limits(*, sheets: int = 0, rows: int = 0, nonempty_cells: int = 0,
                            pages: int = 0, slides: int = 0, images: int = 0) -> None:
    if sheets > MAX_SHEETS or rows > MAX_ROWS or nonempty_cells > MAX_NONEMPTY_CELLS:
        raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")
    if pages > MAX_WORD_PAGES or slides > MAX_PPT_SLIDES or images > MAX_IMAGES:
        raise OfficePolicyError("OFFICE_CONTENT_LIMIT_EXCEEDED")


def office_ingress_decision(*, enabled: bool, inspection_mode: bool, attachment_owned: bool) -> GateDecision:
    if inspection_mode:
        return GateDecision("G1", "BLOCK", "OFFICE_DISABLED_IN_INSPECTION_MODE")
    if not enabled:
        return GateDecision("G0", "BLOCK", "FEATURE_DISABLED")
    if not attachment_owned:
        return GateDecision("G2", "BLOCK", "OFFICE_ASSET_NOT_FOUND")
    return GateDecision("G2", "ALLOW", "OFFICE_ASSET_ALLOWED")


def research_brief_decision(brief: dict, *, enabled: bool, same_owner: bool) -> GateDecision:
    if not enabled:
        return GateDecision("G2", "BLOCK", "RESEARCH_TO_OFFICE_DISABLED")
    if not same_owner:
        return GateDecision("G2", "BLOCK", "RESEARCH_BRIEF_NOT_FOUND")
    required = {"brief_id", "producer_run_id", "as_of", "claims", "citations", "answer_status", "content_hash"}
    if not required.issubset(brief) or not brief.get("citations"):
        return GateDecision("G3", "BLOCK", "RESEARCH_BRIEF_INVALID")
    if brief.get("answer_status") not in {"VERIFIED", "PARTIALLY_VERIFIED"}:
        return GateDecision("G2", "BLOCK", "RESEARCH_BRIEF_STATUS_BLOCKED")
    claims = brief.get("claims") or []
    citations = {item.get("evidence_id") for item in brief.get("citations") or []}
    if not claims or any(not set(item.get("evidence_ids") or []).issubset(citations) for item in claims):
        return GateDecision("G3", "BLOCK", "RESEARCH_BRIEF_INVALID")
    return GateDecision("G2", "ALLOW", "RESEARCH_BRIEF_ALLOWED")


def escape_csv_formula(value: object) -> str:
    text = str(value if value is not None else "")
    return f"'{text}" if text[:1] in {"=", "+", "-", "@"} else text


def _extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _contains_strong_sensitive(content: bytes) -> bool:
    # Decode best-effort.  OOXML byte strings also expose textual cell/paragraph
    # content frequently enough for a conservative first-pass DLP gate.
    decoded = content.decode("utf-8", errors="ignore")
    return bool(_SECRET.search(decoded) or _CN_ID.search(decoded) or _CARD.search(decoded))


def _inspect_ooxml_text_part(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> None:
    """Scan an OOXML XML/relationship entry without materialising it in RAM."""
    tail = b""
    with archive.open(entry, "r") as source:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            sample = tail + chunk
            # Relationship/DDE/OLE checks must work even when the marker lies
            # across a chunk boundary.  A 1 KiB suffix is much longer than any
            # marker we accept here.
            if entry.filename.endswith(".rels") and b"TargetMode=\"External\"" in sample:
                raise OfficePolicyError("OFFICE_EXTERNAL_REFERENCE_FORBIDDEN")
            if b"oleObject" in sample or re.search(rb"(?i)<(?:[a-z0-9_]+:)?dde(?:\s|>)|\bDDE\s*\(", sample):
                raise OfficePolicyError("OFFICE_EXTERNAL_REFERENCE_FORBIDDEN")
            if _contains_strong_sensitive(sample):
                raise OfficePolicyError("OFFICE_STRONG_SENSITIVE_DATA")
            tail = sample[-1024:]


def _count_entry_token(archive: zipfile.ZipFile, entry_name: str, token: bytes) -> int:
    """Count a fixed XML marker across chunks without materialising an entry."""
    try:
        source = archive.open(entry_name, "r")
    except KeyError:
        return 0
    count = 0
    tail = b""
    with source:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            sample = tail + chunk
            count += sample.count(token)
            tail = sample[-max(1, len(token) - 1):]
    return count
