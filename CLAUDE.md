# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project
Mercari JP crawler — search and monitor items on jp.mercari.com via proxy.

## Commands
```bash
# Search (curl only, no JS rendering)
python crawler.py search -k "ポケモンカード"

# Search with headless browser + export to Excel
python crawler.py search -k "ポケモン" --headless -n 10 --excel

# Search + download images + Excel
python crawler.py search -k "ワンピース" --headless --download ./images --excel mydata.xlsx

# Monitor (poll for new items)
python crawler.py monitor -k "呪術廻戦 缶バッジ" --interval 15 --headless
```

## Architecture
Single-file script (`crawler.py`), ~630 lines. Three layers:

- **HTTP layer** (`_curl`, `_curl_post`): shell out to `curl` with SOCKS/HTTP proxy. No `requests` dependency.
- **Parsing layer** (`_parse_items_from_json`, `_parse_card`): extract `Item` dataclasses from Mercari's HTML — first tries `__NEXT_DATA__` SSR JSON, falls back to regex on streamed RSC data. Sold-out items are filtered.
- **Browser layer** (`_selenium_search`): launches ChromeDriver + headless Chrome, scrolls infinite-load pages, parses `[data-testid="item-cell"]` cards via Selenium. When `fetch_details=True`, navigates to each product page to extract description/seller/likes.

Public API: `search()`, `monitor()`, `download_images()`, `export_to_excel()`. CLI via `argparse` subcommands (`search` / `monitor`).

## Key Details
- Proxy: `$MERCARI_PROXY` env var, defaults to `http://127.0.0.1:7897`.
- Chrome/ChromeDriver paths hardcoded to user's local install. Check with `chromedriver.exe --version` if version mismatch.
- Headless mode scrolls until `max_items` collected or 3 rounds with no new items.
- `--excel` appends to existing file, deduplicating by item ID. Columns: ID, 标题, 价格, 图片链接, 商品链接, 卖家, 点赞, 商品説明, 爬取时间.
- Image download saves to `./mercari_images/` by default, skips files already on disk.
- Dependencies: `selenium`, `openpyxl` (auto-installed). `curl` required on PATH.
