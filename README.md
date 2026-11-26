# scryfall-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) accurate Magic: The Gathering card data from the [Scryfall API](https://scryfall.com/docs/api): exact Oracle text, rulings, prices, format legality and full card search.

No API key is needed.

## Tools

| Tool | What it does |
|---|---|
| `search_cards` | Search with full Scryfall syntax (`c:ug t:creature mv<=3 f:modern`), sorted and paged; `include_images` adds an image link per card |
| `get_card` | One card by name (typos are fine): Oracle text, cost, prices, legality |
| `get_card_image` | The card's picture as image content, so the model can see it; both faces of double-faced cards |
| `get_rulings` | Official rulings for a card, for rules questions |
| `get_printings` | A card's paper printings, cheapest first |
| `random_card` | A random card, optionally from a search query |
| `analyze_decklist` | Price a pasted decklist, break it down by type, and check legality in a format |
| `list_sets` | Sets, newest first, with codes and release dates |

Results are trimmed to the fields a model reasons about. A raw Scryfall card object is several kilobytes; a search result here is a line or two per card.

## Install

Requires [uv](https://docs.astral.sh/uv/).

```bash
cd /path/to/scryfall-mcp && uv sync
```

**Claude Code**

```bash
claude mcp add scryfall -- uv run --directory /path/to/scryfall-mcp scryfall-mcp
```

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`, then restart the app. Use the absolute path to `uv` (`which uv`), because desktop apps don't inherit your shell's `PATH`.

```json
{
  "mcpServers": {
    "scryfall": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/path/to/scryfall-mcp", "scryfall-mcp"]
    }
  }
}
```

## Things to try

- "Find mono-green ramp creatures under $1 that are legal in Pauper."
- "How does Blood Moon interact with Urza's Saga?"
- "Here's my Commander list. What does it cost, and is anything banned?"
- "What's the cheapest printing of Sol Ring?"
- "Show me the Alpha printing of Shivan Dragon."

## Development

```bash
uv run pytest
```

The tests are offline: they use canned responses, never the live API. To poke at the server by hand, use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run scryfall-mcp
```

Layout:

- `src/scryfall_mcp/client.py` — HTTP client: headers, rate limiting, caching, error mapping
- `src/scryfall_mcp/formatting.py` — card-to-text formatting and the decklist parser
- `src/scryfall_mcp/server.py` — the MCP tools

## Being a good Scryfall citizen

The client follows [Scryfall's API guidelines](https://scryfall.com/docs/api):

- identifies itself with `User-Agent` and `Accept` headers
- waits 100ms between requests and backs off on HTTP 429
- caches responses in memory for an hour (prices change at most daily)
- resolves decklists through the batch endpoint, 75 cards per request
- returns card images whole and unaltered, with the artist credited, and only downloads them from Scryfall's own image host

Known limits:

- Decklist prices use Scryfall's default printing of each card, which is not always the cheapest one.
- `analyze_decklist` checks each card's legality but not deck size or copy limits.

This project is not affiliated with Scryfall or Wizards of the Coast. Card data and prices come from Scryfall; per their terms, don't put that data behind a paywall.
