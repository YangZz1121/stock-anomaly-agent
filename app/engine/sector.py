"""口语板块 → 一级行业指数。只读本地清单，不访问行情接口。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

from app.config import Settings
from app.engine.industry import CatalogEntry, IndustryCatalog, load_catalog

_GROUPS_REL = os.path.join("market", "sector_groups.json")


@dataclass(frozen=True)
class SectorMatch:
    label: str
    entries: Tuple[CatalogEntry, ...]

    @property
    def names(self) -> List[str]:
        return [entry.name for entry in self.entries]


def resolve_sector_phrase(
    text: str, settings: Optional[Settings] = None
) -> Optional[SectorMatch]:
    raw = (text or "").strip()
    if not raw:
        return None
    cfg = settings or Settings()
    catalog = load_catalog(cfg)
    hit = _longest_group(raw) or _longest_industry(raw, catalog)
    if hit is None:
        return None
    label, names = hit
    entries = _entries_for_names(catalog, names)
    if not entries:
        return None
    return SectorMatch(label=label, entries=tuple(entries))


def _longest_group(text: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    best: Optional[Tuple[str, Tuple[str, ...]]] = None
    best_len = 0
    for group in _sector_groups():
        for phrase in group[0]:
            if phrase and phrase in text and len(phrase) > best_len:
                best = (group[1], group[2])
                best_len = len(phrase)
    return best


def _longest_industry(
    text: str, catalog: IndustryCatalog
) -> Optional[Tuple[str, Tuple[str, ...]]]:
    best_name = ""
    for name in catalog.by_name:
        if name and name in text and len(name) > len(best_name):
            best_name = name
    if not best_name:
        return None
    official = catalog.by_name[best_name].name
    return official, (official,)


def _entries_for_names(
    catalog: IndustryCatalog, names: Sequence[str]
) -> List[CatalogEntry]:
    out: List[CatalogEntry] = []
    seen = set()
    for name in names:
        entry = catalog.by_name.get(name)
        if entry is None or entry.thscode in seen:
            continue
        seen.add(entry.thscode)
        out.append(entry)
    return out


@lru_cache(maxsize=1)
def _sector_groups() -> Tuple[Tuple[Tuple[str, ...], str, Tuple[str, ...]], ...]:
    """((phrases...), label, industries...)"""
    path = _groups_path()
    if not os.path.exists(path):
        return ()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return ()
    groups = []
    for item in raw.get("groups") or []:
        label = (item.get("label") or "").strip()
        industries = tuple(
            name.strip() for name in (item.get("industries") or []) if name
        )
        if not label or not industries:
            continue
        phrases = [label, *(item.get("aliases") or [])]
        phrases = tuple(sorted({p.strip() for p in phrases if p}, key=len, reverse=True))
        groups.append((phrases, label, industries))
    return tuple(groups)


def _groups_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "fixtures", _GROUPS_REL)
