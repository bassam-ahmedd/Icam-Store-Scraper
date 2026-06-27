#!/usr/bin/env python3
"""
icamstore.net product scraper -> Google Sheets (one tab per category).

Why this is built the way it is
-------------------------------
icamstore.net is fronted by a Cloudflare *managed challenge* (Turnstile).
Plain requests/cloudscraper/headless-browsers from a datacenter IP all get
a 403 "Just a moment..." page. GitHub Actions runners are datacenter IPs too,
so they are challenged exactly the same way. The only reliable way to fetch
the site on a schedule is a commercial unlocker with residential proxies +
JS challenge solving. This uses ZenRows (already in your stack). The fetch
layer is isolated in ZenRowsClient so you can swap to ScraperAPI/Scrapfly
by changing one class.

Data source
-----------
The site runs WooCommerce, so we read the public Store API:
    /wp-json/wc/store/v1/products
which returns name, permalink, price (+currency), and is_in_stock directly.
That keeps the request count low (a few calls per category/day). If the
Store API is ever disabled, scrape_category_html() is a drop-in fallback
that parses the category listing pages instead.

Output columns per tab: Product Name | Product Link | Price | Availability
"""

import os
import re
import sys
import json
import time
import html
import logging
from urllib.parse import urlencode, quote_plus

import requests
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE_URL = "https://icamstore.net"

# Categories to scrape -> one Google Sheet tab each. Order is preserved.
CATEGORY_SLUGS = [
    "cameras",
    "lenses-accessories",
    "printers-instax-cameras",
    "professional-video",
    "batteries-power",
    "accessories",
    "lighting-studio",
    "tripods-supports",
    "gimbals-stabilizers",
    "rigs-supports",
    "storages-accessories",
    "audio",
    "mobile-equipment",
]

# Env / secrets
ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "").strip()
SHEET_ID        = os.environ.get("SHEET_ID", "").strip()
# Service-account creds: either inline JSON (GOOGLE_CREDENTIALS) or a file path (GOOGLE_SA_FILE)
GOOGLE_CREDENTIALS  = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
GOOGLE_SA_FILE  = os.environ.get("GOOGLE_SA_FILE", "service_account.json").strip()

# Proxy locale: the store is UAE-based; fetching from an AE residential IP keeps
# pricing/currency consistent with what a UAE shopper sees.
PROXY_COUNTRY   = os.environ.get("PROXY_COUNTRY", "ae").strip()

PER_PAGE        = 100          # Store API max page size
REQUEST_TIMEOUT = 90           # seconds (unlocker + JS render is slow)
MAX_RETRIES     = 2            # js_render succeeds on attempt 1; this is just transient-error insurance
RETRY_BACKOFF   = 3            # seconds, multiplied by attempt number

HEADER_ROW = ["Product Name", "Product Link", "Price", "Availability",
              "Key Features", "Description", "Image"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("icamstore")


# --------------------------------------------------------------------------- #
# Fetch layer (ZenRows unlocker). Swap this class to change provider.
# --------------------------------------------------------------------------- #

class ZenRowsClient:
    """Thin wrapper around the ZenRows Universal Scraper API."""

    ENDPOINT = "https://api.zenrows.com/v1/"

    def __init__(self, api_key: str, proxy_country: str = ""):
        if not api_key:
            raise RuntimeError(
                "ZENROWS_API_KEY is not set. The site is Cloudflare-locked and "
                "cannot be fetched without an unlocker."
            )
        self.api_key = api_key
        self.proxy_country = proxy_country
        self.session = requests.Session()

    def get(self, url: str, js_render: bool = True) -> str:
        """Fetch a URL through ZenRows.

        icamstore.net challenges *every* route (API included), so js_render +
        premium_proxy is the only combination that works. We go straight to it
        instead of wasting attempts/credits on a cheaper tier that always 422s.
        """
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            params = {
                "apikey": self.api_key,
                "url": url,
                "premium_proxy": "true",
                "js_render": "true",
            }
            if self.proxy_country:
                params["proxy_country"] = self.proxy_country
            try:
                r = self.session.get(
                    self.ENDPOINT, params=params, timeout=REQUEST_TIMEOUT
                )
                body = r.text
                if r.status_code == 200 and "Just a moment" not in body:
                    return body
                last_err = f"status={r.status_code}"
                log.warning("  fetch attempt %s -> %s", attempt, last_err)
            except requests.RequestException as e:
                last_err = repr(e)
                log.warning("  fetch attempt %s error: %s", attempt, last_err)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
        raise RuntimeError(f"Failed to fetch {url} ({last_err})")


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #

def extract_json(text: str):
    """Parse JSON that may be wrapped in browser HTML (when js_render is on)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"<pre[^>]*>(.*?)</pre>", text, re.S | re.I)
    if m:
        try:
            return json.loads(html.unescape(m.group(1)).strip())
        except json.JSONDecodeError:
            pass
    # Last resort: grab the outermost JSON array/object in the body.
    m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
    if m:
        return json.loads(m.group(1))
    raise ValueError("Response did not contain parseable JSON")


def format_price(prices: dict) -> str:
    """Build a price string from the Store API 'prices' object.

    The Store API already positions the currency text inside currency_prefix /
    currency_suffix, so we use those directly (prefix + number + suffix). We only
    fall back to the bare symbol/code if both are empty.
    """
    try:
        raw = prices.get("price")
        if raw in (None, ""):
            return ""
        minor = int(prices.get("currency_minor_unit", 2))
        value = int(raw) / (10 ** minor)
        ts = prices.get("currency_thousand_separator", ",")
        ds = prices.get("currency_decimal_separator", ".")
        num = f"{value:,.{minor}f}"
        if ts != "," or ds != ".":
            num = num.replace(",", "\0").replace(".", ds).replace("\0", ts)
        prefix = prices.get("currency_prefix", "") or ""
        suffix = prices.get("currency_suffix", "") or ""
        if not prefix and not suffix:
            sym = prices.get("currency_symbol") or prices.get("currency_code") or ""
            suffix = f" {sym}" if sym else ""
        out = re.sub(r"\s+", " ", html.unescape(f"{prefix}{num}{suffix}")).strip()
        # Some stores set BOTH a prefix and suffix currency token (e.g. "EGP16,000 EGP").
        # Collapse the duplicate, keeping a single trailing token.
        code = (prices.get("currency_code") or "").strip()
        if code and out.count(code) > 1:
            out = re.sub(r"\s+", " ", out.replace(code, "")).strip()
            out = f"{out} {code}".strip()
        return out
    except (ValueError, TypeError):
        return str(prices.get("price", ""))


def availability_text(item: dict) -> str:
    sa = item.get("stock_availability") or {}
    if isinstance(sa, dict) and sa.get("text"):
        txt = html.unescape(re.sub(r"<[^>]+>", "", sa["text"])).strip()
        if txt:
            return txt
    low = item.get("low_stock_remaining")
    if low:
        return f"Only {low} left in stock"
    if item.get("is_in_stock") is True:
        return "In stock"
    if item.get("is_in_stock") is False:
        return "Out of stock"
    if item.get("is_purchasable") is False:
        return "Unavailable"
    return "Unknown"


def html_to_text(raw: str, max_len: int = 15000) -> str:
    """Convert WooCommerce HTML (description / short_description) to readable text.

    Each paragraph / heading / table row becomes one line; list items become
    bullet lines. Inline markup is flattened, whitespace collapsed, length capped.
    """
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "html.parser")
    lines = []
    for el in soup.find_all(["li", "p", "h1", "h2", "h3", "h4", "h5", "h6", "tr"]):
        if el.name == "p" and el.find_parent("li"):
            continue  # avoid double-counting paragraphs nested in list items
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not t:
            continue
        lines.append(("• " + t) if el.name == "li" else t)
    text = "\n".join(lines)
    if not text:  # content wasn't in block tags (e.g. bare text / <br> only)
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + " …"
    return text


def image_cell(item: dict) -> str:
    """Return a Google Sheets =IMAGE() formula for the product's first image."""
    imgs = item.get("images") or []
    if imgs and isinstance(imgs, list):
        url = (imgs[0].get("thumbnail") or imgs[0].get("src") or "").strip()
        if url.startswith("http"):
            return f'=IMAGE("{url.replace(chr(34), "%22")}")'
    return ""


def safe_cell(v):
    """Neutralize accidental formula injection in text cells (names, etc.)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


# --------------------------------------------------------------------------- #
# Store API path (primary)
# --------------------------------------------------------------------------- #

def fetch_category_map(client: ZenRowsClient) -> dict:
    """Return {slug: {'id': int, 'name': str}} for all product categories."""
    cat_map = {}
    page = 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products/categories"
               f"?per_page=100&page={page}")
        log.info("Fetching category list (page %s)", page)
        data = extract_json(client.get(url, js_render=False))
        if not data:
            break
        for c in data:
            slug = c.get("slug")
            if slug:
                cat_map[slug] = {"id": c.get("id"),
                                 "name": html.unescape(c.get("name") or slug)}
        if len(data) < 100:
            break
        page += 1
    return cat_map


def fetch_products_by_category_id(client: ZenRowsClient, cat_id: int) -> list:
    """Paginate the Store API products endpoint for one category id."""
    rows = []
    page = 1
    while True:
        url = (f"{BASE_URL}/wp-json/wc/store/v1/products"
               f"?category={cat_id}&per_page={PER_PAGE}&page={page}&orderby=title&order=asc")
        data = extract_json(client.get(url, js_render=False))
        if not data:
            break
        for item in data:
            rows.append([
                html.unescape((item.get("name") or "").strip()),
                (item.get("permalink") or "").strip(),
                format_price(item.get("prices") or {}),
                availability_text(item),
                html_to_text(item.get("short_description") or ""),
                html_to_text(item.get("description") or ""),
                image_cell(item),
            ])
        log.info("    page %s -> %s products (running total %s)",
                 page, len(data), len(rows))
        if len(data) < PER_PAGE:
            break
        page += 1
        time.sleep(1)
    return rows


# --------------------------------------------------------------------------- #
# HTML fallback path (used only if the Store API yields nothing)
# --------------------------------------------------------------------------- #

def _first(el, selectors):
    for sel in selectors:
        found = el.select_one(sel)
        if found:
            return found
    return None


def scrape_category_html(client: ZenRowsClient, slug: str) -> list:
    """Parse the category listing pages directly as a fallback."""
    rows, seen = [], set()
    page = 1
    while True:
        if page == 1:
            url = f"{BASE_URL}/product-category/{slug}/"
        else:
            url = f"{BASE_URL}/product-category/{slug}/page/{page}/"
        log.info("    [html] %s", url)
        try:
            soup = BeautifulSoup(client.get(url, js_render=True), "html.parser")
        except RuntimeError:
            break

        cards = (soup.select("li.product")
                 or soup.select("ul.products li")
                 or soup.select(".product-grid-item")
                 or soup.select(".products .product"))
        if not cards:
            break

        new = 0
        for card in cards:
            link_el = _first(card, [
                "a.woocommerce-LoopProduct-link",
                "a.woocommerce-loop-product__link",
                "a.woocommerce-loop-product__title",
                'a[href*="/product/"]',
                "a",
            ])
            link = link_el.get("href", "").strip() if link_el else ""
            if not link or link in seen:
                continue
            seen.add(link)

            title_el = _first(card, [
                ".woocommerce-loop-product__title",
                "h2", "h3", ".product-title", ".product_title",
            ])
            name = title_el.get_text(strip=True) if title_el else (
                link_el.get("aria-label", "").strip() if link_el else "")

            price_el = _first(card, ["ins .woocommerce-Price-amount",
                                     ".price", ".woocommerce-Price-amount"])
            price = price_el.get_text(" ", strip=True) if price_el else ""

            classes = " ".join(card.get("class", [])).lower()
            text = card.get_text(" ", strip=True).lower()
            if "outofstock" in classes or "out-of-stock" in classes or "out of stock" in text:
                avail = "Out of stock"
            elif card.select_one("a.add_to_cart_button, a.ajax_add_to_cart"):
                avail = "In stock"
            else:
                avail = "Unknown"

            img_el = card.select_one("img")
            img_url = ""
            if img_el:
                img_url = (img_el.get("data-src") or img_el.get("src") or "").strip()
            img_cell = f'=IMAGE("{img_url.replace(chr(34), "%22")}")' if img_url.startswith("http") else ""

            rows.append([name, link, price, avail, "", "", img_cell])
            new += 1

        if new == 0:
            break
        # Stop if there's no "next page" link.
        if not soup.select_one("a.next.page-numbers, .next.page-numbers"):
            break
        page += 1
        time.sleep(1)
    return rows


# --------------------------------------------------------------------------- #
# Google Sheets output
# --------------------------------------------------------------------------- #

def get_gspread_client() -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if GOOGLE_CREDENTIALS:
        info = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif os.path.exists(GOOGLE_SA_FILE):
        creds = Credentials.from_service_account_file(GOOGLE_SA_FILE, scopes=scopes)
    else:
        raise RuntimeError(
            "No Google credentials. Set GOOGLE_CREDENTIALS (inline) or provide "
            f"{GOOGLE_SA_FILE}."
        )
    return gspread.authorize(creds)


def sanitize_tab(name: str) -> str:
    name = re.sub(r"[\[\]\:\*\?\/\\]", " ", name).strip()
    return name[:99] or "Sheet"


def write_tab(spreadsheet, tab_name: str, rows: list):
    """Clear + write one worksheet in a single batched update (quota-friendly)."""
    tab_name = sanitize_tab(tab_name)
    try:
        ws = spreadsheet.worksheet(tab_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        needed = max(len(rows) + 10, 100)
        ws = spreadsheet.add_worksheet(title=tab_name, rows=needed, cols=len(HEADER_ROW))

    # Sanitize the six text columns against formula injection; keep the image
    # formula (col 7 / G) intact. USER_ENTERED so =IMAGE() renders as a thumbnail.
    safe_rows = [[safe_cell(c) for c in r[:6]] + [r[6] if len(r) > 6 else ""]
                 for r in rows]
    values = [HEADER_ROW] + safe_rows
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    # Center the image column (G) so thumbnails sit nicely.
    try:
        ws.format("G:G", {"horizontalAlignment": "CENTER"})
    except Exception:
        pass
    log.info("  wrote %s rows to tab '%s'", len(rows), tab_name)


def is_sold_out(avail_text: str) -> bool:
    t = (avail_text or "").lower()
    return ("out of stock" in t) or ("sold out" in t) or ("unavailable" in t)


def write_summary(spreadsheet, summary: list):
    """Summary tab: Category | Products | In Stock | Sold Out, pinned first.

    `summary` rows are [category_name, products, in_stock, sold_out].
    """
    tab = "Summary"
    try:
        ws = spreadsheet.worksheet(tab)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab, rows=60, cols=4, index=0)

    from datetime import datetime
    try:
        import zoneinfo
        now = datetime.now(zoneinfo.ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %H:%M %z")
    except Exception:
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " UTC"

    tot_p = sum(int(r[1]) for r in summary)
    tot_in = sum(int(r[2]) for r in summary)
    tot_out = sum(int(r[3]) for r in summary)

    values = [["Category", "Products", "In Stock", "Sold Out"]]
    values += [[r[0], r[1], r[2], r[3]] for r in summary]
    values += [["TOTAL", tot_p, tot_in, tot_out]]
    values += [["", "", "", ""]]
    values += [["Last updated", now, "", ""]]
    values += [["Source", f'=HYPERLINK("{BASE_URL}","{BASE_URL}")', "", ""]]

    # USER_ENTERED so the HYPERLINK formula renders as a clickable link.
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")
    ws.freeze(rows=1)
    try:
        ws.format("A1:D1", {"textFormat": {"bold": True}})
        total_row = 1 + len(summary) + 1
        ws.format(f"A{total_row}:D{total_row}", {"textFormat": {"bold": True}})
    except Exception:
        pass

    # Pin Summary as the first tab and clear out the old _Run Log if present.
    try:
        others = [w for w in spreadsheet.worksheets() if w.id != ws.id]
        spreadsheet.reorder_worksheets([ws] + others)
    except Exception:
        pass
    try:
        spreadsheet.del_worksheet(spreadsheet.worksheet("_Run Log"))
    except Exception:
        pass
    # Remove stale tabs left over from the first run, whose names were
    # HTML-encoded (e.g. "Lenses &amp; Accessories"). Current runs name them
    # cleanly ("Lenses & Accessories"), so anything containing "&amp;" is an orphan.
    for w in list(spreadsheet.worksheets()):
        if "&amp;" in w.title:
            try:
                spreadsheet.del_worksheet(w)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def cairo_schedule_guard():
    """On scheduled (cron) triggers, only proceed at 05:xx or 17:xx Cairo time.

    GitHub cron is UTC and DST-unaware, so the workflow lists both the summer and
    winter UTC times for 05:30/17:30 Cairo. This guard lets only the two correct
    triggers per day actually run, year-round, with no manual seasonal changes.
    Manual ('workflow_dispatch') and local runs always proceed.
    """
    if os.environ.get("GITHUB_EVENT_NAME", "") != "schedule":
        return
    try:
        import zoneinfo
        from datetime import datetime
        hour = datetime.now(zoneinfo.ZoneInfo("Africa/Cairo")).hour
    except Exception:
        return  # if tz resolution fails, don't block the run
    if hour not in (5, 17):
        log.info("Cairo guard: current Cairo hour is %s (not 5 or 17) - skipping this trigger.", hour)
        sys.exit(0)


def main():
    cairo_schedule_guard()
    if not SHEET_ID:
        log.error("SHEET_ID env var is required.")
        sys.exit(1)

    client = ZenRowsClient(ZENROWS_API_KEY, proxy_country=PROXY_COUNTRY)
    gc = get_gspread_client()
    spreadsheet = gc.open_by_key(SHEET_ID)

    # Map the requested slugs to their category ids via the Store API.
    try:
        cat_map = fetch_category_map(client)
    except Exception as e:
        log.warning("Could not load category map (%s); will use HTML fallback.", e)
        cat_map = {}

    summary = []
    for slug in CATEGORY_SLUGS:
        meta = cat_map.get(slug)
        tab_name = (meta or {}).get("name") or slug.replace("-", " ").title()
        log.info("Category: %s -> tab '%s'", slug, tab_name)

        rows = []
        if meta and meta.get("id"):
            try:
                rows = fetch_products_by_category_id(client, meta["id"])
            except Exception as e:
                log.warning("  Store API failed for %s (%s)", slug, e)

        if not rows:
            log.info("  falling back to HTML scrape for %s", slug)
            try:
                rows = scrape_category_html(client, slug)
            except Exception as e:
                log.error("  HTML fallback also failed for %s (%s)", slug, e)

        # De-duplicate by product link, keep order.
        seen, deduped = set(), []
        for r in rows:
            if r[1] and r[1] not in seen:
                seen.add(r[1])
                deduped.append(r)

        write_tab(spreadsheet, tab_name, deduped)
        sold = sum(1 for r in deduped if is_sold_out(r[3]))
        in_stock = len(deduped) - sold
        summary.append([sanitize_tab(tab_name), len(deduped), in_stock, sold])
        time.sleep(1)

    write_summary(spreadsheet, summary)
    total = sum(s[1] for s in summary)
    log.info("Done. %s products across %s categories.", total, len(summary))


if __name__ == "__main__":
    main()
