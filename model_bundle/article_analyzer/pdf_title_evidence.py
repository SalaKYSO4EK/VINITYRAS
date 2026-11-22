"""Verify document titles against the typography and glyphs on physical page one."""
import re
from pathlib import Path
from statistics import median


def printed_outline_title(page, path):
    """A sole enclosing bookmark can name the work even when /Title is a job number."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        with Path(path).open("rb") as stream:
            reader = PdfReader(stream)
            outline = reader.trailer["/Root"]["/Outlines"]["/First"]
            title = outline.get("/Title")
            if "/Next" in outline or "/First" not in outline or not isinstance(title, str):
                return None
            words = title.split()
            if not 5 <= len(words) <= 120:
                return None
            # The separate text reader supplies word boundaries and inline subscripts.
            # Require the entire title at the start of page one, where the glyph check
            # below independently locates the corresponding prominent printed heading.
            pattern = r"^\s*(" + r"\s+".join(map(re.escape, words)) + r")(?=\s|$)"
            match = re.match(pattern, reader.pages[0].extract_text() or "")
            if match:
                return printed_metadata_title(page, title, opening_fragment=match[1])
    except (PdfReadError, OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, NotImplementedError):
        pass
    return None


def printed_metadata_title(page, metadata, *, opening_fragment=None):
    if not isinstance(metadata, str) or len(metadata.split()) < 5:
        return None
    # PDF content order preserves chemical subscripts printed below the line.
    chars, text = [], []
    for char in page.chars:
        for letter in char["text"]:
            if not letter.isspace():
                text.append(letter)
                chars.append(char)
    printed = "".join(text)
    title = "".join(metadata.split())
    first = printed.find(title)
    if first < 0:
        return None
    if opening_fragment is not None and first != 0:
        return None
    if opening_fragment is None and printed.find(title, first + 1) >= 0:
        return None
    glyphs = chars[first:first + len(title)]
    size = median(c["size"] for c in glyphs)
    if not size or size < median(c["size"] for c in chars) * 1.17:
        return None
    left, right = min(c["x0"] for c in glyphs), max(c["x1"] for c in glyphs)
    top, bottom = min(c["top"] for c in glyphs), max(c["bottom"] for c in glyphs)
    if top < page.height * .045 or bottom > page.height * .66 or bottom - top > size * 12:
        return None
    # Reject a matching substring of a longer heading or a title in a running header.
    fonts = {c["fontname"] for c in glyphs if abs(c["size"] - size) < 1}
    for c in chars[:first] + chars[first + len(title):]:
        if (c["fontname"] in fonts and abs(c["size"] - size) < 1
                and c["bottom"] >= top - size * .65 and c["top"] <= bottom + size * .65):
            return None
        if (left < (c["x0"] + c["x1"]) / 2 < right
                and top < (c["top"] + c["bottom"]) / 2 < bottom):
            return None
    box = (left, top, right, bottom)
    crop = page.crop(box)
    fragment = opening_fragment or crop.extract_text(x_tolerance=1) or ""
    title_words = metadata.split()
    # Character order handles subscripts; rendered text supplies word boundaries
    # for PDFs that encode spaces only by distances between adjacent glyphs.
    glyph_text = "".join(c["text"] for c in crop.chars)
    if not fragment.strip() or (fragment.split() != title_words and glyph_text.split() != title_words):
        return None
    return {"title": " ".join(metadata.split()), "status": "extracted",
            "method": ("root_bookmark_verified_against_opening_heading" if opening_fragment is not None
                       else "metadata_verified_against_visible_glyphs"),
            "source_page": 1, "source_fragment": fragment, "source_bbox": [round(v, 3) for v in box]}
