"""Tests that never touch the network."""

import httpx
import pytest

from scryfall_mcp.client import ScryfallClient, ScryfallError
from scryfall_mcp.formatting import deck_key, format_detail, format_summary, parse_decklist, usd_price

BOLT = {
    "name": "Lightning Bolt",
    "mana_cost": "{R}",
    "cmc": 1.0,
    "type_line": "Instant",
    "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "color_identity": ["R"],
    "set": "clu",
    "set_name": "Ravnica: Clue Edition",
    "collector_number": "141",
    "rarity": "uncommon",
    "released_at": "2024-02-23",
    "prices": {"usd": "1.50", "usd_foil": None, "eur": "1.20", "tix": "0.03"},
    "legalities": {"modern": "legal", "legacy": "legal", "standard": "not_legal", "oldschool": "banned"},
}

DELVER = {
    "name": "Delver of Secrets // Insectile Aberration",
    "layout": "transform",
    "cmc": 1.0,
    "type_line": "Creature — Human Wizard // Creature — Human Insect",
    "color_identity": ["U"],
    "set": "isd",
    "rarity": "common",
    "prices": {"usd": None, "usd_foil": "4.00"},
    "legalities": {},
    "card_faces": [
        {"name": "Delver of Secrets", "mana_cost": "{U}", "type_line": "Creature — Human Wizard",
         "oracle_text": "At the beginning of your upkeep, look at the top card of your library.",
         "power": "1", "toughness": "1"},
        {"name": "Insectile Aberration", "mana_cost": "", "type_line": "Creature — Human Insect",
         "oracle_text": "Flying", "power": "3", "toughness": "2"},
    ],
}  # fmt: skip


def test_summary_is_compact():
    text = format_summary(BOLT)
    assert text.splitlines()[0] == "Lightning Bolt {R} — Instant [CLU · uncommon · $1.50]"
    assert "deals 3 damage" in text
    assert "deals 3 damage" not in format_summary(BOLT, include_text=False)


def test_detail_lists_legality_by_status():
    text = format_detail(BOLT)
    assert "Legal: legacy, modern" in text
    assert "Banned: oldschool" in text
    assert "standard" not in text
    assert "Prices: $1.50 · €1.20 · 0.03 tix" in text


def test_double_faced_cards_show_both_faces():
    text = format_detail(DELVER)
    assert "Delver of Secrets {U}" in text
    assert "Insectile Aberration" in text and "3/2" in text
    assert usd_price(DELVER) == 4.0  # falls back to the foil price
    assert "Flying" in format_summary(DELVER)


def test_parse_arena_export():
    entries = parse_decklist(
        "Deck\n4 Lightning Bolt (CLU) 141\n2x Fire // Ice\nMountain\n\nSideboard\n3 Pyroblast (EMA) 142 *F*\n"
    )
    assert [(e.quantity, e.name, e.section) for e in entries] == [
        (4, "Lightning Bolt", "Main"),
        (2, "Fire // Ice", "Main"),
        (1, "Mountain", "Main"),
        (3, "Pyroblast", "Sideboard"),
    ]


def test_parse_mtgo_blank_line_sideboard_and_sb_prefix():
    mtgo = parse_decklist("// burn\n20 Mountain\n4 Lightning Bolt\n\n3 Smash to Smithereens\n")
    assert [e.section for e in mtgo] == ["Main", "Main", "Sideboard"]

    prefixed = parse_decklist("4 Lightning Bolt\nSB: 2 Pyroblast")
    assert [e.section for e in prefixed] == ["Main", "Sideboard"]


def test_blank_lines_grouping_card_types_are_not_a_sideboard():
    grouped = parse_decklist("4 Goblin Guide\n\n4 Lightning Bolt\n\n20 Mountain\n")
    assert {e.section for e in grouped} == {"Main"}


def _client(handler) -> ScryfallClient:
    return ScryfallClient(min_interval=0, transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_client_caches_and_sends_required_headers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"object": "card", "name": "Lightning Bolt"})

    client = _client(handler)
    first = await client.get("/cards/named", {"fuzzy": "bolt", "set": None})
    second = await client.get("/cards/named", {"fuzzy": "bolt"})
    assert first == second and len(calls) == 1
    assert calls[0].headers["user-agent"].startswith("scryfall-mcp/")
    assert calls[0].headers["accept"] == "application/json"
    assert "set" not in calls[0].url.params


@pytest.mark.anyio
async def test_client_surfaces_scryfall_error_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"object": "error", "code": "not_found", "status": 404, "details": "No cards found."})

    with pytest.raises(ScryfallError) as excinfo:
        await _client(handler).get("/cards/named", {"fuzzy": "zzzz"})
    assert excinfo.value.not_found and str(excinfo.value) == "No cards found."


@pytest.mark.anyio
async def test_client_retries_once_after_rate_limit():
    responses = [httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json={"ok": True})]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    assert await _client(handler).get("/sets") == {"ok": True}


def test_split_cards_match_on_front_face():
    assert deck_key("Fire // Ice") == deck_key("Fire/Ice") == deck_key("fire") == "fire"


FIRE_ICE = {
    "id": "fire-ice", "name": "Fire // Ice", "mana_cost": "{1}{R} // {1}{U}", "cmc": 4.0,
    "type_line": "Instant // Instant", "prices": {"usd": "0.25"}, "legalities": {"modern": "not_legal"},
    "card_faces": [{"name": "Fire", "type_line": "Instant"}, {"name": "Ice", "type_line": "Instant"}],
}  # fmt: skip
MOUNTAIN = {
    "id": "mountain", "name": "Mountain", "cmc": 0.0, "type_line": "Basic Land — Mountain",
    "prices": {"usd": None}, "legalities": {"modern": "legal"},
}  # fmt: skip


@pytest.fixture
def fake_scryfall(monkeypatch):
    """Point the server at a canned Scryfall and record what it was asked."""
    from scryfall_mcp import server

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/cards/collection":
            return httpx.Response(200, json={"data": [{**BOLT, "id": "bolt"}, FIRE_ICE, MOUNTAIN], "not_found": []})
        if path == "/cards/named" and request.url.params["fuzzy"] == "Lightnng Helix":
            helix = {**BOLT, "id": "helix", "name": "Lightning Helix", "prices": {"usd": "2.00"}}
            return httpx.Response(200, json=helix)
        if path == "/cards/search" and "Mountain" in request.url.params["q"]:
            return httpx.Response(200, json={"data": [{**MOUNTAIN, "prices": {"usd": "0.10"}}], "total_cards": 1})
        return httpx.Response(404, json={"object": "error", "status": 404, "details": "No cards found matching “nope”"})

    monkeypatch.setattr(server, "scryfall", _client(handler))
    return requests


async def _call(name: str, args: dict):
    from mcp import Client

    from scryfall_mcp.server import mcp

    async with Client(mcp) as client:
        result = await client.call_tool(name, args)
    return result.is_error, "\n".join(c.text for c in result.content)


@pytest.mark.anyio
async def test_analyze_decklist_end_to_end(fake_scryfall):
    deck = "4 Lightning Bolt\n2 Fire // Ice\n3 Lightnng Helix\n10 Mountain\n1 Nope\n"
    is_error, text = await _call("analyze_decklist", {"decklist": deck, "format": "modern"})

    assert not is_error
    # 4 x 1.50 + 2 x 0.25 + 3 x 2.00 (fuzzy-matched typo) + 10 x 0.10 (cheapest-printing fallback)
    assert "19 cards · $13.50 total" in text
    assert "Fire // Ice: not legal" in text
    assert "Not found on Scryfall: Nope" in text
    assert "1 Nope — NOT FOUND" in text

    batch = next(r for r in fake_scryfall if r.url.path == "/cards/collection")
    assert b'"name":"Fire"' in batch.content.replace(b" ", b"")  # front face only


@pytest.mark.anyio
async def test_scryfall_errors_reach_the_model(fake_scryfall):
    is_error, text = await _call("get_card", {"name": "nope"})
    assert is_error and "No cards found matching “nope”" in text


@pytest.fixture
def anyio_backend():
    return "asyncio"
