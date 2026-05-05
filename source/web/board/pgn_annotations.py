from __future__ import annotations

import re
from dataclasses import dataclass

from ...core.boardtools import normalize_figurine
from .contracts import Arrow, Circle


_CAL_RE = re.compile(r"\[%cal\s+([^\]]+)\]")
_CSL_RE = re.compile(r"\[%csl\s+([^\]]+)\]")
_PGN_COMMAND_RE = re.compile(r"\[%[^\]]+\]")

_COLOR_MAP = {
    "G": "green",
    "R": "red",
    "B": "blue",
    "Y": "yellow",
}


@dataclass(frozen=True)
class Annotations:
    arrows: list[Arrow]
    circles: list[Circle]


def parse_comment(comment: str) -> Annotations:
    """
    Parses Lichess-style PGN annotations embedded in comments:

    - Arrows:  [%cal Gc2c3,Rc3d4]
    - Circles: [%csl Ra3,Ga4]
    """

    arrows: list[Arrow] = []
    circles: list[Circle] = []

    for m in _CAL_RE.finditer(comment or ""):
        for token in (t.strip() for t in m.group(1).split(",")):
            if len(token) < 5:
                continue
            color = _COLOR_MAP.get(token[0].upper())
            if not color:
                continue
            orig = token[1:3]
            dest = token[3:5]
            arrows.append(Arrow(orig=orig, dest=dest, color=color))

    for m in _CSL_RE.finditer(comment or ""):
        for token in (t.strip() for t in m.group(1).split(",")):
            if len(token) < 3:
                continue
            color = _COLOR_MAP.get(token[0].upper())
            if not color:
                continue
            square = token[1:3]
            circles.append(Circle(square=square, color=color))

    return Annotations(arrows=arrows, circles=circles)


def comment_text(*comments: str) -> str:
    """
    Return human-readable PGN comment text with command tags removed.
    """

    parts: list[str] = []
    for raw_comment in comments:
        cleaned = _PGN_COMMAND_RE.sub("", raw_comment or "")
        cleaned = normalize_figurine(cleaned)
        lines = [line.strip() for line in cleaned.splitlines()]
        normalized_lines: list[str] = []
        previous_blank = True
        for line in lines:
            if not line:
                if not previous_blank:
                    normalized_lines.append("")
                previous_blank = True
                continue
            normalized_lines.append(line)
            previous_blank = False
        normalized = "\n".join(normalized_lines).strip()
        if normalized:
            parts.append(normalized)
    return "\n\n".join(parts)
