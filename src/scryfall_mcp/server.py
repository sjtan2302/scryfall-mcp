"""MCP server exposing Scryfall's Magic: The Gathering card data."""

from __future__ import annotations

import functools
from collections import Counter
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import ScryfallClient, ScryfallError
from .formatting import (
    Card,
    DeckEntry,
    deck_key,
    faces,
    format_detail,
    format_summary,
    front_face_name,
    image_urls,
    lookup_keys,
    oracle_text,
    parse_decklist,
    usd_price,
)

mcp = MCPServer(
    "scryfall",
    title="Scryfall",
    instructions=(
        "Authoritative Magic: The Gathering card data from Scryfall. Use these tools instead of "
        "recalling card text, prices, legality or rulings from memory: Oracle wording is exact, "
        "prices update daily, and sets newer than your training data are included."
    ),
    # stdio servers log to stderr; keep per-request chatter out of the client's logs.
    log_level="WARNING",
)

scryfall = ScryfallClient()

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True, idempotent_hint=True)

SCRYFALL_PAGE_SIZE = 175
COLLECTION_BATCH_SIZE = 75
MAX_FUZZY_FALLBACKS = 10
MAX_PRICE_FALLBACKS = 5

SortOrder = Literal[
    "name", "released", "set", "rarity", "color", "cmc", "power", "toughness",
    "usd", "eur", "tix", "edhrec", "penny", "artist", "review",
]  # fmt: skip


def _tool(title: str, *, idempotent: bool = True):
    """Register a read-only tool whose Scryfall errors reach the model as readable text."""
    annotations = READ_ONLY if idempotent else READ_ONLY.model_copy(update={"idempotent_hint": False})

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except ScryfallError as exc:
                # The SDK hides the text of unexpected exceptions; ToolError passes it on,
                # and Scryfall's messages ("Too many cards match ambiguous name...") are useful.
                raise ToolError(str(exc)) from exc

        # Results are prose for the model, so skip the structured-output wrapper.
        mcp.tool(title=title, annotations=annotations, structured_output=False)(wrapper)
        return wrapper

    return decorator


async def _cheapest_printing(exact_name: str) -> list[Card]:
    """Paper printings that have a USD price, cheapest first. Empty if none are priced."""
    try:
        data = await scryfall.get(
            "/cards/search",
            {"q": f'!"{exact_name}" game:paper usd>0', "unique": "prints", "order": "usd", "dir": "asc"},
        )
    except ScryfallError as exc:
        if exc.not_found:
            return []
        raise
    return data.get("data", [])


async def _named(name: str, set_code: str | None = None, exact: bool = False) -> Card:
    params = {("exact" if exact else "fuzzy"): name, "set": set_code.lower() if set_code else None}
    return await scryfall.get("/cards/named", params)


# -- search ----------------------------------------------------------------


@_tool("Search cards")
async def search_cards(
    query: Annotated[str, Field(description="A Scryfall search query, e.g. 'c:ug t:creature cmc<=3 f:modern'.")],
    order: Annotated[SortOrder, Field(description="Field to sort by.")] = "name",
    direction: Annotated[Literal["auto", "asc", "desc"], Field(description="Sort direction.")] = "auto",
    unique: Annotated[
        Literal["cards", "art", "prints"],
        Field(description="'cards' returns one result per card; 'prints' returns every printing."),
    ] = "cards",
    limit: Annotated[int, Field(ge=1, le=50, description="How many cards to return.")] = 20,
    offset: Annotated[int, Field(ge=0, description="Results to skip, for paging through long result sets.")] = 0,
    include_text: Annotated[bool, Field(description="Include each card's Oracle text.")] = True,
    include_images: Annotated[
        bool, Field(description="Include a link to each card's image. Use get_card_image to see a card.")
    ] = False,
) -> str:
    """Search for Magic cards using Scryfall's full search syntax.

    Common syntax (combine terms with spaces; prefix with - to negate; use `or` and parentheses):
      c:rg / c>=uw / c:colorless   card colors           id:esper / id<=wubrg   color identity (Commander)
      t:creature t:elf             type line             o:"draw a card"         Oracle text contains
      kw:flying                    keyword ability       mv<=3 / mv:even         mana value
      pow>=4 tou<pow               power / toughness     m:{2}{U}{U}             mana cost
      f:modern / banned:legacy     format legality       r:mythic / r>=rare      rarity
      s:mh3 / e:dsk                set code              usd<2 / eur>=10 / tix<1 price
      is:commander is:fetchland is:dual is:reserved is:spell is:permanent        shortcuts
      year>=2024 / date>=2025-01-01                      release date
      otag:removal / otag:ramp / otag:card-draw          community function tags
      !"Lightning Bolt"            exact card name       name:/regex/            name regex
    """
    page = offset // SCRYFALL_PAGE_SIZE + 1
    start = offset % SCRYFALL_PAGE_SIZE
    params = {"q": query, "order": order, "dir": direction, "unique": unique}

    cards: list[Card] = []
    total = 0
    warnings: list[str] = []
    try:
        while len(cards) < limit:
            data = await scryfall.get("/cards/search", {**params, "page": page})
            total = data.get("total_cards", total)
            warnings = data.get("warnings") or warnings
            cards.extend(data.get("data", [])[start : start + limit - len(cards)])
            if not data.get("has_more"):
                break
            page, start = page + 1, 0
    except ScryfallError as exc:
        if not exc.not_found:
            raise
        if not cards:
            return f"No cards matched `{query}`. {exc}"

    shown_to = offset + len(cards)
    header = f"{total} cards match `{query}`. Showing {offset + 1}–{shown_to}."
    if shown_to < total:
        header += f" Call again with offset={shown_to} for more."
    lines = [header]
    lines.extend(f"Note: {w}" for w in warnings)
    lines.append("")
    separator = "\n\n" if include_text or include_images else "\n"
    lines.append(
        separator.join(format_summary(c, include_text=include_text, include_image=include_images) for c in cards)
    )
    return "\n".join(lines)


# -- single cards ----------------------------------------------------------


@_tool("Get card")
async def get_card(
    name: Annotated[str, Field(description="Card name. Misspellings and partial names are fine unless exact=true.")],
    set_code: Annotated[str | None, Field(description="Optional set code (e.g. 'mh3') to pick a printing.")] = None,
    exact: Annotated[bool, Field(description="Require an exact name match instead of fuzzy matching.")] = False,
) -> str:
    """Look up one card by name: exact Oracle text, mana cost, types, prices and format legality."""
    return format_detail(await _named(name, set_code, exact))


@_tool("Get card image")
async def get_card_image(
    name: Annotated[str, Field(description="Card name. Misspellings and partial names are fine.")],
    set_code: Annotated[
        str | None, Field(description="Optional set code (e.g. 'lea') to get that printing's artwork.")
    ] = None,
    size: Annotated[
        Literal["small", "normal", "large"],
        Field(description="Image size: small (146x204), normal (488x680) or large (672x936)."),
    ] = "normal",
) -> list[str | Image]:
    """Get the picture of a card: the full card image, unaltered, as it is printed.

    Returns both faces for double-faced cards. Use this when the user wants to see a card
    or asks about its artwork; use get_card when only the text is needed.
    """
    card = await _named(name, set_code)
    images = image_urls(card, size)
    if not images:
        raise ToolError(f"Scryfall has no image for {card.get('name', name)}.")

    # The card-level credit already covers every face when it is present.
    credits = [card.get("artist")] if card.get("artist") else [f.get("artist") for f in card.get("card_faces") or []]
    artists = dict.fromkeys(a for a in credits if a)
    caption = (
        f"{card.get('name', name)} — {card.get('set_name', '?')} ({(card.get('set') or '?').upper()}) "
        f"#{card.get('collector_number', '?')}"
    )
    if artists:
        caption += f". Illustrated by {', '.join(artists)}."
    if len(images) > 1:
        caption += f" {len(images)} faces: {', '.join(face for face, _ in images)}."

    result: list[str | Image] = [caption]
    for _, url in images:
        data, image_format = await scryfall.get_image(url)
        result.append(Image(data=data, format=image_format))
    result.append("\n".join(f"Image link: {url}" for _, url in images))
    return result


@_tool("Get rulings")
async def get_rulings(
    name: Annotated[str, Field(description="Card name (fuzzy matched).")],
) -> str:
    """Get the official rulings and clarifications for a card, alongside its Oracle text.

    Use this for rules questions and card interactions.
    """
    card = await _named(name)
    data = await scryfall.get(f"/cards/{card['id']}/rulings")
    rulings = data.get("data", [])

    lines = [card.get("name", name), oracle_text(card), ""]
    if not rulings:
        lines.append("No rulings are recorded for this card.")
    else:
        lines.append(f"{len(rulings)} rulings:")
        for ruling in rulings:
            source = "Wizards" if ruling.get("source") == "wotc" else "Scryfall"
            lines.append(f"- ({ruling.get('published_at', '?')}, {source}) {ruling.get('comment', '')}")
    return "\n".join(lines)


@_tool("Get printings")
async def get_printings(
    name: Annotated[str, Field(description="Card name (fuzzy matched).")],
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum printings to list.")] = 30,
) -> str:
    """List a card's paper printings cheapest first, with set, rarity, release date and price.

    Use this to find the cheapest version of a card or to see which sets it appeared in.
    """
    card = await _named(name)
    exact_name = card.get("name", name)
    prints = await _cheapest_printing(exact_name)
    if not prints:
        return f"{exact_name}: no paper printing currently has a USD price on Scryfall."

    lines = [f"{exact_name}: cheapest paper printings with a USD price."]
    for printing in prints[:limit]:
        prices = printing.get("prices") or {}
        price = f"${prices['usd']}" if prices.get("usd") else "—"
        foil = f" (foil ${prices['usd_foil']})" if prices.get("usd_foil") else ""
        lines.append(
            f"- {price:>8}{foil}  {printing.get('set_name', '?')} ({(printing.get('set') or '?').upper()}) "
            f"#{printing.get('collector_number', '?')} · {printing.get('rarity', '?')} · "
            f"{printing.get('released_at', '?')}"
        )
    if len(prints) > limit:
        lines.append(f"…and more; showing the {limit} cheapest.")
    return "\n".join(lines)


@_tool("Random card", idempotent=False)
async def random_card(
    query: Annotated[
        str | None,
        Field(description="Optional Scryfall search query to pick from, e.g. 'is:commander id:golgari'."),
    ] = None,
) -> str:
    """Get a random card, optionally restricted to cards matching a Scryfall search query."""
    return format_detail(await scryfall.get("/cards/random", {"q": query}, cache=False))


# -- decklists -------------------------------------------------------------


async def _fetch_by_name(names: list[str]) -> tuple[dict[str, Card], list[str]]:
    """Resolve decklist names in batches. Returns cards keyed by deck_key, and the misses."""
    found: dict[str, Card] = {}
    lookups = list(dict.fromkeys(front_face_name(n) for n in names))
    for i in range(0, len(lookups), COLLECTION_BATCH_SIZE):
        batch = lookups[i : i + COLLECTION_BATCH_SIZE]
        data = await scryfall.post("/cards/collection", {"identifiers": [{"name": n} for n in batch]})
        for card in data.get("data", []):
            for key in lookup_keys(card):
                found.setdefault(key, card)

    # The batch endpoint needs exact names; give typos one fuzzy try each.
    missing = [n for n in names if deck_key(n) not in found]
    for name in missing[:MAX_FUZZY_FALLBACKS]:
        try:
            found[deck_key(name)] = await _named(name)
        except ScryfallError:
            continue
    return found, [n for n in names if deck_key(n) not in found]


async def _fill_missing_prices(cards: list[Card]) -> dict[str, float]:
    """For cards whose default printing has no price (common for basics during preview
    season), find the cheapest priced printing. Returns prices keyed by card name."""
    prices: dict[str, float] = {}
    unpriced = [c for c in cards if usd_price(c) is None]
    for card in unpriced[:MAX_PRICE_FALLBACKS]:
        prints = await _cheapest_printing(card["name"])
        price = usd_price(prints[0]) if prints else None
        if price is not None:
            prices[card["name"]] = price
    return prices


def _card_category(card: Card) -> str:
    type_line = (faces(card)[0].get("type_line") or card.get("type_line") or "").split("—")[0]
    for category in ("Land", "Creature", "Planeswalker", "Battle", "Instant", "Sorcery", "Artifact", "Enchantment"):
        if category in type_line:
            return category
    return "Other"


@_tool("Analyze decklist")
async def analyze_decklist(
    decklist: Annotated[
        str,
        Field(description="Plain-text decklist, one card per line, e.g. '4 Lightning Bolt'. Section headers "
              "such as 'Sideboard' or 'Commander' and Arena/MTGO export formats are understood."),
    ],
    format: Annotated[
        str | None,
        Field(description="Format to check legality against, e.g. 'modern', 'commander', 'pauper', 'standard'."),
    ] = None,
) -> str:
    """Price a decklist and check it: per-card and total cost, type breakdown, and format legality.

    Looks up every card in one batch, so prefer this over calling get_card for each card in a list.
    """
    entries = parse_decklist(decklist)
    if not entries:
        return "No cards found in that decklist. Expected lines like '4 Lightning Bolt'."

    unique_names = list(dict.fromkeys(e.name for e in entries))
    found, missing = await _fetch_by_name(unique_names)

    def card_for(entry: DeckEntry) -> Card | None:
        return found.get(deck_key(entry.name))

    unique_cards = list({c["id"]: c for c in found.values() if "id" in c}.values())
    fallback_prices = await _fill_missing_prices(unique_cards)

    fmt = format.strip().lower() if format else None
    if fmt:
        sample = next(iter(found.values()), None)
        if sample is not None and fmt not in (sample.get("legalities") or {}):
            known = ", ".join(sorted(sample.get("legalities") or {}))
            return f"Unknown format '{format}'. Scryfall tracks: {known}."

    lines: list[str] = []
    total_cards = 0
    total_price = 0.0
    unpriced: list[str] = []
    problems: list[str] = []
    categories: Counter[str] = Counter()
    mana_values: list[float] = []

    sections = list(dict.fromkeys(e.section for e in entries))
    for section in sections:
        rows = [e for e in entries if e.section == section]
        lines.append(f"{section} ({sum(e.quantity for e in rows)})")
        for entry in rows:
            card = card_for(entry)
            if card is None:
                lines.append(f"  {entry.quantity} {entry.name} — NOT FOUND")
                continue

            price = usd_price(card)
            if price is None:
                price = fallback_prices.get(card["name"])
            if price is None:
                unpriced.append(card["name"])
                cost = "no price"
            else:
                cost = f"${price:,.2f} ea" if entry.quantity > 1 else f"${price:,.2f}"
            mana = card.get("mana_cost") or (faces(card)[0].get("mana_cost") or "")
            lines.append(f"  {entry.quantity} {card['name']} {mana} — {_card_category(card)} — {cost}".replace("  —", " —"))

            if section == "Maybeboard":
                continue
            total_cards += entry.quantity
            total_price += (price or 0.0) * entry.quantity
            if section in ("Main", "Commander"):
                categories[_card_category(card)] += entry.quantity
                if _card_category(card) != "Land":
                    mana_values.extend([card.get("cmc", 0.0)] * entry.quantity)

            if fmt:
                status = (card.get("legalities") or {}).get(fmt)
                if status in ("not_legal", "banned"):
                    problems.append(f"{card['name']}: {status.replace('_', ' ')}")
                elif status == "restricted" and entry.quantity > 1:
                    problems.append(f"{card['name']}: restricted to 1 copy, list has {entry.quantity}")
        lines.append("")

    summary = [f"{total_cards} cards · ${total_price:,.2f} total"]
    if categories:
        breakdown = ", ".join(f"{count} {name.lower()}" for name, count in categories.most_common())
        summary.append(f"Main deck: {breakdown}")
    if mana_values:
        summary.append(f"Average mana value of nonland cards: {sum(mana_values) / len(mana_values):.2f}")
    if fmt:
        if problems:
            summary.append(f"Not {fmt}-legal ({len(problems)} issue{'s' if len(problems) != 1 else ''}):")
            summary.extend(f"  - {p}" for p in problems)
        else:
            summary.append(f"Every card found is legal in {fmt}. (Deck size and copy limits are not checked.)")
    if missing:
        summary.append(f"Not found on Scryfall: {', '.join(missing)}")
    if unpriced:
        summary.append(f"No USD price available (counted as $0): {', '.join(sorted(set(unpriced)))}")
    summary.append("Prices are for Scryfall's default printing of each card (the cheapest printing where "
        "that one has no price); get_printings finds cheaper versions.")

    return "\n".join(lines + summary)


# -- sets ------------------------------------------------------------------


@_tool("List sets")
async def list_sets(
    query: Annotated[
        str | None, Field(description="Filter by set code or part of the set name, e.g. 'mh3' or 'horizons'.")
    ] = None,
    set_type: Annotated[
        str | None,
        Field(description="Filter by set type, e.g. 'expansion', 'core', 'masters', 'commander', 'draft_innovation'."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum sets to return.")] = 25,
) -> str:
    """List Magic sets, newest first, with set code, type, release date and card count.

    Use this to find a set's code for searches (s:CODE) or to see what has been released recently.
    """
    data = await scryfall.get("/sets")
    sets: list[dict[str, Any]] = data.get("data", [])

    if query:
        needle = query.strip().lower()
        sets = [s for s in sets if needle == s.get("code", "") or needle in s.get("name", "").lower()]
    if set_type:
        sets = [s for s in sets if s.get("set_type") == set_type.strip().lower()]
    if not sets:
        return "No sets matched."

    sets.sort(key=lambda s: s.get("released_at") or "", reverse=True)
    lines = [f"{len(sets)} sets match." + (f" Showing the {limit} newest." if len(sets) > limit else "")]
    for s in sets[:limit]:
        lines.append(
            f"- {s.get('code', '?').upper()} · {s.get('name', '?')} · {s.get('set_type', '?')} · "
            f"{s.get('released_at', 'unreleased')} · {s.get('card_count', 0)} cards"
        )
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
