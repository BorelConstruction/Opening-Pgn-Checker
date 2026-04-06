from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import Arrow, Circle


_CAL_RE = re.compile(r"\[%cal\s+([^\]]+)\]")
_CSL_RE = re.compile(r"\[%csl\s+([^\]]+)\]")

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

