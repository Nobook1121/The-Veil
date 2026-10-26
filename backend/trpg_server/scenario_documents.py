"""Structured scenario document parsing and deterministic chunking."""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

try:
    # PyMuPDF 1.24+ exposes the supported module name ``pymupdf``.  Importing
    # the historical ``fitz`` alias emits a deprecation warning at server
    # startup, so keep it only as a compatibility fallback for older installs.
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None


SUPPORTED_SCENARIO_EXTENSIONS = {".doc", ".docx", ".pdf", ".txt", ".md", ".markdown", ".text"}


class ScenarioDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class DocumentBlock:
    text: str
    page: int | None = None
    heading_level: int | None = None
    chapter_path: list[str] | None = None


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    markdown: str
    blocks: list[DocumentBlock]
    page_count: int | None = None


@dataclass(frozen=True)
class DocumentChunk:
    chunk_id: str
    text: str
    page: int | None = None
    page_end: int | None = None
    chapter_path: list[str] | None = None


def validate_scenario_upload(filename: str, size: int, max_bytes: int) -> str:
    suffix = Path(filename or "").suffix.casefold()
    if suffix not in SUPPORTED_SCENARIO_EXTENSIONS:
        raise ScenarioDocumentError("仅支持 DOC、DOCX、PDF、TXT 和 Markdown 文档")
    if size <= 0:
        raise ScenarioDocumentError("文件为空")
    if size > max_bytes:
        raise ScenarioDocumentError(f"文件超过 {max_bytes // 1024 // 1024} MiB 上限")
    return suffix


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _heading(line: str) -> tuple[int, str] | None:
    match = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
    if match:
        return len(match.group(1)), match.group(2).strip()
    match = re.match(r"^\s*第\s*[一二三四五六七八九十百0-9]+\s*[章节幕场景、:.：]\s*(.+?)\s*$", line)
    return (1, match.group(1).strip()) if match else None


def _text_document(raw: bytes, filename: str) -> ParsedDocument:
    text = _clean(raw.decode("utf-8-sig", errors="replace"))
    if not text:
        raise ScenarioDocumentError("文件为空")
    blocks: list[DocumentBlock] = []
    path: list[str] = []
    for line in text.splitlines():
        heading = _heading(line)
        if heading:
            level, title = heading
            path = path[: level - 1] + [title]
            blocks.append(DocumentBlock(line.strip(), None, level, list(path)))
        elif line.strip():
            blocks.append(DocumentBlock(line.strip(), None, None, list(path)))
    return ParsedDocument(filename, text, blocks, None)


def _docx_document(raw: bytes, filename: str) -> ParsedDocument:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (KeyError, OSError, ValueError, ElementTree.ParseError) as exc:
        raise ScenarioDocumentError("Unable to read docx document") from exc
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines: list[str] = []
    for paragraph in root.findall(".//w:body/w:p", ns):
        value = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        if not value:
            continue
        style = paragraph.find("./w:pPr/w:pStyle", ns)
        style_name = style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "") if style is not None else ""
        if style_name.casefold().startswith("heading"):
            level = re.search(r"(\d+)", style_name)
            value = "#" * min(int(level.group(1)) if level else 1, 6) + " " + value
        lines.append(value)
    for table in root.findall(".//w:tbl", ns):
        for row in table.findall("./w:tr", ns):
            cells = ["".join(node.text or "" for node in cell.findall(".//w:t", ns)).strip() for cell in row.findall("./w:tc", ns)]
            if any(cells):
                lines.append(" | ".join(cells))
    return _text_document("\n".join(lines).encode("utf-8"), filename)


def _pdf_document(raw: bytes, filename: str, ocr_provider=None) -> ParsedDocument:
    if fitz is None:
        raise ScenarioDocumentError("PDF support requires PyMuPDF")
    try:
        document = fitz.open(stream=raw, filetype="pdf")
        pages: list[str] = []
        blocks: list[DocumentBlock] = []
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text").strip()
            if not text and ocr_provider is not None:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    rows = ocr_provider.extract(pix.tobytes("png"))
                    text = "\n".join(str(row.get("text", "")) for row in rows if row.get("text"))
                except Exception:
                    text = ""
            if not text:
                continue
            pages.append(text)
            for line in text.splitlines():
                line = line.strip()
                if line:
                    heading = _heading(line)
                    blocks.append(DocumentBlock(line, page_number, heading[0] if heading else None, []))
        if not "".join(pages).strip():
            raise ScenarioDocumentError("PDF appears scanned; OCR is required")
        parsed = _text_document("\n\n".join(pages).encode("utf-8"), filename)
        return ParsedDocument(filename, parsed.markdown, [DocumentBlock(b.text, blocks[i].page if i < len(blocks) else None, b.heading_level, b.chapter_path) for i, b in enumerate(parsed.blocks)], len(document))
    except ScenarioDocumentError:
        raise
    except Exception as exc:
        raise ScenarioDocumentError("Unable to read PDF document") from exc


def _doc_document(raw: bytes, filename: str) -> ParsedDocument:
    """旧版 .doc 走 scenario_importer 的 OLE 解析；直接当文本解码会得到乱码。"""
    from trpg_server.scenario_importer import extract_script_text

    try:
        text = extract_script_text(raw, filename)
    except ValueError as exc:
        raise ScenarioDocumentError(str(exc)) from exc
    document = _text_document(text.encode("utf-8"), Path(filename).with_suffix(".txt").name)
    return ParsedDocument(filename=filename, markdown=document.markdown, blocks=document.blocks, page_count=None)


def parse_scenario_document(raw: bytes, filename: str, ocr_provider=None) -> ParsedDocument:
    validate_scenario_upload(filename, len(raw), max(len(raw), 1))
    suffix = Path(filename).suffix.casefold()
    if suffix == ".pdf":
        return _pdf_document(raw, filename, ocr_provider=ocr_provider)
    if suffix == ".docx":
        return _docx_document(raw, filename)
    if suffix == ".doc":
        return _doc_document(raw, filename)
    return _text_document(raw, filename)


def chunk_parsed_document(document: ParsedDocument, target_min: int = 800, target_max: int = 800, overlap: int = 150) -> list[DocumentChunk]:
    """按标题把文档切分为语义块（一个场景/章节一个块）。

    直接导入要求「一个场景一个块」，Word/PDF 的标题（转为 Markdown ``#``）与
    ``第X章`` 等标题行即场景/章节边界，因此遇到标题就开启新块；只有正文超过
    ``target_max`` 时才继续拆分超大块。``target_min``/``overlap`` 保留以兼容
    既有调用签名。

    ``target_max`` 是「子块」上限：同一章节被拆出的多个子块共享同一
    ``chapter_path``，检索命中后可据此回填整章上下文。
    """
    if not document.markdown.strip():
        return []
    chunks: list[DocumentChunk] = []
    current: list[str] = []
    section_title: str | None = None

    def flush() -> None:
        if not current:
            return
        marker = section_title.lstrip("#").strip() if section_title else ""
        chapter_path = [marker] if marker else []
        text = "\n".join(current).strip()
        if text:
            first_line = text.splitlines()[0].lstrip("#").strip()
            # 被继续切分的续块不含标题行，补上所属章节，保证每块自带语境。
            if marker and not first_line.startswith(marker[:12]):
                text = f"【{marker}】\n{text}"
            chunks.append(DocumentChunk(f"chunk-{len(chunks)+1:04d}", text, None, None, chapter_path))
        current.clear()

    for line in document.markdown.splitlines():
        # 标题行作为新的场景/章节起点；首个标题之前的内容自成一块。
        if _heading(line):
            flush()
            section_title = line.strip()
        current.append(line)
        if len("\n".join(current)) >= target_max:
            # 超过上限时优先在最近的空行（自然段边界）处切分，避免把句子拦腰截断。
            split_at = None
            for index in range(len(current) - 1, 0, -1):
                if not current[index].strip():
                    split_at = index
                    break
            if split_at is not None and split_at >= len(current) // 3:
                tail = current[split_at + 1:]
                del current[split_at:]
                flush()
                current.extend(tail)
            else:
                flush()
    flush()
    return chunks
