"""Figure captions. (Phase 2)

A figure is identified by its caption paragraph ("Figure 3:", "Fig. 3."). Sub-panel letters ("Figure 3a") map
to the parent figure id. Only the first caption per id is kept (later ones are usually repeats in an appendix).
"""

from __future__ import annotations

from onus.ingest.tables import Caption
from onus.schemas.document import Figure


def extract_figures(captions: list[Caption]) -> list[Figure]:
    seen: set[str] = set()
    out: list[Figure] = []
    for c in captions:
        if c.kind != "figure" or c.id in seen:
            continue
        seen.add(c.id)
        out.append(Figure(id=c.id, caption=c.text, page=c.page))
    return out
