"""Address source text by page and character offsets."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


_BREAK = re.compile(r"\n[ \t]*\n+|(?<=[.!?])\s+(?=[A-ZА-ЯЁ])")


@dataclass(frozen=True)
class Fragment:
    page: int
    start: int
    end: int
    text: str

    @property
    def id(self) -> str:
        return f"P{self.page}-{self.start}"

    def location(self) -> dict:
        return {"page": self.page, "start": self.start, "end": self.end, "quote": self.text}


def source_fragments(pages: dict[int, str], max_chars: int = 600) -> dict[str, Fragment]:
    fragments = {}
    for page, text in pages.items():
        start = 0
        spans = []
        for match in _BREAK.finditer(text):
            spans.append((start, match.start()))
            start = match.end()
        spans.append((start, len(text)))
        for start, end in spans:
            while start < end:
                stop = min(start + max_chars, end)
                if stop < end:
                    split = max(text.rfind(" ", start, stop), text.rfind("\n", start, stop))
                    if split > start:
                        stop = split + 1
                excerpt = text[start:stop].strip()
                if excerpt:
                    left = start + len(text[start:stop]) - len(text[start:stop].lstrip())
                    fragment = Fragment(page, left, left + len(excerpt), excerpt)
                    fragments[fragment.id] = fragment
                start = stop
    return fragments


def render_fragments(fragments: dict[str, Fragment]) -> str:
    return "\n\n".join(f"[{fragment_id}] {fragment.text}" for fragment_id, fragment in fragments.items())


def _searchable(text: str) -> tuple[str, list[int]]:
    characters = []
    offsets = []
    for offset, character in enumerate(text):
        for value in unicodedata.normalize("NFKC", character).casefold():
            if value.isspace():
                value = " "
                if characters and characters[-1] == " ":
                    continue
            characters.append(value)
            offsets.append(offset)
    return "".join(characters), offsets


class EvidenceIndex:
    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.fragments = source_fragments(pages)
        self.searchable = {page: _searchable(text) for page, text in pages.items()}

    def matches(self, value: str, fragment: Fragment | None) -> list[Fragment]:
        target, _ = _searchable(value.strip())
        if not target:
            return []
        # Keep complete tokens and avoid citing pieces of decimals or signed numbers.
        numeric = re.fullmatch(r"[+\-−]?[0-9]+(?:[.,][0-9]+)?", target)
        left = r"(?<!\w)(?<![+\-−])(?<!\d[.,])" if numeric else r"(?<!\w)" if target[0].isalnum() else ""
        right = r"(?!\w|[.,]\d)" if numeric else r"(?!\w)" if target[-1].isalnum() else ""
        pattern = re.compile(left + re.escape(target) + right)
        results = []
        pages = (fragment.page,) if fragment else tuple(self.searchable)
        for page in pages:
            text, offsets = self.searchable[page]
            for match in pattern.finditer(text):
                start, end = offsets[match.start()], offsets[match.end() - 1] + 1
                if fragment and not (fragment.start <= start and end <= fragment.end):
                    continue
                results.append(Fragment(page, start, end, self.pages[page][start:end]))
        return results

    def locate(self, value: str, selected: Fragment | None) -> dict:
        if not value.strip():
            return {"status": "empty"}
        matches = self.matches(value, selected) if selected else []
        if len(matches) == 1:
            return {"status": "text_matched", "method": "selected_fragment", **matches[0].location()}
        matches = self.matches(value, None)
        if len(matches) == 1:
            return {"status": "text_matched", "method": "unique_in_article", **matches[0].location()}
        if not matches:
            return {"status": "not_found", "match_count": 0}
        result = {"status": "ambiguous", "match_count": len(matches)}
        pages = {match.page for match in matches}
        if len(pages) == 1:
            result["page"] = matches[0].page
            result["alternatives"] = [match.location() for match in matches[:5]]
        return result
