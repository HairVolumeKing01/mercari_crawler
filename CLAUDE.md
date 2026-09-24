# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project
Mercari JP crawler — search and monitor items on jp.mercari.com via proxy.

## Commands
```bash
# Search (headless is the default; curl mode returns skeletons — see Key Details)
python crawler.py search -k "ポケモンカード"

# Search with headless browser + export to Excel
python crawler.py search -k "ポケモン" --headless -n 10 --excel

# Search + download images + Excel
python crawler.py search -k "ワンピース" --headless --download ./images --excel mydata.xlsx

# Monitor (poll for new items)
python crawler.py monitor -k "呪術廻戦 缶バッジ" --interval 15 --headless

# Dashboard (local web UI over the SQLite DB, default port 5000)
python dashboard.py --port 5000 --db ./mercari.db
```

The monitor also runs as a **background service** on this machine — see "Running service".

## Architecture
`crawler.py` (~1700 lines) + `dashboard.py` (local web UI). Four layers:

- **HTTP layer** (`_curl`, `_curl_post`): shells out to `curl` with SOCKS/HTTP proxy. No `requests` dependency.
- **Parsing layer** (`_parse_items_from_json`, `_parse_card`, `_parse_relative_ja`): builds `Item` dataclasses from Mercari HTML.
- **Browser layer** (`_selenium_search_once`, `_fetch_items_headless`): ChromeDriver + headless Chrome. Search scrolls the infinite-load grid and parses item cards; detail fetching visits each item page.
- **Storage layer**: SQLite (`save_to_sqlite`, `_update_sqlite_item`) and Excel (`export_to_excel`). SQLite is what the running monitor uses.

Public API: `search()`, `monitor()`, `download_images()`, `enrich_items()`, `export_to_excel()`. CLI via `argparse` subcommands (`search` / `monitor`).

## DOM Selectors — check this first
**All Mercari DOM coupling lives in the `SELECTORS` dict near the top of `crawler.py`.** When the crawler starts returning 0 items, blank names/prices, or empty seller/description, look there first: a silent `data-testid` rename is almost always the cause.

Mercari re-skins without notice. Breakages to date:
- 2026-09 — item cards lost `[role="img"][aria-label]` (which carried title + price) → now `thumbnail-item-name` / `item-tile-price`
- 2026-09 — `like-count` removed from cards and item pages → the `likes` field was dropped entirely
- 2026-09 — item pages lost `<time datetime>` (age is now relative text, see `_parse_relative_ja`) and `seller-name` (now `seller-link`, first line of its text)

Keep raw `data-testid=` strings out of parsing code — add a key to `SELECTORS` instead. There is a test asserting this.

Also note `BOT_CHALLENGE_MARKERS` in the same block: substrings that mean Mercari served a bot wall instead of results.

## Running service
Three `monitor` processes (one per profile in `config.json`) run continuously under `run_monitor_forever.ps1`, which respawns any child that dies.

**Editing `crawler.py` has no effect until those processes are restarted.**

```powershell
# stop — matches crawler.py monitor / start_monitor.bat / run_monitor_forever.ps1
powershell -File .\stop_monitor.ps1

# start
Start-Process cmd.exe -ArgumentList "/c",".\start_monitor.bat" `
  -WorkingDirectory "D:\mercari_crawler" -WindowStyle Minimized
```

After stopping, also kill orphaned browsers — chrome processes whose command line contains `--headless` or `mercari_chrome_`. **Never kill chrome by name alone**; that is the user's own browser.

Each browser session gets a unique `--user-data-dir` under `%TEMP%\mercari_chrome_<port>`, deleted by `_cleanup_profile()` when the session ends. Before that existed these accumulated at ~60MB each (3450 dirs / 59.7GB) and filled the C: drive — do not remove the cleanup.

`logs/monitor_*.log` is UTF-8 but was piped through a GBK console, so non-ASCII reads as mojibake. Judge health by the ASCII parts (`Found N item-cells`, `retry`, `new items`) instead.

## Key Details
- Proxy: `$MERCARI_PROXY` env var, defaults to `http://127.0.0.1:7897`.
- Chrome/ChromeDriver paths are hardcoded to the user's local install (`_CHROME_BIN`, `_CHROMEDRIVER`). Check `chromedriver.exe --version` on version mismatch.
- **curl cannot fetch item data.** Mercari serves client-rendered skeletons to non-JS clients, so `--no-headless` and `_fetch_item_curl()` yield nothing. `_fetch_item_curl()` returns early on purpose — it is a stub until Mercari restores SSR.
- Detail pages hydrate late: the detail box lands at ~3s and the seller row at ~5.9s. `_fetch_items_headless()` uses explicit `WebDriverWait`s; do not replace them with a fixed sleep.
- Excel columns: `ID, 标题, 价格, 图片, 商品链接, 卖家, 商品説明, 评论, 发布时间, 爬取时间`. `export_to_excel()` is **header-driven** (`_excel_layout`), so older workbooks that still carry the retired 点赞 column keep their own layout. A file with no `ID` header raises instead of writing at guessed offsets.
- SQLite: `items` table keyed `(id, keyword)`. Databases created before 2026-09 still carry an unused `likes` column — `CREATE TABLE IF NOT EXISTS` intentionally leaves it alone.
- `likes` / `numLikes` are deliberately absent from `Item`. Note `--order num_likes:desc` is unrelated — it is a Mercari search-sort parameter and still supported.
- Image download saves to `./mercari_images/` by default, skipping files already on disk.
- Dependencies: `selenium`, `openpyxl` (auto-installed). `curl` required on PATH.
