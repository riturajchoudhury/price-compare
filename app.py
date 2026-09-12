"""
Local Amazon.in / Flipkart price checker (Streamlit + sync Playwright).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import streamlit as st
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# playwright-stealth compatibility (v1 stealth_sync vs v2 Stealth API)
# ---------------------------------------------------------------------------
try:
    from playwright_stealth import stealth_sync as _stealth_sync
except ImportError:
    from playwright_stealth import Stealth

    def _stealth_sync(page: Page) -> None:
        Stealth().apply_stealth_sync(page)


def stealth_sync(page: Page) -> None:
    """Apply stealth evasions before navigation."""
    _stealth_sync(page)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BROWSER_SESSION_DIR = "./browser_session"
AMAZON_SEARCH = "https://www.amazon.in/s?k={q}"
FLIPKART_SEARCH = "https://www.flipkart.com/search?q={q}"

CAPTCHA_HINTS = (
    "/sorry/",
    "enter the characters",
    "opfcaptcha",
    "type the characters",
    "robot check",
    "access denied",
    "captcha",
    "validatecaptcha",
    "automated access",
    "api-services-support@amazon.com",
    "we just need to make sure",
    "something went wrong",
    "request could not be satisfied",
    "shield",
    "cf-chl",
    "challenge",
)


class CaptchaOrBlockError(Exception):
    """Raised when a captcha / bot block is detected (triggers tenacity retry)."""


class ScrapeError(Exception):
    """Non-fatal scrape failure that should still retry (empty organic results, etc.)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _page_looks_blocked(page: Page) -> bool:
    url = (page.url or "").lower()
    try:
        body = (page.content() or "").lower()
    except Exception:
        body = ""
    blob = f"{url}\n{body[:80000]}"
    return any(h in blob for h in CAPTCHA_HINTS)


def _raise_if_blocked(page: Page, site: str) -> None:
    if _page_looks_blocked(page):
        raise CaptchaOrBlockError(f"{site}: captcha or block detected at {page.url}")


def _absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)


def _amazon_canonical_dp(url: str) -> str:
    """Keep a clean /dp/ASIN product URL when possible."""
    m = re.search(r"(/dp/[A-Z0-9]{10})", url, re.I)
    if m:
        return f"https://www.amazon.in{m.group(1)}"
    parsed = urlparse(url)
    if not parsed.scheme:
        return _absolute_url("https://www.amazon.in", url)
    return url.split("?")[0]


def _walk_jsonld(node: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        types = node.get("@type")
        type_list = types if isinstance(types, list) else ([types] if types else [])
        type_names = {str(t).lower() for t in type_list}
        if "product" in type_names:
            found.append(node)
        if "@graph" in node:
            found.extend(_walk_jsonld(node["@graph"]))
        for v in node.values():
            found.extend(_walk_jsonld(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_jsonld(item))
    return found


def _offers_from_product(product: dict) -> dict:
    offers = product.get("offers") or product.get("Offers") or {}
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        offers = {}
    return offers


def extract_from_jsonld(page: Page) -> dict[str, Any]:
    """Parse Product + offers from application/ld+json blocks."""
    scripts = page.locator('script[type="application/ld+json"]')
    count = scripts.count()
    products: list[dict] = []
    for i in range(count):
        raw = scripts.nth(i).inner_text(timeout=2000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Some pages concatenate multiple JSON objects; try line-ish salvage
            try:
                data = json.loads(raw.strip().rstrip(";"))
            except json.JSONDecodeError:
                continue
        products.extend(_walk_jsonld(data))

    if not products:
        return {}

    product = products[0]
    offers = _offers_from_product(product)
    price = offers.get("price") or offers.get("lowPrice")
    currency = offers.get("priceCurrency") or "INR"
    availability = str(offers.get("availability") or "")
    title = product.get("name") or ""

    available = True
    if availability:
        avail_l = availability.lower()
        if "outofstock" in avail_l or "soldout" in avail_l or "discontinued" in avail_l:
            available = False

    # Extract image URL from JSON-LD if present
    image = product.get("image")
    image_url: str | None = None
    if isinstance(image, list) and image:
        # Take the first image URL if it is a list
        # Handle case where list items might be dicts (ImageObject)
        first = image[0]
        if isinstance(first, dict) and "url" in first:
            image_url = first["url"]
        elif isinstance(first, str):
            image_url = first
        else:
            # Fallback: try to convert to string
            image_url = str(first)
    elif isinstance(image, str):
        image_url = image
    elif isinstance(image, dict) and "url" in image:
        image_url = image["url"]

    result = {
        "title": title,
        "price": str(price) if price is not None else None,
        "currency": currency,
        "availability": availability,
        "available": available,
        "image": image_url,
    }
    return result


_PRICE_RE = re.compile(r"(?:₹|Rs\.?)\s*([\d,]+(?:\.\d+)?)", re.I)
_BAD_PRICE_CONTEXT = re.compile(
    r"(m\.?r\.?p|list\s*price|emi|/month|per\s*month|with\s*exchange|"
    r"exchange\s*value|save\s*₹|you\s*save|coupon|without\s*exchange|"
    r"as\s*low\s*as|from\s*₹)",
    re.I,
)


def _normalize_price_number(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw).replace(",", ""))
    if not cleaned:
        return None
    # Drop trailing dots / empty fractions
    if cleaned.count(".") > 1:
        cleaned = cleaned.split(".")[0]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    # Prefer integer rupees when fraction is .00
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _price_from_offscreen_text(text: str) -> str | None:
    text = " ".join((text or "").split())
    if not text or _BAD_PRICE_CONTEXT.search(text):
        return None
    m = _PRICE_RE.search(text)
    if not m:
        # Sometimes a-offscreen is only digits: "54900.00"
        if re.fullmatch(r"[\d,]+(?:\.\d+)?", text.strip()):
            return _normalize_price_number(text)
        return None
    return _normalize_price_number(m.group(1))


def extract_amazon_buybox_price(page: Page) -> str | None:
    """
    Target Amazon's live selling price in the buybox — not MRP, EMI, or exchange.
    Uses stable buybox regions + .a-offscreen (a11y price), excluding .a-text-price (MRP).
    """
    # Core price feature IDs are Amazon's long-lived buybox hooks (not weekly hashed classes).
    roots = (
        "#corePriceDisplay_desktop_feature_div",
        "#corePrice_feature_div",
        "#corePriceDisplay_mobile_feature_div",
        "#apex_desktop",
        "#apex_desktop_newAccordionRow",
        "#ppd",
    )
    offscreen_selectors = (
        "span.a-price:not(.a-text-price) > span.a-offscreen",
        "span.aok-offscreen",
    )

    for root in roots:
        root_loc = page.locator(root)
        if root_loc.count() == 0:
            continue
        for sel in offscreen_selectors:
            nodes = root_loc.locator(sel)
            try:
                n = min(nodes.count(), 6)
            except Exception:
                n = 0
            for i in range(n):
                try:
                    text = nodes.nth(i).inner_text(timeout=1000)
                except Exception:
                    try:
                        text = nodes.nth(i).text_content(timeout=1000) or ""
                    except Exception:
                        continue
                price = _price_from_offscreen_text(text)
                if price:
                    return price

        # Reconstruct from whole + fraction inside the first non-MRP a-price
        whole = root_loc.locator("span.a-price:not(.a-text-price) span.a-price-whole")
        if whole.count() > 0:
            try:
                whole_txt = whole.first.inner_text(timeout=1000)
                frac_loc = root_loc.locator(
                    "span.a-price:not(.a-text-price) span.a-price-fraction"
                )
                frac_txt = ""
                if frac_loc.count() > 0:
                    frac_txt = frac_loc.first.inner_text(timeout=500)
                combined = f"{whole_txt}.{frac_txt}" if frac_txt else whole_txt
                price = _normalize_price_number(combined)
                if price:
                    return price
            except Exception:
                pass

    # Text fallback: lines that look like a selling price, never MRP/EMI
    for label in ("Deal Price", "With Deal", "Price"):
        loc = page.get_by_text(label, exact=False)
        try:
            n = min(loc.count(), 10)
        except Exception:
            n = 0
        for i in range(n):
            try:
                text = loc.nth(i).inner_text(timeout=1000)
            except Exception:
                continue
            if _BAD_PRICE_CONTEXT.search(text):
                continue
            # Skip EMI-style "Price ₹999/month"
            if re.search(r"/month|emi", text, re.I):
                continue
            m = _PRICE_RE.search(text)
            if m:
                return _normalize_price_number(m.group(1))

    return None


def _first_rupee_text(page: Page) -> str | None:
    """Flipkart-oriented fallback: first plausible ₹ amount, skipping MRP/EMI context."""
    try:
        body = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return None

    for m in _PRICE_RE.finditer(body):
        start = max(0, m.start() - 40)
        end = min(len(body), m.end() + 40)
        ctx = body[start:end]
        if _BAD_PRICE_CONTEXT.search(ctx):
            continue
        price = _normalize_price_number(m.group(1))
        if price:
            return price
    return None


def _delivery_text(page: Page, site: str) -> str:
    if site == "amazon":
        needles = (
            "Get it by",
            "Deliver to",
            "Currently unavailable",
            "Cannot be delivered",
            "Out of stock",
            "In stock",
            "FREE delivery",
        )
    else:
        needles = (
            "Delivery by",
            "Get by",
            "Delivered by",
            "Not deliverable",
            "Currently out of stock",
            "Out of Stock",
            "Free delivery",
        )

    snippets: list[str] = []
    for needle in needles:
        loc = page.get_by_text(needle, exact=False)
        try:
            n = min(loc.count(), 3)
        except Exception:
            n = 0
        for i in range(n):
            try:
                t = loc.nth(i).inner_text(timeout=1000).strip()
            except Exception:
                continue
            # Keep a short readable line
            line = " ".join(t.split())
            if line and line not in snippets:
                snippets.append(line[:240])
        if snippets:
            break
    return " | ".join(snippets) if snippets else "Delivery info not found"


def _infer_available(delivery: str, jsonld: dict) -> bool:
    if "available" in jsonld:
        return bool(jsonld["available"])
    low = delivery.lower()
    bad = (
        "currently unavailable",
        "cannot be delivered",
        "out of stock",
        "not deliverable",
        "currently out of stock",
    )
    return not any(b in low for b in bad)


def dismiss_flipkart_login(page: Page) -> None:
    """Close Flipkart login modal if present (text-based, no brittle classes)."""
    try:
        close = page.get_by_text("✕", exact=True)
        if close.count() > 0:
            close.first.click(timeout=1500)
            return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Search: organic product link extraction
# ---------------------------------------------------------------------------
def amazon_first_organic_url(page: Page, keyword: str, timeout_ms: int) -> str:
    q = quote_plus(keyword)
    url = AMAZON_SEARCH.format(q=q)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1500)
    _raise_if_blocked(page, "Amazon.in")

    cards = page.locator('div[data-component-type="s-search-result"][data-asin]')
    try:
        cards.first.wait_for(state="attached", timeout=min(timeout_ms, 12000))
    except PlaywrightTimeoutError as exc:
        _raise_if_blocked(page, "Amazon.in")
        raise CaptchaOrBlockError("Amazon.in: search results did not load (page delayed or bot-blocked)") from exc

    count = cards.count()
    for i in range(count):
        card = cards.nth(i)
        asin = (card.get_attribute("data-asin") or "").strip()
        if not asin:
            continue
        try:
            text = card.inner_text(timeout=2000)
        except Exception:
            text = ""
        if re.search(r"\bsponsored\b", text, re.I):
            continue

        href = None
        for sel in ('h2 a[href]', 'a.a-link-normal[href*="/dp/"]'):
            link = card.locator(sel)
            if link.count() == 0:
                continue
            href = link.first.get_attribute("href")
            if href and "/dp/" in href:
                break
            href = None

        if not href:
            continue
        return _amazon_canonical_dp(_absolute_url("https://www.amazon.in", href))

    raise ScrapeError("Amazon.in: no organic (non-sponsored) product found")


def flipkart_first_organic_url(page: Page, keyword: str, timeout_ms: int) -> str:
    q = quote_plus(keyword)
    url = FLIPKART_SEARCH.format(q=q)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1500)
    dismiss_flipkart_login(page)
    _raise_if_blocked(page, "Flipkart")

    links = page.locator('a[href*="/p/"]')
    try:
        links.first.wait_for(state="attached", timeout=min(timeout_ms, 12000))
    except PlaywrightTimeoutError as exc:
        _raise_if_blocked(page, "Flipkart")
        raise CaptchaOrBlockError("Flipkart: search results did not load (page delayed or bot-blocked)") from exc

    count = min(links.count(), 40)
    for i in range(count):
        a = links.nth(i)
        href = a.get_attribute("href") or ""
        if "/p/" not in href or "/search?" in href:
            continue
        # Skip sponsored "Ad" cards: check nearby card text for a standalone Ad marker
        try:
            # Climb a few ancestors for card text without relying on Flipkart class names
            card_text = a.evaluate(
                """(el) => {
                    let n = el;
                    for (let i = 0; i < 6 && n; i++) {
                        n = n.parentElement;
                    }
                    return n ? (n.innerText || '') : (el.innerText || '');
                }"""
            )
        except Exception:
            card_text = ""
        # Flipkart sponsored rows typically show a lone "Ad" badge
        if re.search(r"(^|\n)\s*Ad\s*(\n|$)", card_text) or re.search(
            r"\bSponsored\b", card_text, re.I
        ):
            continue

        full = _absolute_url("https://www.flipkart.com", href)
        # Drop tracking query noise but keep path
        return full.split("?")[0]

    raise ScrapeError("Flipkart: no organic (non-ad) product found")


def scrape_product_page(page: Page, url: str, site: str, timeout_ms: int) -> dict[str, Any]:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(1200)
    if site == "flipkart":
        dismiss_flipkart_login(page)
    _raise_if_blocked(page, site)

    jsonld = extract_from_jsonld(page)
    title = jsonld.get("title") or ""
    if not title:
        try:
            title = page.title()
        except Exception:
            title = ""

    currency = jsonld.get("currency") or "INR"
    price: str | None = None

    if site == "amazon":
        # Prefer buybox selling price — JSON-LD / body scan often hit MRP or EMI.
        price = extract_amazon_buybox_price(page)
        if not price:
            price = _normalize_price_number(jsonld.get("price"))
    else:
        price = _normalize_price_number(jsonld.get("price"))
        if not price:
            rupee = _first_rupee_text(page)
            if rupee:
                price = _normalize_price_number(rupee)

    if not price and site == "amazon":
        # Last resort only after buybox + JSON-LD
        rupee = _first_rupee_text(page)
        if rupee:
            price = _normalize_price_number(rupee)

    delivery = _delivery_text(page, "amazon" if site == "amazon" else "flipkart")
    available = _infer_available(delivery, jsonld)
    image_url = jsonld.get("image")
    if isinstance(image_url, list) and image_url:
        # Take the first image URL if it is a list
        # Handle case where list items might be dicts (ImageObject)
        first = image_url[0]
        if isinstance(first, dict) and "url" in first:
            image_url = first["url"]
        elif isinstance(first, str):
            image_url = first
        else:
            # Fallback: try to convert to string
            image_url = str(first)
    elif isinstance(image_url, str):
        image_url = image_url
    elif isinstance(image_url, dict) and "url" in image_url:
        image_url = image_url["url"]

    # Fallback: extract image from page if not found in JSON-LD
    if not image_url:
        image_url = _extract_image_from_page(page, site)

    return {
        "site": "Amazon.in" if site == "amazon" else "Flipkart",
        "title": title,
        "url": page.url,
        "price": price,
        "currency": currency,
        "delivery": delivery,
        "available": available,
        "error": None,
        "image": image_url,
    }


def _extract_image_from_page(page: Page, site: str) -> str | None:
    """Extract product image URL from page using site-specific selectors."""
    try:
        if site == "amazon":
            # Try Amazon's main image selector
            landing_img = page.locator('#landingImage')
            if landing_img.count() > 0:
                src = landing_img.first.get_attribute('src')
                if src:
                    return src
            # Try data-old-hires attribute (often higher resolution)
            old_hires_img = page.locator('img[data-old-hires]')
            if old_hires_img.count() > 0:
                src = old_hires_img.first.get_attribute('data-old-hires')
                if src:
                    return src
        elif site == "flipkart":
            # Flipkart common image selectors (fallback)
            # Try to find image with common patterns
            img_selectors = [
                'img._396cs4._3exPp9',  # common class pattern
                'img._1Nyybr._30XEf0',   # another pattern
                'img[src*="rukmini1.flixcart.com"]',  # Flipkart image domain
            ]
            for selector in img_selectors:
                img = page.locator(selector)
                if img.count() > 0:
                    src = img.first.get_attribute('src')
                    if src:
                        return src
            # Generic fallback: try to find any prominent image
            # Look for images with reasonable dimensions (heuristic)
            imgs = page.locator('img')
            count = imgs.count()
            if count > 0:
                # Check first few images for plausible product image attributes
                for i in range(min(count, 5)):
                    img = imgs.nth(i)
                    src = img.get_attribute('src')
                    alt = img.get_attribute('alt') or ''
                    width = img.get_attribute('width')
                    height = img.get_attribute('height')
                    # Heuristic: image src looks like a product image URL
                    if src and ('http' in src) and (
                        'image' in src.lower() or
                        'photo' in src.lower() or
                        any(domain in src for domain in ['rukmini1.flixcart.com', 'amazon.in', 'ssl-images-amazon.com'])
                    ):
                        # Prefer images with alt text or dimensions
                        if alt or (width and height and int(width) > 100 and int(height) > 100):
                            return src
                        # Otherwise return first plausible image
                        return src
        # Generic fallback for any site
        imgs = page.locator('img')
        count = imgs.count()
        if count > 0:
            # Check first few images for plausible product image attributes
            for i in range(min(count, 5)):
                img = imgs.nth(i)
                src = img.get_attribute('src')
                alt = img.get_attribute('alt') or ''
                width = img.get_attribute('width')
                height = img.get_attribute('height')
                # Heuristic: image src looks like a product image URL
                if src and ('http' in src) and (
                    'image' in src.lower() or
                    'photo' in src.lower() or
                    any(domain in src for domain in ['rukmini1.flixcart.com', 'amazon.in', 'ssl-images-amazon.com'])
                ):
                    # Prefer images with alt text or dimensions
                    if alt or (width and height and int(width) > 100 and int(height) > 100):
                        return src
                    # Otherwise return first plausible image
                    return src
    except Exception:
        pass
    return None


def empty_result(site_label: str, error: str) -> dict[str, Any]:
    return {
        "site": site_label,
        "title": None,
        "url": None,
        "price": None,
        "currency": None,
        "delivery": None,
        "available": None,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Main scrape (tenacity retry)
# ---------------------------------------------------------------------------
@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=3),
    retry=retry_if_exception_type(
        (CaptchaOrBlockError, ScrapeError, PlaywrightTimeoutError)
    ),
)
def scrape_site(page: Page, site: str, keyword: str, timeout_ms: int) -> dict[str, Any]:
    """Search one store, open first organic product, extract price + delivery."""
    if site == "amazon":
        product_url = amazon_first_organic_url(page, keyword, timeout_ms)
        return scrape_product_page(page, product_url, "amazon", timeout_ms)
    product_url = flipkart_first_organic_url(page, keyword, timeout_ms)
    return scrape_product_page(page, product_url, "flipkart", timeout_ms)


def scrape_both(
    keyword: str, headless: bool, timeout_ms: int, log_fn: Any = None
) -> dict[str, dict[str, Any]]:
    """Search both sites using one persistent tab; each site has its own retries."""
    def log(msg: str) -> None:
        if log_fn:
            log_fn(msg)

    # Linux and Cloud servers do not have an active monitor; always force headless
    if sys.platform != "win32" or os.environ.get("RENDER") or os.environ.get("PORT") or not os.environ.get("DISPLAY"):
        headless = True

    results: dict[str, dict[str, Any]] = {}
    with sync_playwright() as p:
        log("🚀 Initializing Chromium browser...")
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=BROWSER_SESSION_DIR,
                headless=headless,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except Exception:
            context = p.chromium.launch_persistent_context(
                user_data_dir=BROWSER_SESSION_DIR,
                headless=True,
                locale="en-IN",
                timezone_id="Asia/Kolkata",
                viewport={"width": 1366, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        try:
            if context.pages:
                page = context.pages[0]
            else:
                page = context.new_page()

            stealth_sync(page)

            # Block heavy resources to reduce RAM, bandwidth, and load times
            def route_interceptor(route: Any) -> None:
                if route.request.resource_type in {"image", "media", "font"}:
                    route.abort()
                else:
                    route.continue_()
            
            context.route("**/*", route_interceptor)

            log(f"🛒 Searching Amazon.in for '{keyword}'...")
            try:
                results["amazon"] = scrape_site(page, "amazon", keyword, timeout_ms)
                log("✅ Amazon.in: search and extract complete")
            except Exception as exc:
                results["amazon"] = empty_result("Amazon.in", str(exc))
                log(f"⚠️ Amazon.in: {exc}")

            log(f"🛍️ Searching Flipkart for '{keyword}'...")
            try:
                results["flipkart"] = scrape_site(page, "flipkart", keyword, timeout_ms)
                log("✅ Flipkart: search and extract complete")
            except Exception as exc:
                results["flipkart"] = empty_result("Flipkart", str(exc))
                log(f"⚠️ Flipkart: {exc}")

            return results
        finally:
            context.close()


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------
def render_result(col, data: dict[str, Any]) -> None:
    with col:
        st.subheader(data.get("site") or "Result")
        if data.get("error"):
            st.error(data["error"])
            return
        if data.get("title"):
            st.markdown(f"**{data['title']}**")
        if data.get("url"):
            st.markdown(f"[Open product]({data['url']})")
        # Display product image if available
        image_url = data.get("image")
        if image_url:
            try:
                st.image(image_url, width=200)
            except Exception:
                pass
        price = data.get("price")
        currency = data.get("currency") or "INR"
        if price:
            st.metric("Price", f"{currency} {price}")
        else:
            st.warning("Price not found")
        delivery = data.get("delivery") or "—"
        st.write("**Delivery / availability**")
        st.write(delivery)
        avail = data.get("available")
        if avail is True:
            st.success("Likely available / deliverable")
        elif avail is False:
            st.warning("Unavailable or not deliverable")


def main() -> None:
    st.set_page_config(page_title="India Price Compare", layout="wide")
    st.title("Amazon.in vs Flipkart — local price check")
    st.caption(
        "Type a product name. The app searches both stores, skips sponsored ads, "
        "and reads price + delivery from the first organic product page."
    )

    is_cloud = bool(sys.platform != "win32" or os.environ.get("RENDER") or os.environ.get("PORT"))

    with st.sidebar:
        st.header("Settings")
        if is_cloud:
            st.info("☁️ **Cloud Deployment**: Running headless in Render container.")
            headed = False
        else:
            headed = st.checkbox("Headed (warm-up)", value=False)

        timeout_s = st.slider("Page timeout (seconds)", 10, 60, 25)

        if not is_cloud:
            st.markdown(
                """
**One-time warm-up**

1. Enable **Headed (warm-up)** above
2. Search any product
3. In the browser window, set your **Kolkata pincode** on Amazon.in and Flipkart
4. Solve captchas / login once if asked
5. Close the app — cookies stay in `./browser_session`
                """
            )
        else:
            st.caption(
                "💡 Note: Cloud datacenter IPs may occasionally be blocked or delayed by Amazon/Flipkart. "
                "For guaranteed residential IP bypass, run locally with Cloudflare Tunnel."
            )

    keyword = st.text_input(
        "Product name or keyword",
        placeholder="iPhone 15 128GB",
    )
    run = st.button("Compare prices", type="primary", disabled=not bool(keyword.strip()))

    if run and keyword.strip():
        headless = True if is_cloud else (not headed)
        timeout_ms = int(timeout_s) * 1000

        with st.status("🔍 Searching Amazon.in and Flipkart...", expanded=True) as status_box:
            try:
                results = scrape_both(
                    keyword.strip(),
                    headless=headless,
                    timeout_ms=timeout_ms,
                    log_fn=status_box.write,
                )
                status_box.update(
                    label="✅ Search finished", state="complete", expanded=False
                )
            except Exception as exc:
                status_box.update(
                    label="⚠️ Search encountered an error", state="error", expanded=True
                )
                st.error(f"Scrape failed: {exc}")
                return

        c1, c2 = st.columns(2)
        render_result(c1, results.get("amazon") or empty_result("Amazon.in", "No data"))
        render_result(c2, results.get("flipkart") or empty_result("Flipkart", "No data"))


if __name__ == "__main__":
    main()