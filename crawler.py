#!/usr/bin/env python
"""
Mercari JP Crawler
==================
Search & monitor items on jp.mercari.com via proxy.

Requirements: Python 3.9+, curl, (optional: playwright for JS pages)
Usage:
  python crawler.py search -k "ポケモンカード"
  python crawler.py search -k "ポケモン" --json --page 2
  python crawler.py monitor -k "呪術廻戦 缶バッジ" --interval 15
"""

import json
import os
import re
import html
import smtplib
import subprocess
import sys
import time
import sqlite3
import urllib.parse
import traceback
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.header import Header
from email.policy import SMTP
from email import encoders
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, Callable


def _load_config() -> dict:
    """Load local config.json if present. Environment variables still override it."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[config] failed to read config.json: {e}")
        return {}


# ==================== Config ====================
CONFIG = _load_config()
SMTP_CONFIG = CONFIG.get("smtp", {}) if isinstance(CONFIG.get("smtp", {}), dict) else {}

PROXY = os.environ.get("MERCARI_PROXY") or CONFIG.get("mercari_proxy") or "http://127.0.0.1:7897"
BASE_URL = "https://jp.mercari.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
SMTP_HOST = os.environ.get("SMTP_HOST") or SMTP_CONFIG.get("host") or "smtp.qq.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or SMTP_CONFIG.get("port") or 465)
SMTP_USER = os.environ.get("SMTP_USER") or SMTP_CONFIG.get("user") or ""
SMTP_PASS = os.environ.get("SMTP_PASS") or SMTP_CONFIG.get("pass") or ""
SMTP_TO = os.environ.get("SMTP_TO") or SMTP_CONFIG.get("to") or ""
EMAIL_NOTIFY_VERSION = "2026-06-15-bytes-v2"

# ==================== Models ====================
@dataclass
class Item:
    id: str
    name: str
    price: int
    image_url: str
    url: str = ""
    seller: str = ""
    likes: int = 0
    sold_out: bool = False
    description: str = ""
    comments: str = ""
    listed_at: str = ""

    def __post_init__(self):
        if not self.url and self.id:
            self.url = f"{BASE_URL}/product/{self.id}"


@dataclass
class SearchResult:
    items: list[Item] = field(default_factory=list)
    total_count: int = 0
    page: int = 1
    has_next: bool = False


# ==================== HTTP ====================
def _curl(url: str, extra_headers: dict = None, timeout: int = 30, raw: bool = False, use_proxy: bool = True):
    """GET via curl. Set use_proxy=False for direct connection."""
    cmd = ["curl", "-s", "--connect-timeout", "15",
           "--max-time", str(timeout), "--compressed",
           "-H", f"User-Agent: {UA}"]
    if use_proxy and PROXY:
        cmd[1:1] = ["-x", PROXY]
    hdrs = extra_headers or {}
    for k, v in hdrs.items():
        cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        return r.stdout if raw else r.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return b"" if raw else ""
    except Exception:
        return b"" if raw else ""


def _curl_post(url: str, data: dict, timeout: int = 30) -> str:
    """POST JSON via curl + proxy."""
    body = json.dumps(data)
    cmd = [
        "curl", "-x", PROXY, "-s", "--connect-timeout", "15",
        "--max-time", str(timeout),
        "-X", "POST",
        "-H", f"User-Agent: {UA}",
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-d", body,
        url,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return r.stdout
    except Exception:
        return ""


def _parse_ts(ts) -> str:
    """Parse a timestamp (Unix int/str or ISO string) → 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        pass
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.utcoffset() is not None:
            import datetime as _dt
            d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            d = datetime.fromtimestamp(d.timestamp())
        return d.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:
        return str(ts)[:16].replace("T", " ")
    except Exception:
        return ""


# ==================== HTML Parser ====================
def _search_session_id(html: str) -> str:
    """Extract searchSessionId from SSR page."""
    m = re.search(r'initialSearchSessionId["\']?\s*[:=]\s*["\']([a-f0-9]+)["\']', html)
    return m.group(1) if m else ""


def _parse_items_from_json(html: str) -> list[Item]:
    """Try parsing items from __NEXT_DATA__ or embedded JSON in scripts."""
    items = []

    # Strategy 1: __NEXT_DATA__
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            props = data.get("props", {}).get("pageProps", {})
            sr = props.get("searchResult", {}) or props.get("initialData", {})
            raw_items = sr.get("items", [])
            for it in raw_items:
                if it.get("status") == "sold_out":
                    continue
                # Extract listing time from __NEXT_DATA__
                created_ts = it.get("created") or it.get("createdAt") or it.get("updatedAt") or 0
                listed_at = _parse_ts(created_ts)

                items.append(Item(
                    id=str(it.get("id", "")),
                    name=it.get("name", ""),
                    price=int(it.get("price", 0) or 0),
                    image_url=(it.get("thumbnails") or [""])[0],
                    seller=(it.get("seller") or {}).get("name", ""),
                    likes=int(it.get("numLikes", 0) or 0),
                    sold_out=False,
                    listed_at=listed_at,
                ))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Strategy 2: Streamed RSC data
    if not items:
        # Look for item-card JSON patterns in streamed data
        pattern = r'"name":"([^"]+)".*?"price":(\d+).*?"thumbnails":\["([^"]+)"\]'
        for m in re.finditer(pattern, html):
            items.append(Item(
                id=f"unknown_{m.start()}",
                name=m.group(1),
                price=int(m.group(2)),
                image_url=m.group(3),
            ))

    return items


def _search_session_call(session_id: str, keyword: str, page: int = 1) -> list[Item]:
    """Try to use the search session API endpoint."""
    # This is a best-effort attempt at Mercari's internal search API
    url = f"{BASE_URL}/search?keyword={urllib.parse.quote(keyword)}"
    html = _curl(url)
    return _parse_items_from_json(html)


# ==================== Selenium Headless Fallback ====================
# Auto-detected Chrome + ChromeDriver locations
_CHROME_BIN = os.environ.get("CHROME_BIN",
    "C:\\Users\\WUDIFALIANGWANG\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe")
_CHROMEDRIVER = os.environ.get("CHROMEDRIVER",
    "C:\\Users\\WUDIFALIANGWANG\\Downloads\\chromedriver\\chromedriver-win64\\chromedriver.exe")

def _parse_card(card) -> Optional[Item]:
    """Parse a single item-cell into Item. Returns None on failure or if sold out."""
    from selenium.webdriver.common.by import By
    try:
        # Check for sold-out badge first
        card_text = card.text
        if re.search(r'SOLD|売り切れ|売切', card_text):
            return None

        link = card.find_element(By.CSS_SELECTOR, '[data-testid="thumbnail-link"]')
        href = link.get_attribute("href") or ""
        item_id = href.rstrip("/").split("/")[-1] if href else ""

        thumb = card.find_element(By.CSS_SELECTOR, '[role="img"]')
        aria = thumb.get_attribute("aria-label") or ""
        name = aria
        price = 0
        m = re.search(r'(\d[\d,]*)円', aria)
        if m:
            price = int(m.group(1).replace(",", ""))
            name = aria[:m.start()].replace("の画像", "").strip()

        try:
            img = card.find_element(By.TAG_NAME, "img")
            img_url = img.get_attribute("src") or ""
        except Exception:
            img_url = ""

        # Try to get likes count
        likes = 0
        try:
            likes_el = card.find_element(By.CSS_SELECTOR, '[data-testid="like-count"]')
            likes_text = likes_el.text or ""
            likes_m = re.search(r'(\d[\d,]*)', likes_text)
            if likes_m:
                likes = int(likes_m.group(1).replace(",", ""))
        except Exception:
            pass

        if name or price:
            item_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            return Item(id=item_id, name=name, price=price, image_url=img_url,
                        url=item_url, likes=likes)
    except Exception:
        pass
    return None


def _selenium_search(keyword: str, page: int = 1, order: str = "created_time",
                     max_items: int = 20, fetch_details: bool = False,
                     retry: int = 1) -> SearchResult:
    """Headless browser search using Selenium + Chrome. Scrolls for max_items.
    Note: fetch_details is deprecated — detail fetching moved to curl-based enrich_items()."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
    except ImportError as e:
        raise RuntimeError(f"Missing dependency: {e}. Run: pip install selenium")

    import subprocess as sp
    import random

    for attempt in range(retry + 1):
        try:
            result = _selenium_search_once(keyword, page, order, max_items)
            if result.items or attempt >= retry:
                return result
            print(f"  [retry] 0 items on attempt {attempt+1}, retrying...")
        except Exception as e:
            if attempt >= retry:
                raise
            print(f"  [retry] attempt {attempt+1} failed: {e}, retrying in 5s...")
        time.sleep(5)
    return SearchResult(page=page)


def _selenium_search_once(keyword: str, page: int = 1, order: str = "created_time",
                           max_items: int = 20) -> SearchResult:
    """Single browser session. Internal helper — use _selenium_search instead."""
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
    except ImportError as e:
        raise RuntimeError(f"Missing dependency: {e}. Run: pip install selenium")

    import subprocess as sp
    import random

    # Kill leftover Chrome/ChromeDriver from previous crashed cycles
    try:
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True, timeout=5)
        subprocess.run(["taskkill", "/f", "/im", "chromedriver.exe"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(1)

    port = random.randint(10000, 60000)
    cd_proc = sp.Popen(
        [_CHROMEDRIVER, f"--port={port}", "--readable-timestamp"],
        stdout=sp.DEVNULL, stderr=sp.DEVNULL
    )
    time.sleep(2)

    params = _build_search_params(keyword, order)
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
    result = SearchResult(page=page)

    opts = Options()
    opts.binary_location = _CHROME_BIN
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-vulkan")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--disable-webgl")
    opts.add_argument("--disable-accelerated-2d-canvas")
    opts.add_argument("--disable-features=VizDisplayCompositor,UseSkiaRenderer")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--proxy-server={PROXY}")
    opts.add_argument("--lang=ja")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--user-agent={UA}")

    try:
        driver = webdriver.Remote(f"http://127.0.0.1:{port}", options=opts)
        driver.set_page_load_timeout(45)
        for _nav_try in range(3):
            try:
                driver.get(url)
                break
            except Exception as nav_err:
                if _nav_try == 2:
                    raise
                print(f"  [nav] connection error ({nav_err}), retry in 4s...")
                time.sleep(4)

        # Inject stealth JS to hide automation from Mercari bot detection
        driver.execute_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
            window.chrome = {runtime: {}};
            const _origQ = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) => (
                p.name === 'notifications' ?
                Promise.resolve({state: Notification.permission}) :
                _origQ(p)
            );
        """)

        # Diagnostic: check what page we got
        print(f"  [debug] Page title: {driver.title[:80]}")
        print(f"  [debug] URL: {driver.current_url[:100]}")

        # Check if Mercari returned a bot-challenge page
        ps = (driver.page_source or "")[:3000].lower()
        if any(kw in ps for kw in [
            "captcha", "recaptcha", "cf-challenge",
            "access denied", "are you a robot",
            "just a moment", "checking your browser",
            "verify you are human", "security check",
        ]):
            print("  [blocked] Mercari returned a bot challenge page -- backing off")
            driver.quit()
            return result

        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="item-grid-skeleton"], [data-testid="item-cell"]'))
        )
        time.sleep(3)
        try:
            WebDriverWait(driver, 25).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '[data-testid="item-cell"]'))
            )
        except Exception:
            pass
        time.sleep(2)

        # Quick check: do we see ANY item cells?
        initial_cards = driver.find_elements(By.CSS_SELECTOR, '[data-testid="item-cell"]')
        print(f"  [debug] Found {len(initial_cards)} item-cells after load")

        # If 0 cells, page might be blocked/broken — dump snippet for diagnosis
        if len(initial_cards) == 0:
            body_text = (driver.find_element(By.TAG_NAME, "body").text or "")[:300]
            print(f"  [debug] Body preview: {body_text}")

        seen_ids = set()
        prev_count = 0
        no_new_rounds = 0

        try:
            while len(result.items) < max_items and no_new_rounds < 6:
                cards = driver.find_elements(By.CSS_SELECTOR, '[data-testid="item-cell"]')
                for card in cards:
                    if len(result.items) >= max_items:
                        break
                    item = _parse_card(card)
                    if item and item.id and item.id not in seen_ids:
                        seen_ids.add(item.id)
                        result.items.append(item)

                if len(result.items) <= prev_count:
                    no_new_rounds += 1
                else:
                    no_new_rounds = 0
                prev_count = len(result.items)

                if len(result.items) >= max_items:
                    break

                # Scroll to trigger lazy-load (Mercari is infinite-scroll, no page param)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.7);")
                time.sleep(0.3)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
        except Exception as e:
            print(f"\n  [!] Browser died mid-scroll: {e}")
            print(f"  [!] Returning {len(result.items)} items collected before crash")

        # ⚠️ Detail fetching removed from browser session — too crash-prone.
        # Use enrich_items() separately (curl-based) after saving to Excel.

        driver.quit()
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        cd_proc.terminate()
        cd_proc.wait(timeout=5)

    return result


# ==================== Public API ====================
SEARCH_URL = f"{BASE_URL}/search"


def _build_search_params(keyword: str, order: str = "created_time") -> dict:
    """Convert internal order format to Mercari URL params (sort + order)."""
    params = {"keyword": keyword, "status": "on_sale"}
    # Map our order values → sort field + order direction
    mapping = {
        "created_time":    ("created_time", "desc"),
        "price:asc":       ("price",        "asc"),
        "price:desc":      ("price",        "desc"),
        "num_likes:desc":  ("num_likes",    "desc"),
    }
    sort_field, sort_dir = mapping.get(order, ("created_time", "desc"))
    params["sort"] = sort_field
    params["order"] = sort_dir
    return params


def search(keyword: str, page: int = 1, order: str = "created_time",
           use_headless: bool = True, max_items: int = 20,
           fetch_details: bool = False) -> SearchResult:
    """
    Search Mercari JP.

    Args:
        keyword: Japanese search term
        page: Page number (1-based)
        order: 'created_time', 'price:asc', 'price:desc', 'num_likes:desc'
        use_headless: Use headless browser (default True; SSR no longer has item data)
        max_items: Max items to collect via scroll (headless only)
        fetch_details: Visit each product page for description (headless only)

    Returns:
        SearchResult with found items
    """
    if not use_headless:
        # ⚠️ curl mode broken since ~2025: Mercari SSR only returns skeletons.
        # Items are loaded via client-side JS API calls = headless required.
        print("[warn] curl mode may return 0 items — use headless")
        result = SearchResult(page=page)
        params = _build_search_params(keyword, order)
        html = _curl(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}")
        result.items = _parse_items_from_json(html)
        if result.items:
            return result
        print("[warn] curl returned 0 items, falling back to headless...")

    return _selenium_search(keyword, page, order, max_items=max_items,
                            fetch_details=fetch_details)


def monitor(keyword: str | list[str], interval_min: int = 30,
            use_headless: bool = True,
            excel_path: str = "",
            db_path: str = "",
            email_notify: bool = False,
            email_to: str = "",
            health_email: bool = False,
            callback: Optional[Callable[[list[Item]], None]] = None):
    """
    Periodically poll for new items matching keyword.

    Args:
        keyword: Search term or list of search terms
        interval_min: Check interval in minutes
        use_headless: Use headless browser
        excel_path: Export new items to this Excel file
        db_path: Save items to SQLite database (preferred over Excel)
        email_notify: Send email when new items are found
        email_to: Override SMTP_TO recipient list
        health_email: Send one daily "still running" email
        callback: Called with list of new items each cycle
    """
    keywords = [keyword] if isinstance(keyword, str) else list(keyword)
    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        raise ValueError("At least one keyword is required")

    seen_by_keyword: dict[str, set[str]] = {kw: set() for kw in keywords}
    last_health_date = ""

    print(f"[monitor] Watching {len(keywords)} keyword(s) every {interval_min}min. Proxy: {PROXY}")
    for kw in keywords:
        print(f"[monitor]   - {kw}")
    if db_path:
        print(f"[monitor] DB: {db_path}")
    if excel_path:
        print(f"[monitor] Excel: {excel_path}")
    if email_notify:
        print(f"[monitor] Email: {email_to or SMTP_TO or '(missing SMTP_TO)'}")
    if health_email:
        print("[monitor] Daily health email: enabled")

    fail_count = 0
    while True:
        try:
            cycle_counts: dict[str, int] = {}
            for kw in keywords:
                # Sync seen IDs from DB (user may have cleared it via dashboard)
                if db_path:
                    db_items = get_all_items(db_path, keyword=kw)
                    seen_by_keyword[kw] = {it["id"] for it in db_items}

                result = search(kw, order="created_time", use_headless=use_headless,
                                fetch_details=False)
                seen_ids = seen_by_keyword.setdefault(kw, set())
                new_items = [it for it in result.items if it.id and it.id not in seen_ids]

                for it in result.items:
                    if it.id:
                        seen_ids.add(it.id)

                cycle_counts[kw] = len(result.items)

                if new_items:
                    fail_count = 0
                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    print(f"\n[{timestamp}] {len(new_items)} new items for '{kw}':")
                    for it in new_items:
                        print(f"  ¥{it.price:,}  {it.name[:60]}")
                        print(f"  {it.url}")

                    if db_path:
                        n = save_to_sqlite(new_items, db_path, keyword=kw)
                        print(f"[monitor] {n} new items saved to DB for '{kw}'")
                        enrich_items(new_items, fetch_details=True)
                        for it in new_items:
                            if it.description or it.seller or it.likes or it.listed_at:
                                _update_sqlite_item(it, db_path)

                    if excel_path:
                        export_to_excel(new_items, excel_path)
                        if not db_path:
                            enrich_items(new_items, fetch_details=True)
                            _export_update_excel(new_items, excel_path)

                    if email_notify:
                        send_email_notification(new_items, kw, smtp_to=email_to or SMTP_TO)

                    if callback:
                        callback(new_items)
                else:
                    fail_count = 0
                    print(f"[{time.strftime('%H:%M:%S')}] No new items for '{kw}' ({len(seen_ids)} tracked)")

            today = time.strftime("%Y-%m-%d")
            if health_email and email_notify and today != last_health_date:
                if send_health_email(keywords, cycle_counts, db_path=db_path, smtp_to=email_to or SMTP_TO):
                    last_health_date = today

        except Exception as e:
            fail_count += 1
            print(f"[monitor] Error (#{fail_count}): {e}")
            if fail_count >= 3:
                print("[monitor] 3 consecutive failures, waiting 5min before retry...")
            time.sleep(60)  # wait 1min before retry on error

        time.sleep(max(3, interval_min) * 60)


def to_dicts(items: list[Item]) -> list[dict]:
    return [asdict(it) for it in items]


def _guess_image_subtype(data: bytes, url: str = "") -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    clean_url = (url or "").split("?", 1)[0].lower()
    if clean_url.endswith((".jpg", ".jpeg")):
        return "jpeg"
    if clean_url.endswith(".png"):
        return "png"
    if clean_url.endswith(".gif"):
        return "gif"
    if clean_url.endswith(".webp"):
        return "webp"
    return "jpeg"


def _download_inline_image(url: str, cid: str, max_bytes: int = 2_500_000):
    if not url:
        return None
    data = _curl(url, raw=True, use_proxy=True, timeout=20)
    if not data:
        return None
    if len(data) > max_bytes:
        print(f"[email] inline image skipped: too large ({len(data)} bytes)")
        return None
    subtype = _guess_image_subtype(data, url)
    if subtype == "webp":
        try:
            from io import BytesIO
            from PIL import Image as PILImage
            img = PILImage.open(BytesIO(data)).convert("RGB")
            converted = BytesIO()
            img.save(converted, format="JPEG", quality=88, optimize=True)
            data = converted.getvalue()
            subtype = "jpeg"
        except Exception as e:
            print(f"[email] inline image webp conversion failed: {e}")
    return {
        "cid": cid,
        "payload": data,
        "subtype": subtype,
        "filename": f"{cid}.{subtype}",
    }


def _send_html_email(subject: str, body: str,
                     smtp_user: str = "",
                     smtp_pass: str = "",
                     smtp_to: str = "",
                     smtp_host: str = "",
                     smtp_port: int = 0,
                     inline_parts: Optional[list[dict]] = None) -> bool:
    """Send an HTML email via SMTP. Handles UTF-8 body/subject safely."""
    smtp_user = smtp_user or SMTP_USER
    smtp_pass = smtp_pass or SMTP_PASS
    smtp_to = smtp_to or SMTP_TO
    smtp_host = smtp_host or SMTP_HOST
    smtp_port = smtp_port or SMTP_PORT
    if not smtp_user or not smtp_pass or not smtp_to:
        print("[email] skipped: missing SMTP_USER / SMTP_PASS / SMTP_TO")
        return False

    recipients = [addr.strip() for addr in smtp_to.split(",") if addr.strip()]
    if not recipients:
        print("[email] skipped: no recipients")
        return False

    print(f"[email] notifier version: {EMAIL_NOTIFY_VERSION}")

    inline_parts = inline_parts or []
    msg = MIMEMultipart("related" if inline_parts else "alternative")
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(subject, "utf-8").encode()
    if inline_parts:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "html", "utf-8"))
        msg.attach(alt)
        for part in inline_parts:
            payload = part.get("payload") or b""
            subtype = part.get("subtype") or "jpeg"
            cid = part.get("cid") or ""
            filename = part.get("filename") or "image"
            if not payload or not cid:
                continue
            image_part = MIMEBase("image", subtype)
            image_part.set_payload(payload)
            encoders.encode_base64(image_part)
            image_part.add_header("Content-ID", f"<{cid}>")
            image_part.add_header("Content-Disposition", "inline", filename=filename)
            msg.attach(image_part)
    else:
        msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        payload = msg.as_bytes(policy=SMTP)
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, local_hostname="localhost", timeout=30) as server:
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, payload)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, local_hostname="localhost", timeout=30) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, payload)
        print(f"[email] Sent to {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"[email] failed: {e}")
        traceback.print_exc()
        return False


def send_health_email(keywords: list[str], cycle_counts: dict[str, int],
                      db_path: str = "",
                      smtp_to: str = "") -> bool:
    """Send one daily health check email for the monitor process."""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    db_total = ""
    if db_path:
        try:
            db_total = str(len(get_all_items(db_path)))
        except Exception:
            db_total = "unknown"

    rows = []
    for kw in keywords:
        rows.append(
            "<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #eee'>{html.escape(kw)}</td>"
            f"<td style='padding:8px;border-bottom:1px solid #eee;text-align:right'>{cycle_counts.get(kw, 0)}</td>"
            "</tr>"
        )

    subject = f"Mercari监控健康检查：{len(keywords)}个关键词"
    body = f"""<!doctype html>
<html>
<body style="font-family:Arial,'Microsoft YaHei',sans-serif;background:#f6f7f9;margin:0;padding:20px">
  <div style="max-width:720px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;border:1px solid #eee">
    <div style="padding:16px 20px;background:#0f172a;color:#fff">
      <div style="font-size:18px;font-weight:700">Mercari 监控仍在运行</div>
      <div style="font-size:13px;color:#cbd5e1;margin-top:4px">{now}</div>
    </div>
    <div style="padding:16px 20px">
      <p style="margin:0 0 12px">脚本已完成本轮检查，代理配置：{html.escape(PROXY)}</p>
      {f"<p style='margin:0 0 12px'>数据库商品总数：{html.escape(db_total)}</p>" if db_total else ""}
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr>
            <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd">关键词</th>
            <th style="text-align:right;padding:8px;border-bottom:1px solid #ddd">本轮搜索结果数</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
  </div>
</body>
</html>"""
    ok = _send_html_email(subject, body, smtp_to=smtp_to)
    if ok:
        print("[email] Daily health check sent")
    return ok


def send_email_notification(items: list[Item], keyword: str,
                            smtp_user: str = "",
                            smtp_pass: str = "",
                            smtp_to: str = "",
                            smtp_host: str = "",
                            smtp_port: int = 0) -> bool:
    """Send a QQ/SMTP email containing new item images, prices, and links."""
    if not items:
        return False

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    subject = f"Mercari新品提醒：{keyword}（{len(items)}件）"
    rows = []
    inline_parts = []
    for idx, it in enumerate(items):
        name = html.escape(it.name or "(no title)")
        url = html.escape(it.url or "")
        image_url = html.escape(it.image_url or "")
        seller = html.escape(it.seller or "")
        listed_at = html.escape(it.listed_at or "")
        price = f"¥{it.price:,}" if it.price else "价格未知"
        image_src = image_url
        if it.image_url:
            import re as _re2
            cid_suffix = _re2.sub(r"[^A-Za-z0-9]", "", it.id or str(idx)) or str(idx)
            cid = f"mercari_{idx}_{cid_suffix}"
            inline = _download_inline_image(it.image_url, cid)
            if inline:
                inline_parts.append(inline)
                image_src = f"cid:{cid}"
            else:
                print(f"[email] inline image failed for {it.id}, using remote URL")
        image_html = (
            f'<a href="{url}" target="_blank"><img src="{image_src}" '
            'style="width:160px;max-height:160px;object-fit:contain;border-radius:6px;border:1px solid #eee"></a>'
            if image_src else ""
        )
        rows.append(f"""
        <tr>
          <td style="width:170px;padding:12px;vertical-align:top">{image_html}</td>
          <td style="padding:12px;vertical-align:top">
            <div style="font-size:16px;font-weight:600;line-height:1.4;margin-bottom:8px">{name}</div>
            <div style="font-size:18px;color:#d4380d;font-weight:700;margin-bottom:8px">{price}</div>
            <div style="font-size:13px;color:#555;margin-bottom:8px">
              {f"卖家：{seller}<br>" if seller else ""}
              {f"发布时间：{listed_at}<br>" if listed_at else ""}
            </div>
            <a href="{url}" target="_blank" style="color:#1677ff;text-decoration:none">打开 Mercari 原链接</a>
          </td>
        </tr>
        """)

    body = f"""<!doctype html>
<html>
<body style="font-family:Arial,'Microsoft YaHei',sans-serif;background:#f6f7f9;margin:0;padding:20px">
  <div style="max-width:760px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;border:1px solid #eee">
    <div style="padding:16px 20px;background:#111827;color:#fff">
      <div style="font-size:18px;font-weight:700">Mercari 新品提醒</div>
      <div style="font-size:13px;color:#d1d5db;margin-top:4px">关键词：{html.escape(keyword)} | {now}</div>
    </div>
    <table style="width:100%;border-collapse:collapse">
      {''.join(rows)}
    </table>
  </div>
</body>
</html>"""
    ok = _send_html_email(subject, body, smtp_user=smtp_user, smtp_pass=smtp_pass,
                          smtp_to=smtp_to, smtp_host=smtp_host, smtp_port=smtp_port,
                          inline_parts=inline_parts)
    if ok:
        print(f"[email] Sent {len(items)} new items")
    return ok


def download_images(items: list[Item], out_dir: str = "./mercari_images") -> int:
    """Download item images. Returns count of downloaded files."""
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    for it in items:
        if not it.image_url:
            continue
        # Strip query string, use .jpg for webp thumbs
        clean_url = it.image_url.split("?")[0]
        ext = ".jpg" if ".webp" in clean_url else os.path.splitext(clean_url)[1] or ".jpg"
        fname = f"{it.id}{ext}"
        fpath = os.path.join(out_dir, fname)
        if os.path.exists(fpath):
            continue
        r = _curl(it.image_url, raw=True, use_proxy=True)  # mercdn needs proxy
        if r and len(r) > 1000:
            with open(fpath, "wb") as f:
                f.write(r)
            count += 1
            print(f"  [{count}] {fname}")
    return count


def _fetch_item_description(item_id: str) -> dict:
    """Fetch item details from product page. Tries curl first, falls back to headless."""
    return _fetch_item_curl(item_id)


def _fetch_item_curl(item_id: str) -> dict:
    """Fetch item details via curl. Returns {description, seller, likes, listed_at}."""
    url = f"{BASE_URL}/product/{item_id}"
    html = _curl(url)
    result = {"description": "", "seller": "", "likes": 0, "listed_at": ""}

    if not html:
        return result

    # Try __NEXT_DATA__ on product page
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            props = data.get("props", {}).get("pageProps", {})
            item_data = props.get("item", {}) or props
            result["description"] = item_data.get("description", "") or ""
            result["seller"] = (item_data.get("seller") or {}).get("name", "")
            result["likes"] = int(item_data.get("numLikes", 0) or 0)
            created_ts = 0
            for key in ("created", "createdAt", "publishedAt", "created_at", "published_at"):
                if item_data.get(key):
                    created_ts = item_data[key]
                    break
            if not created_ts:
                def _deep_find(d, target_keys):
                    if isinstance(d, dict):
                        for k, v in d.items():
                            if any(t in k.lower() for t in ("created", "published")):
                                return v
                            found = _deep_find(v, target_keys)
                            if found:
                                return found
                    return None
                created_ts = _deep_find(data, ()) or 0
            result["listed_at"] = _parse_ts(created_ts)
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    # Fallback: scrape description from meta or structured data
    if not result["description"]:
        m_desc = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html)
        if m_desc:
            result["description"] = m_desc.group(1)
        else:
            m_body = re.search(r'商品の説明</h\d>(.*?)(?:<h\d>|出品者情報|$)', html, re.DOTALL)
            if m_body:
                result["description"] = re.sub(r'<[^>]+>', '', m_body.group(1)).strip()[:500]

    # Try <time datetime="..."> element
    if not result["listed_at"]:
        for m_te in re.finditer(r'<time[^>]+datetime=["\']([^"\']+)["\']', html):
            ts = _parse_ts(m_te.group(1))
            if ts:
                result["listed_at"] = ts
                break

    # Try schema.org JSON-LD for datePublished
    if not result["listed_at"]:
        m_ld = re.search(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m_ld:
            try:
                ld = json.loads(m_ld.group(1))
                pub = ld.get("datePublished") or ld.get("dateCreated") or ""
                if pub:
                    result["listed_at"] = _parse_ts(pub)
            except Exception:
                pass

    return result


def _fetch_items_headless(item_ids: list[str]) -> dict[str, dict]:
    """Fetch details for multiple items using one headless browser session.
    Returns {item_id: {description, seller, likes, listed_at}}."""
    results = {iid: {"description": "", "seller": "", "likes": 0, "listed_at": ""} for iid in item_ids}
    if not item_ids:
        return results

    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.options import Options
    except ImportError as e:
        raise RuntimeError(f"Missing dependency: {e}. Run: pip install selenium")

    import subprocess as sp
    import random

    # Kill leftovers
    try:
        sp.run(["taskkill", "/f", "/im", "chrome.exe"], capture_output=True, timeout=5)
        sp.run(["taskkill", "/f", "/im", "chromedriver.exe"], capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)

    port = random.randint(10000, 60000)
    cd_proc = sp.Popen(
        [_CHROMEDRIVER, f"--port={port}", "--readable-timestamp"],
        stdout=sp.DEVNULL, stderr=sp.DEVNULL
    )
    time.sleep(1.5)

    opts = Options()
    opts.binary_location = _CHROME_BIN
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(f"--proxy-server={PROXY}")
    opts.add_argument("--lang=ja")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument(f"--user-agent={UA}")

    driver = None
    try:
        driver = webdriver.Remote(f"http://127.0.0.1:{port}", options=opts)
        driver.set_page_load_timeout(25)

        for idx, item_id in enumerate(item_ids):
            url = f"{BASE_URL}/item/{item_id}"
            print(f"\r  [detail] {idx+1}/{len(item_ids)} headless {item_id}...", end="")
            try:
                driver.get(url)
                driver.execute_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                    Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US', 'en']});
                    window.chrome = {runtime: {}};
                """)
                time.sleep(1.5)  # let JS hydrate

                # Extract description
                try:
                    desc_el = driver.find_element(By.CSS_SELECTOR, '[data-testid="description"]')
                    results[item_id]["description"] = desc_el.text[:500]
                except Exception:
                    pass

                # Extract seller
                try:
                    seller_el = driver.find_element(By.CSS_SELECTOR, '[data-testid="seller-name"]')
                    results[item_id]["seller"] = seller_el.text.strip()
                except Exception:
                    pass

                # Extract likes (sidebar like count)
                try:
                    likes_els = driver.find_elements(By.CSS_SELECTOR, '[data-testid="like-count"]')
                    for el in likes_els:
                        m = re.search(r'(\d[\d,]*)', el.text)
                        if m:
                            results[item_id]["likes"] = int(m.group(1).replace(",", ""))
                            break
                except Exception:
                    pass

                # Extract listed_at from <time> element
                try:
                    time_el = driver.find_element(By.TAG_NAME, "time")
                    dt = time_el.get_attribute("datetime") or ""
                    if dt:
                        results[item_id]["listed_at"] = _parse_ts(dt)
                except Exception:
                    pass

                # Fallback: search page source for datetime in <time> tags
                if not results[item_id]["listed_at"]:
                    html = driver.page_source
                    # Debug: dump first item's HTML for diagnostics
                    if idx == 0:
                        with open("debug_product.html", "w", encoding="utf-8") as f:
                            f.write(html)
                        all_times = re.findall(r'<time[^>]*>', html)
                        print(f"\n  [debug] <time> tags found: {len(all_times)}")
                        dt_attrs = re.findall(r'datetime=([\"\\\'])([^\"\\\']+)\\1', html)
                        print(f"  [debug] datetime attrs: {dt_attrs[:3]}")
                    for m_te in re.finditer(r'<time[^>]+datetime=["\']([^"\']+)["\']', html):
                        ts = _parse_ts(m_te.group(1))
                        if ts:
                            results[item_id]["listed_at"] = ts
                            break

                time.sleep(0.2)
            except Exception as e:
                print(f"\n  [!] headless detail error for {item_id}: {e}")
                continue

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        cd_proc.terminate()
        cd_proc.wait(timeout=5)

    return results


def enrich_items(items: list[Item], fetch_details: bool = True):
    """Enrich items with description/details from product pages.
    Tries curl first (fast), falls back to headless batch (slow but works)."""
    if not fetch_details:
        return

    # Phase 1: try curl for all items (fast path)
    need_headless = []
    for i, it in enumerate(items):
        if it.description and it.seller and it.likes and it.listed_at:
            continue
        print(f"\r  [detail] curl {i+1}/{len(items)} {it.id}...", end="")
        detail = _fetch_item_curl(it.id)
        if detail["description"]:
            it.description = detail["description"][:500]
        if detail["seller"] and not it.seller:
            it.seller = detail["seller"]
        if detail["likes"] and not it.likes:
            it.likes = detail["likes"]
        if detail["listed_at"]:
            it.listed_at = detail["listed_at"]
        # Track items that still need listed_at
        if not it.listed_at:
            need_headless.append(it.id)
        time.sleep(0.1)

    # Phase 2: headless batch for items that curl couldn't handle
    if need_headless:
        print(f"\n  [detail] headless batch for {len(need_headless)} items...")
        try:
            details = _fetch_items_headless(need_headless)
            for it in items:
                if it.id in details:
                    d = details[it.id]
                    if d["description"] and not it.description:
                        it.description = d["description"][:500]
                    if d["seller"] and not it.seller:
                        it.seller = d["seller"]
                    if d["likes"] and not it.likes:
                        it.likes = d["likes"]
                    if d["listed_at"] and not it.listed_at:
                        it.listed_at = d["listed_at"]
        except Exception as e:
            print(f"\n  [!] headless enrichment failed: {e}")

    # Report missing timestamps
    missing = [it.id for it in items if not it.listed_at]
    if missing:
        print(f"  [!] {len(missing)}/{len(items)} items still missing listed_at: {missing[:5]}...")

    print()


def export_to_excel(items: list[Item], filepath: str = "./mercari_items.xlsx"):
    """Export/append items to Excel file. Deduplicates by item id. Embeds images."""
    from datetime import datetime
    from io import BytesIO
    from openpyxl import Workbook, load_workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment

    headers = ["ID", "标题", "价格", "图片", "商品链接", "卖家", "点赞", "商品説明", "评论", "发布时间", "爬取时间"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _embed_images(ws, item, row_num):
        """Download and embed item image into column D. Converts webp to PNG."""
        if not item.image_url:
            return
        try:
            img_bytes = _curl(item.image_url, raw=True, use_proxy=True, timeout=15)
            if img_bytes and len(img_bytes) > 1000:
                img_buffer = BytesIO(img_bytes)
                if b'WEBP' in img_bytes[:40] or b'RIFF' in img_bytes[:4]:
                    from PIL import Image as PILImage
                    pil_img = PILImage.open(img_buffer)
                    img_buffer = BytesIO()
                    pil_img.save(img_buffer, format="PNG")
                img_buffer.seek(0)
                xl_img = XLImage(img_buffer)
                xl_img.width = 120
                xl_img.height = 120
                ws.add_image(xl_img, f"D{row_num}")
                ws.row_dimensions[row_num].height = 95
        except Exception:
            pass

    def _apply_styles(ws):
        """Apply column widths and text wrapping."""
        col_widths = {'A': 18, 'B': 45, 'C': 11, 'D': 18, 'E': 35,
                      'F': 14, 'G': 7, 'H': 55, 'I': 40, 'J': 12, 'K': 18}
        wrap_cols = {'B', 'H', 'I'}  # 标题, 商品説明, 评论

        for col_letter, width in col_widths.items():
            ws.column_dimensions[col_letter].width = width

        for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
            for cell in row:
                col_letter = cell.column_letter
                if col_letter in wrap_cols:
                    cell.alignment = Alignment(wrap_text=True, vertical='top')
                else:
                    cell.alignment = Alignment(vertical='top')

    if os.path.exists(filepath):
        wb = load_workbook(filepath)
        ws = wb.active

        # Build existing IDs + collect empty-row slots (from user deletions)
        existing_ids = set()
        empty_rows = []  # rows where all cells are None
        last_used_row = 1  # header
        for row in range(2, ws.max_row + 1):
            row_vals = [ws.cell(row=row, column=c).value for c in range(1, 12)]
            if any(v is not None for v in row_vals):
                last_used_row = row
                vid = row_vals[0]
                if vid:
                    existing_ids.add(str(vid))
            else:
                empty_rows.append(row)

        new_count = 0
        for it in items:
            if it.id in existing_ids:
                continue
            existing_ids.add(it.id)
            row_data = [it.id, it.name, it.price, "", it.url,
                        it.seller, it.likes, it.description, it.comments, it.listed_at, now]

            # Fill empty slots first, then append after last_used_row
            if empty_rows:
                row_num = empty_rows.pop(0)
            else:
                last_used_row += 1
                row_num = last_used_row

            for c, val in enumerate(row_data, 1):
                ws.cell(row=row_num, column=c).value = val
            _embed_images(ws, it, row_num)
            new_count += 1
            print(f"\r  [img] {new_count}/{len(items)}", end="")
        if new_count:
            _apply_styles(ws)
            print()
        wb.save(filepath)
        # Real total: scan for last non-empty row after writing
        real_last = 1
        for row in range(2, ws.max_row + 1):
            if any(ws.cell(row=row, column=c).value is not None for c in range(1, 12)):
                real_last = row
        print(f"[Excel] 追加 {new_count} 条，共 {real_last - 1} 条 → {filepath}")
    else:
        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for i, it in enumerate(items, 1):
            row_data = [it.id, it.name, it.price, "", it.url,
                        it.seller, it.likes, it.description, it.comments, it.listed_at, now]
            ws.append(row_data)
            row_num = i + 1
            _embed_images(ws, it, row_num)
            print(f"\r  [img] {i}/{len(items)}", end="")
        _apply_styles(ws)
        print()
        wb.save(filepath)
        print(f"[Excel] 新建 {len(items)} 条 → {filepath}")


def _export_update_excel(items: list[Item], filepath: str):
    """Update existing Excel rows with enriched data (description, comments, etc.).
    Only updates rows whose IDs already exist in the file."""
    from openpyxl import load_workbook

    if not os.path.exists(filepath):
        return

    wb = load_workbook(filepath)
    ws = wb.active

    # Build ID → row_num map (column A)
    id_to_row = {}
    for row in range(2, ws.max_row + 1):
        vid = ws.cell(row=row, column=1).value
        if vid:
            id_to_row[str(vid)] = row

    updated = 0
    for it in items:
        row = id_to_row.get(it.id)
        if not row:
            continue
        # Col F=卖家, G=点赞, H=商品説明, I=评论, J=发布时间
        if it.seller:
            ws.cell(row=row, column=6).value = it.seller
        if it.likes:
            ws.cell(row=row, column=7).value = it.likes
        if it.description:
            ws.cell(row=row, column=8).value = it.description
        if it.comments:
            ws.cell(row=row, column=9).value = it.comments
        if it.listed_at:
            ws.cell(row=row, column=10).value = it.listed_at
        updated += 1

    if updated:
        wb.save(filepath)
        print(f"[Excel] Updated {updated} rows with details → {filepath}")
    wb.close()


# ==================== SQLite Storage ====================
DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mercari.db")


def _init_db(db_path: str = DB_DEFAULT):
    """Create SQLite table if not exists. Migrates old schema if needed."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT,
            keyword TEXT DEFAULT '',
            name TEXT,
            price INTEGER,
            image_url TEXT,
            url TEXT,
            seller TEXT,
            likes INTEGER DEFAULT 0,
            description TEXT,
            comments TEXT,
            crawled_at TEXT,
            listed_at TEXT DEFAULT '',
            PRIMARY KEY (id, keyword)
        )
    """)
    # Migration: add missing columns to old table
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    for col in ["keyword", "listed_at"]:
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT DEFAULT ''")
            except Exception:
                pass
    # Migration: old PK was just (id), need to recreate if still old schema
    pk = conn.execute("PRAGMA table_info(items)").fetchall()
    # If 'keyword' column exists but isn't part of PK, we need to handle gracefully
    # SQLite doesn't support ALTER PK, so we just live with dupes — IGNORE handles it
    conn.commit()
    conn.close()


def save_to_sqlite(items: list[Item], db_path: str = DB_DEFAULT, keyword: str = "") -> int:
    """Insert items into SQLite. keyword tags the crawl source. Returns count of newly inserted items."""
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for it in items:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO items (id, keyword, name, price, image_url, url, seller, likes, description, comments, crawled_at, listed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (it.id, keyword, it.name, it.price, it.image_url, it.url,
                  it.seller, it.likes, it.description, it.comments, now, it.listed_at))
            if conn.execute("SELECT changes()").fetchone()[0] > 0:
                count += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return count


def get_all_items(db_path: str = DB_DEFAULT, keyword: str = "") -> list[dict]:
    """Get all items from SQLite, newest first. Pass keyword to filter."""
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if keyword:
        rows = conn.execute("SELECT * FROM items WHERE keyword=? ORDER BY crawled_at DESC, id DESC", (keyword,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM items ORDER BY crawled_at DESC, id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_keywords(db_path: str = DB_DEFAULT) -> list[str]:
    """Get list of distinct keywords in the DB, newest first."""
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT DISTINCT keyword FROM items ORDER BY keyword").fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


def _update_sqlite_item(item: Item, db_path: str = DB_DEFAULT):
    """Update an existing item's detail fields (seller, likes, description, comments, listed_at)."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        UPDATE items SET seller=COALESCE(NULLIF(?, ''), seller),
                         likes=CASE WHEN ? > 0 THEN ? ELSE likes END,
                         description=COALESCE(NULLIF(?, ''), description),
                         comments=COALESCE(NULLIF(?, ''), comments),
                         listed_at=CASE WHEN ? != '' THEN ? ELSE listed_at END
        WHERE id=?
    """, (item.seller, item.likes, item.likes, item.description, item.comments,
          item.listed_at, item.listed_at, item.id))
    conn.commit()
    conn.close()


def clear_items(db_path: str = DB_DEFAULT) -> int:
    """Delete all items. Returns deleted count."""
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.execute("DELETE FROM items")
    conn.commit()
    conn.close()
    return count


# ==================== CLI ====================
def main():
    global SMTP_USER, SMTP_PASS

    # Fix Windows GBK console encoding
    if sys.stdout.encoding != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    import argparse

    parser = argparse.ArgumentParser(description="Mercari JP Crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="One-time search")
    p.add_argument("-k", "--keyword", required=True, nargs="+", help="Search keyword(s), space-separated")
    p.add_argument("-p", "--page", type=int, default=1)
    p.add_argument("--order", default="created_time",
                   choices=["created_time", "price:asc", "price:desc", "num_likes:desc"])
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--headless", action="store_true", default=True, help="Use headless browser (default: True)")
    p.add_argument("--no-headless", action="store_false", dest="headless", help="Use curl only (deprecated, may return 0 items)")
    p.add_argument("--download", type=str, nargs="?", const="./mercari_images",
                   help="Download images to DIR (default: ./mercari_images)")
    p.add_argument("-n", "--count", type=int, default=50,
                   help="Max items (headless only, uses scroll, default: 50)")
    p.add_argument("--excel", type=str, nargs="?", const="./mercari_items.xlsx",
                   help="Export results to Excel file (default: ./mercari_items.xlsx)")
    p.add_argument("--db", type=str, nargs="?", const=DB_DEFAULT,
                   help="Save to SQLite DB (default: ./mercari.db)")

    # monitor
    p = sub.add_parser("monitor", help="Continuous monitoring for new items")
    p.add_argument("-k", "--keyword", required=True, nargs="+",
                   help="Search keyword(s), space-separated")
    p.add_argument("--interval", type=int, default=30, help="Check interval (minutes)")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--no-headless", action="store_false", dest="headless")
    p.add_argument("--excel", type=str, nargs="?", const="./mercari_monitor.xlsx",
                   help="Export new items to Excel (default: ./mercari_monitor.xlsx)")
    p.add_argument("--db", type=str, nargs="?", const=DB_DEFAULT,
                   help="Save to SQLite DB (default: ./mercari.db)")
    p.add_argument("--email", action="store_true",
                   help="Send email when new items are found (uses QQ SMTP by default)")
    p.add_argument("--health-email", action="store_true",
                   help="Send one daily email confirming the monitor is still running")
    p.add_argument("--email-to", default=SMTP_TO,
                   help="Recipient email(s), comma-separated (default: SMTP_TO env)")
    p.add_argument("--smtp-user", default=SMTP_USER,
                   help="SMTP login email (default: SMTP_USER env)")
    p.add_argument("--smtp-pass", default=SMTP_PASS,
                   help="SMTP auth code/password (default: SMTP_PASS env)")

    args = parser.parse_args()

    if args.command == "search":
        all_items = []

        multi = len(args.keyword) > 1

        for kw in args.keyword:
            result = search(kw, page=args.page, order=args.order,
                            use_headless=args.headless, max_items=args.count,
                            fetch_details=False)
            all_items.extend(result.items)

            if multi:
                header = f"Search: {kw} | Page: {result.page}"
                print(header)
                print("-" * 40)
                for i, it in enumerate(result.items, 1):
                    print(f"  {i:2d}. ¥{it.price:>8,}  {it.name[:55]}")
                if not result.items:
                    print("  (no items found)")
                print()

        if args.json:
            print(json.dumps(to_dicts(all_items), ensure_ascii=False, indent=2))
        elif not multi:
            # Single keyword: show detailed output
            header = f"Search: {args.keyword[0]} | {len(all_items)} items"
            print(header)
            print("-" * 60)
            for i, it in enumerate(all_items, 1):
                print(f"  {i:2d}. ¥{it.price:>8,}  {it.name[:55]}")
                if it.url:
                    print(f"      {it.url}")
            if not all_items:
                print("  (no items found - try --headless for JS-rendered pages)")

        # Download images if requested
        if getattr(args, "download", None) is not None:
            print(f"\n[DL] Saving images to {args.download}/ ...")
            n = download_images(all_items, args.download)
            print(f"[DL] Done: {n} images")

        # Save to SQLite DB
        db_path = getattr(args, "db", None)
        if db_path is not None:
            # Use first keyword as tag; if multi-keyword, join them
            kw_tag = " ".join(args.keyword)
            n = save_to_sqlite(all_items, db_path, keyword=kw_tag)
            print(f"[DB] Saved {n} new items → {db_path}")

        # Export to Excel: save basic items first
        excel_path = getattr(args, "excel", None)
        if excel_path is not None:
            export_to_excel(all_items, excel_path)

        # Enrich via curl — fills listed_at, seller, description (works for both DB and Excel)
        if all_items and (db_path or excel_path):
            print("[detail] Enriching via curl...")
            enrich_items(all_items, fetch_details=True)
            if db_path:
                for it in all_items:
                    if it.description or it.seller or it.likes or it.listed_at:
                        _update_sqlite_item(it, db_path)
            if excel_path:
                _export_update_excel(all_items, excel_path)

    elif args.command == "monitor":
        excel = getattr(args, "excel", None) or ""
        db = getattr(args, "db", None) or ""
        SMTP_USER = args.smtp_user or SMTP_USER
        SMTP_PASS = args.smtp_pass or SMTP_PASS
        try:
            monitor(args.keyword, interval_min=args.interval,
                    use_headless=args.headless, excel_path=excel, db_path=db,
                    email_notify=args.email, email_to=args.email_to,
                    health_email=args.health_email)
        except KeyboardInterrupt:
            print("\n[monitor] Stopped.")


if __name__ == "__main__":
    main()
