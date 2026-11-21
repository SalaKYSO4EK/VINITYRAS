"""Source support for explicitly written, thickness-resolved device stacks."""
import re
import unicodedata

HYPHENS = str.maketrans('‐‑−–—', '-----')


def _compact_with_positions(text):
    compact = []
    positions = []
    for index, character in enumerate(text):
        normalized = unicodedata.normalize('NFKC', character).casefold().translate(HYPHENS)
        for value in normalized:
            if not value.isspace():
                compact.append(value)
                positions.append(index)
    return ''.join(compact), positions


def device_structure_evidence(label, pages):
    """Require the whole stack in one source span, without combining its parts.

    Other kinds of labels are outside this check's scope. Support for a stack
    establishes its identity only. Device attributes require their own evidence.
    """
    target, _ = _compact_with_positions(label)
    if target.count('/') < 3 or not re.search(r'\d(?:\.\d+)?nm', target):
        return {'status': 'not_applicable'}
    for page in pages:
        compact, positions = _compact_with_positions(page['text'])
        start = compact.find(target)
        if start >= 0:
            first = positions[start]
            last = positions[start + len(target) - 1] + 1
            return {'status': 'supported', 'source_page': page['page'],
                    'source_fragment': page['text'][first:last],
                    'method': 'whole_structure_literal'}
    return {'status': 'unconfirmed', 'reason': 'Whole layer sequence and thicknesses were not found in a source span'}


def filter_device_structures(labels, pages):
    """Reject unsupported full stacks and keep the check result for every label."""
    accepted = []
    checks = []
    for label in labels:
        evidence = device_structure_evidence(label, pages)
        checks.append({'device_label': label, **evidence})
        if evidence['status'] != 'unconfirmed':
            accepted.append(label)
    return accepted, checks
