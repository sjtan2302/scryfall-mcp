"""Turn Scryfall card objects into compact text, and parse decklists.

Scryfall card objects run to several kilobytes each. Everything here exists to
hand the model only the fields it actually reasons about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

Card = dict[str, Any]


# -- cards -----------------------------------------------------------------


def faces(card: Card) -> list[Card]:
    """The card's faces; a single-faced card is treated as one face."""
    return card.get("card_faces") or [card]


def _stats(face: Card) -> str:
    if face.get("power") is not None and face.get("toughness") is not None:
        return f"{face['power']}/{face['toughness']}"
    if face.get("loyalty") is not None:
        return f"Loyalty {face['loyalty']}"
    if face.get("defense") is not None:
        return f"Defense {face['defense']}"
    return ""


def _face_header(face: Card, fallback: Card) -> str:
    name = face.get("name", "?")
    cost = face.get("mana_cost") or ""
    type_line = face.get("type_line") or fallback.get("type_line") or ""
    parts = [f"{name} {cost}".strip(), type_line, _stats(face)]
    return " — ".join(p for p in parts if p)


def oracle_text(card: Card) -> str:
    """Oracle text, with each face labelled on multi-faced cards."""
    card_faces = faces(card)
    if len(card_faces) == 1:
        return card_faces[0].get("oracle_text") or ""
    blocks = []
    for face in card_faces:
        text = face.get("oracle_text") or ""
        blocks.append(f"[{_face_header(face, card)}]\n{text}".rstrip())
    return "\n".join(blocks)


def usd_price(card: Card) -> float | None:
    """Cheapest available USD price across finishes, if Scryfall has one."""
    prices = card.get("prices") or {}
    values = []
    for key in ("usd", "usd_foil", "usd_etched"):
        try:
            values.append(float(prices[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return min(values) if values else None


def _money(value: float | None) -> str:
    return f"${value:,.2f}" if value is not None else "no price"


def format_summary(card: Card, *, include_text: bool = True) -> str:
    """One card in a line or two, for search results."""
    card_faces = faces(card)
    if len(card_faces) == 1:
        header = _face_header(card, card)
    else:
        header = " — ".join(p for p in (card.get("name", "?"), card.get("type_line") or "") if p)
        cost = card.get("mana_cost") or " // ".join(f.get("mana_cost") or "" for f in card_faces).strip(" /")
        if cost:
            header = header.replace(card.get("name", "?"), f"{card.get('name', '?')} {cost}", 1)

    meta = " · ".join(
        p for p in ((card.get("set") or "").upper(), card.get("rarity") or "", _money(usd_price(card))) if p
    )
    line = f"{header} [{meta}]"
    if not include_text:
        return line
    text = oracle_text(card)
    if not text:
        return line
    indented = "\n".join(f"  {row}" for row in text.splitlines())
    return f"{line}\n{indented}"


def format_legalities(card: Card) -> list[str]:
    by_status: dict[str, list[str]] = {}
    for fmt, status in (card.get("legalities") or {}).items():
        by_status.setdefault(status, []).append(fmt)
    lines = []
    for status, label in (("legal", "Legal"), ("restricted", "Restricted"), ("banned", "Banned")):
        if by_status.get(status):
            lines.append(f"{label}: {', '.join(sorted(by_status[status]))}")
    if not lines:
        lines.append("Legal: nowhere")
    return lines


def format_prices(card: Card) -> str:
    prices = card.get("prices") or {}
    labels = (("usd", "$", ""), ("usd_foil", "$", " foil"), ("usd_etched", "$", " etched"), ("eur", "€", ""), ("tix", "", " tix"))
    parts = [f"{prefix}{prices[key]}{suffix}" for key, prefix, suffix in labels if prices.get(key)]
    return " · ".join(parts) if parts else "none listed"


def format_detail(card: Card) -> str:
    """Everything the model is likely to need about one card."""
    card_faces = faces(card)
    lines: list[str] = []

    if len(card_faces) == 1:
        lines.append(f"{card.get('name', '?')} {card.get('mana_cost') or ''}".strip())
        lines.append(card.get("type_line") or "")
        if card.get("oracle_text"):
            lines.append(card["oracle_text"])
        if _stats(card):
            lines.append(_stats(card))
    else:
        lines.append(f"{card.get('name', '?')} ({card.get('layout', 'multi-faced')})")
        for face in card_faces:
            lines.append("")
            lines.append(f"{face.get('name', '?')} {face.get('mana_cost') or ''}".strip())
            lines.append(face.get("type_line") or card.get("type_line") or "")
            if face.get("oracle_text"):
                lines.append(face["oracle_text"])
            if _stats(face):
                lines.append(_stats(face))

    lines.append("")
    identity = "".join(card.get("color_identity") or []) or "colorless"
    facts = [f"Mana value: {card.get('cmc', 0):g}", f"Color identity: {identity}"]
    if card.get("keywords"):
        facts.append(f"Keywords: {', '.join(card['keywords'])}")
    lines.append(" | ".join(facts))

    printing = f"{card.get('set_name', '?')} ({(card.get('set') or '?').upper()}) #{card.get('collector_number', '?')}"
    lines.append(f"Printing: {printing} · {card.get('rarity', '?')} · {card.get('released_at', '?')}")
    lines.append(f"Prices: {format_prices(card)}")
    lines.extend(format_legalities(card))

    extras = []
    if card.get("reserved"):
        extras.append("Reserved List")
    if card.get("game_changer"):
        extras.append("Commander Game Changer")
    if card.get("edhrec_rank"):
        extras.append(f"EDHREC rank #{card['edhrec_rank']}")
    if extras:
        lines.append(" · ".join(extras))

    if card.get("scryfall_uri"):
        lines.append(f"Scryfall: {card['scryfall_uri'].split('?')[0]}")
    image = (card.get("image_uris") or card_faces[0].get("image_uris") or {}).get("normal")
    if image:
        lines.append(f"Image: {image}")
    return "\n".join(lines)


def lookup_keys(card: Card) -> set[str]:
    """Names a decklist line might use for this card, normalised."""
    keys = {normalize_name(card.get("name", ""))}
    for face in card.get("card_faces") or []:
        keys.add(normalize_name(face.get("name", "")))
    keys.discard("")
    return keys


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("’", "'")).strip().lower()


def front_face_name(name: str) -> str:
    """'Fire // Ice' or MTGO's 'Fire/Ice' -> 'Fire'.

    Scryfall's batch lookup matches multi-faced cards by their front face only.
    """
    return re.split(r"\s*/+\s*", name.strip(), maxsplit=1)[0]


def deck_key(name: str) -> str:
    """The key a decklist line is matched to a card under."""
    return normalize_name(front_face_name(name))


# -- decklists -------------------------------------------------------------


@dataclass
class DeckEntry:
    quantity: int
    name: str
    section: str


_SECTION_HEADERS = {
    "deck": "Main",
    "main": "Main",
    "maindeck": "Main",
    "mainboard": "Main",
    "sideboard": "Sideboard",
    "side": "Sideboard",
    "commander": "Commander",
    "companion": "Companion",
    "maybeboard": "Maybeboard",
    "considering": "Maybeboard",
}

_LINE = re.compile(r"^(?:(\d+)\s*x?\s+)?(.+)$", re.IGNORECASE)
# Arena/Moxfield exports append "(SET) 123" and sometimes foil markers.
_PRINTING_SUFFIX = re.compile(r"\s+[(\[][A-Za-z0-9]{2,6}[)\]](\s+\S+)?(\s+\*\w+\*)*\s*$")
_FOIL_SUFFIX = re.compile(r"(\s+\*\w+\*)+\s*$")


def parse_decklist(text: str) -> list[DeckEntry]:
    """Parse the common plain-text decklist formats (MTGO, Arena, Moxfield)."""
    entries: list[DeckEntry] = []
    groups: list[int] = []  # blank-line-separated block each entry sits in
    section = "Main"
    explicit_sections = False
    group = 0
    pending_blank = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            pending_blank = bool(entries)
            continue
        if line.startswith(("#", "//")):
            continue

        header = _SECTION_HEADERS.get(line.rstrip(":").strip().lower())
        if header:
            section = header
            explicit_sections = True
            continue

        line_section = section
        if line.lower().startswith("sb:"):
            line, line_section = line[3:].strip(), "Sideboard"
            explicit_sections = True

        match = _LINE.match(line)
        if not match:
            continue
        quantity = int(match.group(1) or 1)
        name = _FOIL_SUFFIX.sub("", _PRINTING_SUFFIX.sub("", match.group(2))).strip()
        if not name or quantity <= 0:
            continue

        if pending_blank:
            group += 1
            pending_blank = False
        entries.append(DeckEntry(quantity, name, line_section))
        groups.append(group)

    # MTGO exports mark the sideboard only with a blank line. Infer that when
    # the list has exactly two blocks and the second is sideboard-sized, so
    # lists that use blank lines to group card types are left alone.
    if not explicit_sections and group == 1:
        tail = [e for e, g in zip(entries, groups) if g == 1]
        if sum(e.quantity for e in tail) <= 15:
            for entry in tail:
                entry.section = "Sideboard"

    return entries
