"""Assemble a Document from the ingest stages. (Phase 2)

    paper.pdf -> pdf.extract_pages -> lines (line numbers, rows, headers/footers) -> sections.detect_heading
              -> lines.group_blocks / build_text -> sections, captions, tables, figures, mentions,
                 checklist, limitations
    code/     -> code_index.build_code_index

Pipeline code reads only `<data_root>/papers/<id>/`. The document is written to
`$ONUS_VAR_DIR/docs/<id>.json`.
"""

from __future__ import annotations

import re
from pathlib import Path

from onus.ingest import checklist as checklist_mod
from onus.ingest import code_index as code_mod
from onus.ingest import figures as figures_mod
from onus.ingest import limitations as lim_mod
from onus.ingest import lines as L
from onus.ingest import refs as refs_mod
from onus.ingest import sections as sec_mod
from onus.ingest import tables as tab_mod
from onus.ingest.pdf import BBox, extract_pages, pdf_metadata_title, sha256_file
from onus.schemas.document import Document, Line, Paragraph

_INNER_CAPTION = re.compile(r"(?<= )(?:Figure|Fig\.|Table)\s*\d{1,3}[a-z]?\s*:")
_EQ_TAG = re.compile(r"\((\d{1,3})\)\s*$")


def safe_id(paper_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", paper_id)


def _mark_title(rows: list[L.Row], layout: L.Layout, page_height: float) -> None:
    """Page-1 rows set clearly larger than body text, above the first heading, are the title."""
    for r in rows:
        if r.kind == "heading":
            break
        if r.size >= layout.body_size + 3 and r.y0 < 0.45 * page_height:
            r.kind = "title"
        elif any(x.kind == "title" for x in rows) and r.size < layout.body_size + 3:
            break


def _detect_headings(pages_rows: list[list[L.Row]], layout: L.Layout) -> None:
    after_refs = False
    in_checklist = False
    for rows in pages_rows:
        for r in rows:
            if r.kind != "text":
                continue
            h = sec_mod.detect_heading(r, layout, after_references=after_refs)
            if h is None:
                continue
            role = h.known_role or sec_mod.role_for(h.title)
            if in_checklist and h.known_role is None:
                continue  # numbered checklist items are not sections
            r.kind = "heading"
            r.heading = h
            if role == "references":
                after_refs = True
            in_checklist = role in ("checklist", "ai_checklist")


def _union(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
            max(b[3] for b in boxes))


def _overlaps(a: BBox, b: BBox) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 0 and min(a[3], b[3]) - max(a[1], b[1]) > 0


def build_document(pdf_path: Path, *, paper_id: str, source: str, code_dir: Path | None = None,
                   var_dir: Path | None = None) -> Document:
    warnings: list[str] = []
    pages = extract_pages(pdf_path)
    if not pages:
        raise ValueError(f"{pdf_path}: no pages")
    layout = L.estimate_layout(pages)

    stripped = [L.strip_line_numbers(page, layout) for page in pages]
    valid = L.validate_numbering([nums for _, nums in stripped])
    pages_rows: list[list[L.Row]] = []
    numbers: list[list[L.LineNumber]] = []
    for page, (text_lines, nums), ok in zip(pages, stripped, valid, strict=True):
        if nums and not ok:  # not review numbering: the digits are page text
            text_lines, nums = page.lines, []
        pages_rows.append(L.merge_rows(text_lines, page))
        numbers.append(nums)
    pages_rows = L.remove_headers_footers(pages_rows, [p.height for p in pages])

    _mark_title(pages_rows[0], layout, pages[0].height)
    _detect_headings(pages_rows, layout)
    for rows, nums in zip(pages_rows, numbers, strict=True):
        L.assign_line_numbers(rows, nums, layout)

    blocks = L.group_blocks(pages_rows, layout)
    built = L.build_text(blocks)
    text = built.text
    title = built.title or pdf_metadata_title(pdf_path) or (text.split("\n", 1)[0][:200] if text else "")

    headings = [(h.row.heading, h.start) for h in built.headings]
    sections = sec_mod.build_sections(headings, len(text))  # type: ignore[arg-type]

    # captions: a caption paragraph stops at the first row inside a located table grid
    captions: list[tab_mod.Caption] = []
    caption_starts: set[int] = set()
    text_blocks = [b for b in blocks if b.kind == "text"]
    for block, para in zip(text_blocks, built.paragraphs, strict=True):
        kind_id = tab_mod.caption_of(text[para.start : para.end])
        if not kind_id:
            continue
        grids = pages[block.page - 1].table_boxes
        rows = []
        for r in block.rows:
            if rows and any(_overlaps(r.bbox, g) for g in grids):
                break
            rows.append(r)
        cap_text = L.normalise(" ".join(r.text for r in rows))
        bbox = _union([r.bbox for r in rows])
        # side-by-side captions share a row: "Figure 1: ... Figure 2: ..." -> two captions
        cuts = [m.start() for m in _INNER_CAPTION.finditer(cap_text)]
        pieces = [cap_text[a:b].strip() for a, b in zip([0, *cuts], [*cuts, len(cap_text)], strict=True)]
        for piece in pieces:
            k = tab_mod.caption_of(piece)
            if k:
                captions.append(tab_mod.Caption(kind=k[0], id=k[1], text=piece, page=block.page, bbox=bbox))
        caption_starts.add(para.start)

    tables, tw = tab_mod.extract_tables(pages, captions, (layout.body_left, layout.body_right))
    warnings += tw
    figures = figures_mod.extract_figures(captions)
    eq_tags = {m.group(1) for ln in built.lines if (m := _EQ_TAG.search(ln.text))}
    mentions = refs_mod.find_mentions(text, tables, figures, sections, caption_starts, eq_tags)
    checklist = checklist_mod.parse_checklist(text, sections)
    ai_checklist = checklist_mod.parse_ai_checklist(text, sections)

    paragraphs = [Paragraph(page=p.page, index=p.index, start=p.start, end=p.end, sentences=p.sentences)
                  for p in built.paragraphs]
    limitations = lim_mod.find_limitations(text, sections, paragraphs)

    work = (var_dir / "work" / safe_id(paper_id) / "code") if var_dir else Path("/nonexistent")
    raw_text = "\n".join(" ".join(w.text for w in p.words) for p in pages)  # incl. removed header/footer rows
    code, cw = code_mod.build_code_index(code_dir, work, text + "\n" + raw_text)
    warnings += cw

    roles = {s.role for s in sections}
    if "abstract" not in roles:
        warnings.append("no Abstract heading found")
    if "references" not in roles:
        warnings.append("no References heading found")
    if len(sections) < 3:
        warnings.append(f"only {len(sections)} sections found")
    if "checklist" in roles and not checklist:
        warnings.append("checklist heading found but no items parsed")

    return Document(
        paper_id=paper_id, source=source, pdf_sha256=sha256_file(pdf_path), title=title, pages=len(pages),
        text=text,
        lines=[Line(page=ln.page, line_no=ln.line_no, text=ln.text, bbox=ln.bbox, start=ln.start, end=ln.end)
               for ln in built.lines],
        paragraphs=paragraphs, sections=sections, tables=tables, figures=figures, mentions=mentions,
        checklist=checklist, ai_checklist=ai_checklist, limitations_spans=limitations, code=code,
        warnings=warnings,
    )


# ---------------------------------------------------------------- paper directories and storage


def ingest_paper_dir(paper_dir: Path, var_dir: Path, *, source: str = "agents4science",
                     paper_id: str | None = None) -> Document:
    """`<data_root>/papers/<forum_id>/` (paper.pdf + optional code/) -> Document."""
    pdf = paper_dir / "paper.pdf"
    if not pdf.is_file():
        raise FileNotFoundError(f"{paper_dir}: no paper.pdf")
    code = paper_dir / "code"
    return build_document(pdf, paper_id=paper_id or paper_dir.name, source=source,
                          code_dir=code if code.is_dir() else None, var_dir=var_dir)


def ingest_pdf(pdf: Path, var_dir: Path, *, source: str = "upload", paper_id: str | None = None) -> Document:
    if paper_id is None:
        paper_id = f"upload:{sha256_file(pdf)[:12]}"
    return build_document(pdf, paper_id=paper_id, source=source, code_dir=None, var_dir=var_dir)


def doc_path(var_dir: Path, paper_id: str) -> Path:
    return var_dir / "docs" / f"{safe_id(paper_id)}.json"


def save_document(doc: Document, var_dir: Path) -> Path:
    p = doc_path(var_dir, doc.paper_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(doc.model_dump_json())
    tmp.replace(p)
    return p


def load_document(path: Path) -> Document:
    return Document.model_validate_json(path.read_text())


def paper_dirs(data_root: Path) -> list[Path]:
    root = data_root / "papers"
    return sorted((p for p in root.iterdir() if p.is_dir() and (p / "paper.pdf").is_file()),
                  key=lambda p: p.name.lower()) if root.is_dir() else []


# ---------------------------------------------------------------- ingest report (for the author)


def ingest_stats(doc: Document) -> dict:
    """Per-paper diagnostics for docs/ingest_report.md. Not used by any pipeline stage."""
    numbered_pages = {ln.page for ln in doc.lines if ln.line_no is not None}
    # Floats (table grids and captions) carry no review line numbers in the template: measure body text only.
    caption_spans = [(p.start, p.end) for p in doc.paragraphs if tab_mod.caption_of(doc.text[p.start : p.end])]

    def in_float(ln: Line) -> bool:
        cx, cy = (ln.bbox[0] + ln.bbox[2]) / 2, (ln.bbox[1] + ln.bbox[3]) / 2
        if any(t.page == ln.page and t.parsed and t.bbox[0] <= cx <= t.bbox[2] and t.bbox[1] <= cy <= t.bbox[3]
               for t in doc.tables):
            return True
        if any(s <= ln.start < e for s, e in caption_spans):
            return True
        # display maths is not numbered by the review template either
        words = re.findall(r"[A-Za-z]{4,}", ln.text)
        return len(words) < 4 and bool(re.search(r"[=∑∫√←⊙∥∼≈⊤�^_{}]|\(\d+\)$", ln.text))

    on_numbered = [ln for ln in doc.lines if ln.page in numbered_pages and not in_float(ln)]
    with_no = sum(1 for ln in on_numbered if ln.line_no is not None)
    roles = [s.role for s in doc.sections]
    loc = L.Locator(doc)
    covered = sum(1 for p in doc.paragraphs for s, e in p.sentences if loc.locate(s, e) is not None)
    total_sent = sum(len(p.sentences) for p in doc.paragraphs)
    return {
        "paper_id": doc.paper_id,
        "pages": doc.pages,
        "numbered_pages": len(numbered_pages),
        "line_no_coverage": (with_no / len(on_numbered)) if on_numbered else None,
        "lines": len(doc.lines),
        "paragraphs": len(doc.paragraphs),
        "sentences": total_sent,
        "sentences_located": covered,
        "sections": len(doc.sections),
        "top_sections": [f"{s.number + ' ' if s.number else ''}{s.title} [{s.role}]" for s in doc.sections
                         if s.level == 1],
        "has_abstract": "abstract" in roles,
        "has_references": "references" in roles,
        "tables": len(doc.tables),
        "tables_parsed": sum(1 for t in doc.tables if t.parsed),
        "figures": len(doc.figures),
        "mentions": len(doc.mentions),
        "mentions_resolved": sum(1 for m in doc.mentions if m.resolved_id),
        "checklist_items": len(doc.checklist),
        "checklist_answers": {c.key: c.answer for c in doc.checklist},
        "ai_checklist_items": len(doc.ai_checklist),
        "limitations_found": bool(doc.limitations_spans),
        "code_present": doc.code.present,
        "code_files": len(doc.code.files),
        "code_source_files": sum(1 for f in doc.code.files if f["is_source"]),
        "code_languages": doc.code.languages,
        "repo_urls": len(doc.code.repo_urls_in_paper),
        "warnings": doc.warnings,
    }


def render_report(stats: list[dict], failures: dict[str, str]) -> str:
    def pct(x: float | None) -> str:
        return "—" if x is None else f"{100 * x:.0f}%"

    out = ["# Ingest report", "",
           f"{len(stats)} papers ingested, {len(failures)} failed.", "",
           ("| paper | pages | numbered pages | line-no coverage | ¶ | sentences located | sections | abstract | "
            "refs | tables (parsed) | figures | mentions (resolved) | checklist | AI checklist | limitations | "
            "code files (source) |"),
           "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |"]
    for s in stats:
        out.append(
            f"| {s['paper_id']} | {s['pages']} | {s['numbered_pages']} | {pct(s['line_no_coverage'])} | "
            f"{s['paragraphs']} | {s['sentences_located']}/{s['sentences']} | {s['sections']} | "
            f"{'yes' if s['has_abstract'] else '**no**'} | {'yes' if s['has_references'] else '**no**'} | "
            f"{s['tables']} ({s['tables_parsed']}) | {s['figures']} | {s['mentions']} ({s['mentions_resolved']}) | "
            f"{s['checklist_items']} | {s['ai_checklist_items']} | {'yes' if s['limitations_found'] else 'no'} | "
            f"{s['code_files']} ({s['code_source_files']}) |")
    for pid, err in failures.items():
        out.append(f"| {pid} | **failed**: {err} |" + " |" * 14)
    out += ["", "## Per paper", ""]
    for s in stats:
        out.append(f"### {s['paper_id']}")
        out.append("")
        out.append("- Sections: " + "; ".join(s["top_sections"]))
        answers = ", ".join(f"{k}={v or '?'}" for k, v in s["checklist_answers"].items()) or "none"
        out.append(f"- Checklist answers: {answers}")
        langs = ", ".join(f"{k} {v} lines" for k, v in s["code_languages"].items()) or "none"
        out.append(f"- Code languages: {langs}; repo URLs in paper: {s['repo_urls']}")
        if s["warnings"]:
            out.append("- Warnings: " + "; ".join(s["warnings"]))
        out.append("")
    return "\n".join(out)
