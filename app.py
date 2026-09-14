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


class ProductMatchError(Exception):
    """Search results loaded, but none matched the requested product closely."""


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


def _recover_amazon_continue_interstitial(
    page: Page, timeout_ms: int, expected_url: str
) -> None:
    """Clear Amazon's button-only 'Continue shopping' checkpoint once."""
    if page.locator("#productTitle").count() > 0:
        return
    try:
        body_text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        return
    if "continue shopping" not in body_text.lower():
        return

    controls = page.locator(
        'button:has-text("Continue shopping"), '
        'input[type="submit"][value*="Continue shopping"], '
        'a:has-text("Continue shopping")'
    )
    if controls.count() > 0:
        try:
            controls.first.click(timeout=3000)
        except Exception:
            pass
    else:
        # Some variants submit the checkpoint automatically after its script runs.
        page.wait_for_timeout(1200)

    ready = page.locator(
        '#productTitle, div[data-component-type="s-search-result"][data-asin]'
    )
    page.wait_for_timeout(500)
    if ready.count() == 0 and expected_url:
        # The checkpoint commonly redirects to Amazon's homepage. Its cookie is now
        # cleared, so revisit the one URL the user originally requested.
        _navigate(page, expected_url, timeout_ms)

    try:
        ready.first.wait_for(
            state="attached", timeout=min(timeout_ms, 6000)
        )
    except PlaywrightTimeoutError:
        # Leave genuine/uncleared blocks to the normal detector and error message.
        pass


def _absolute_url(base: str, href: str) -> str:
    return urljoin(base, href)


def _navigate(page: Page, url: str, timeout_ms: int) -> None:
    """Navigate without depending on Amazon/Flipkart finishing every page script."""
    # Honor the timeout selected in the UI. Amazon can take 30–60 seconds to send
    # its first response to Render even when the request ultimately succeeds.
    page.goto(url, wait_until="commit", timeout=timeout_ms)
    try:
        page.wait_for_load_state(
            "domcontentloaded", timeout=min(timeout_ms, 8000)
        )
    except PlaywrightTimeoutError:
        # On cloud IPs Amazon can keep a script/redirect pending even though the
        # response body and product DOM are already usable. Later site-specific
        # locators decide whether enough content actually arrived.
        pass


def _result_wait_timeout(timeout_ms: int) -> int:
    is_cloud = bool(
        sys.platform != "win32" or os.environ.get("RENDER") or os.environ.get("PORT")
    )
    return min(timeout_ms, 6000 if is_cloud else 12000)


def _amazon_canonical_dp(url: str) -> str:
    """Keep a clean /dp/ASIN product URL when possible."""
    m = re.search(r"(/dp/[A-Z0-9]{10})", url, re.I)
    if m:
        return f"https://www.amazon.in{m.group(1)}"
    parsed = urlparse(url)
    if not parsed.scheme:
        return _absolute_url("https://www.amazon.in", url)
    return url.split("?")[0]


_MATCH_STOP_WORDS = {"a", "an", "and", "for", "in", "of", "on", "the", "with"}
_VARIANT_WORDS = {"air", "lite", "max", "mini", "plus", "pro", "ultra"}


def _match_tokens(text: str) -> list[str]:
    """Normalize product text while preserving model numbers and capacities."""
    normalized = re.sub(r"(?<=\d)\s+(?=(?:gb|tb)\b)", "", text.lower())
    return [
        token
        for token in re.findall(r"\d+(?:\.\d+)?[a-z]+|\d+(?:\.\d+)?|[a-z]+", normalized)
        if token not in _MATCH_STOP_WORDS
    ]


def _product_match_score(keyword: str, candidate: str) -> float:
    """Score a search card against the query and penalize conflicting variants."""
    query_tokens = _match_tokens(keyword)
    candidate_tokens = set(_match_tokens(candidate))
    if not query_tokens or not candidate_tokens:
        return -1000.0

    score = 0.0
    for token in query_tokens:
        if token in candidate_tokens:
            score += 6.0 if any(ch.isdigit() for ch in token) else 2.0
        else:
            score -= 10.0 if any(ch.isdigit() for ch in token) else 2.5

    query_numbers = {t for t in query_tokens if any(ch.isdigit() for ch in t)}
    if not query_numbers.issubset(candidate_tokens):
        score -= 100.0

    query_variants = set(query_tokens) & _VARIANT_WORDS
    if not query_variants.issubset(candidate_tokens):
        score -= 100.0
    extra_variants = (candidate_tokens & _VARIANT_WORDS) - query_variants
    score -= 8.0 * len(extra_variants)
    return score


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
    products: list[dict] = []
    try:
        # script elements are non-visible and Flipkart frequently replaces them
        # during hydration. Snapshot textContent in one call rather than waiting on
        # each locator with inner_text(), which can time out on a changing page.
        raw_scripts = page.locator(
            'script[type="application/ld+json"]'
        ).evaluate_all("nodes => nodes.map(node => node.textContent || '')")
    except Exception:
        raw_scripts = []

    for raw in raw_scripts:
        if not isinstance(raw, str) or not raw.strip():
            continue
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


def _format_capacity(value: str, unit: str) -> str:
    number = value.rstrip("0").rstrip(".") if "." in value else value
    return f"{number} {unit.upper()}"


def _extract_memory_specs(
    page: Page, title: str, site: str
) -> tuple[str | None, str | None]:
    """Extract explicitly labelled RAM and storage without inventing values."""
    scoped_parts = [title]
    title_selector = "#productTitle" if site == "amazon" else "h1"
    try:
        page_titles = page.locator(title_selector)
        if page_titles.count() > 0:
            scoped_parts.append(page_titles.first.inner_text(timeout=1000))
    except Exception:
        pass

    if site == "amazon":
        for selector in (
            "#feature-bullets",
            "#productDetails_techSpec_section_1",
            "#productDetails_detailBullets_sections1",
            "#detailBullets_feature_div",
        ):
            try:
                section = page.locator(selector)
                if section.count() > 0:
                    scoped_parts.append(section.first.inner_text(timeout=1000))
            except Exception:
                pass
    else:
        # Flipkart's compact product highlights are part of the main product area;
        # stop before recommendations, where other products' RAM values appear.
        try:
            body = page.locator("body").inner_text(timeout=2500)
            highlight_start = body.lower().find("product highlights")
            if highlight_start >= 0:
                scoped_parts.append(body[highlight_start : highlight_start + 700])
        except Exception:
            pass

    text = "\n".join(scoped_parts)
    title_text = " ".join(scoped_parts[:2])

    ram_patterns = (
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s*(?:of\s+)?RAM\b",
        r"\bRAM\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|TB)\b",
    )
    storage_patterns = (
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s*(?:internal\s+)?storage\b",
        r"\b(?:internal\s+)?storage\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|TB)\b",
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s+ROM\b",
        r"\bROM\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(GB|TB)\b",
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s*(?:SSD|HDD)\b",
    )

    def first_match(patterns: tuple[str, ...]) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return _format_capacity(match.group(1), match.group(2))
        return None

    ram = first_match(ram_patterns)
    storage = first_match(storage_patterns)

    # Many Android titles use the compact, well-established "8GB+128GB" form.
    compact = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s*[+/]\s*"
        r"(\d+(?:\.\d+)?)\s*(GB|TB)\b",
        title_text,
        re.I,
    )
    if compact:
        ram = ram or _format_capacity(compact.group(1), compact.group(2))
        storage = storage or _format_capacity(compact.group(3), compact.group(4))

    # Common phone-title form: "(Color, 8GB, 128GB Storage)".
    labelled_pair = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\s*,\s*"
        r"(\d+(?:\.\d+)?)\s*(GB|TB)\s*(?:Storage|ROM)\b",
        title_text,
        re.I,
    )
    if labelled_pair:
        ram = ram or _format_capacity(labelled_pair.group(1), labelled_pair.group(2))
        storage = storage or _format_capacity(
            labelled_pair.group(3), labelled_pair.group(4)
        )

    # Apple does not advertise RAM. A single capacity in an iPhone/iPad title is
    # storage; deliberately leave RAM unknown rather than inferring it.
    if not storage and re.search(r"\b(?:iphone|ipad)\b", title_text, re.I):
        capacities = re.findall(
            r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\b", title_text, re.I
        )
        if len(capacities) == 1:
            storage = _format_capacity(*capacities[0])

    return ram, storage


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
        # Amazon's buy box has dedicated primary/secondary delivery slots. Searching
        # the entire page also finds dates from alternate sellers and carousels.
        delivery_roots = page.locator("#mir-layout-DELIVERY_BLOCK")
        if delivery_roots.count() > 0:
            messages: list[str] = []
            # Amazon can render several responsive copies of this ID. Collect all
            # slot nodes across them; the first copy often lacks fastest delivery.
            slots = page.locator(
                '#mir-layout-DELIVERY_BLOCK '
                '[id^="mir-layout-DELIVERY_BLOCK-slot-"]'
            )
            targets = [slots.nth(i) for i in range(slots.count())]
            if not targets:
                targets = [delivery_roots.nth(i) for i in range(delivery_roots.count())]
            for target in targets:
                try:
                    message = " ".join(target.inner_text(timeout=1000).split())
                except Exception:
                    continue
                message = re.sub(r"\s+Details\s*$", "", message, flags=re.I)
                duplicate_key = message.rstrip(". ").lower()
                if message and not any(
                    existing.rstrip(". ").lower() == duplicate_key
                    for existing in messages
                ):
                    messages.append(message)
            if messages:
                fastest = next(
                    (
                        message
                        for message in messages
                        if re.search(r"fastest\s+delivery|order\s+within", message, re.I)
                    ),
                    None,
                )
                primary_candidates = [
                    message for message in messages if message != fastest
                ]
                primary = next(
                    (
                        message
                        for message in primary_candidates
                        if re.match(r"(?:Or\s+)?FREE\s+delivery", message, re.I)
                    ),
                    primary_candidates[0] if primary_candidates else None,
                )
                selected = [message for message in (primary, fastest) if message]
                if selected:
                    return " | ".join(selected)

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
                node = loc.nth(i)
                if site == "flipkart":
                    # Flipkart renders "Delivery by" and its date in sibling
                    # elements. Walk upward to the smallest ancestor containing
                    # both instead of returning only the label.
                    t = node.evaluate(
                        """(el) => {
                            const own = (el.innerText || '').trim();
                            let node = el;
                            for (let i = 0; i < 5 && node.parentElement; i++) {
                                const parent = node.parentElement;
                                const text = (parent.innerText || '').trim();
                                if (text.length > own.length && text.length <= 300) {
                                    return text;
                                }
                                node = parent;
                            }
                            return own;
                        }"""
                    )
                else:
                    t = node.inner_text(timeout=1000).strip()
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
    _navigate(page, url, timeout_ms)
    page.wait_for_timeout(1500)
    _recover_amazon_continue_interstitial(page, timeout_ms, url)
    _raise_if_blocked(page, "Amazon.in")

    cards = page.locator('div[data-component-type="s-search-result"][data-asin]')
    try:
        cards.first.wait_for(state="attached", timeout=_result_wait_timeout(timeout_ms))
    except PlaywrightTimeoutError as exc:
        _raise_if_blocked(page, "Amazon.in")
        raise CaptchaOrBlockError("Amazon.in: search results did not load (page delayed or bot-blocked)") from exc

    raw_candidates = cards.evaluate_all(
        """(nodes) => nodes.slice(0, 40).map((card) => {
            const links = [...card.querySelectorAll('a[href*="/dp/"]')];
            const link = links.find((a) => a.closest('h2')) || links[0] || null;
            const heading = card.querySelector('h2[aria-label]');
            return {
                asin: (card.getAttribute('data-asin') || '').trim(),
                href: link ? (link.getAttribute('href') || '') : '',
                title: heading ? (heading.getAttribute('aria-label') || '') : '',
                text: card.innerText || ''
            };
        })"""
    )

    candidates: list[tuple[float, str]] = []
    for item in raw_candidates:
        asin = str(item.get("asin") or "").strip()
        if not asin:
            continue
        text = str(item.get("text") or "")
        if re.search(r"\bsponsored\b", text, re.I):
            continue

        href = str(item.get("href") or "")
        if "/dp/" not in href:
            continue
        title = str(item.get("title") or "")
        candidates.append(
            (
                _product_match_score(keyword, title or text),
                _amazon_canonical_dp(_absolute_url("https://www.amazon.in", href)),
            )
        )

    if candidates:
        best_score, best_url = max(candidates, key=lambda item: item[0])
        if best_score >= 0:
            return best_url
        raise ProductMatchError(
            "Amazon.in: no result closely matched the requested product"
        )

    raise ScrapeError("Amazon.in: no organic (non-sponsored) product found")


def flipkart_first_organic_url(page: Page, keyword: str, timeout_ms: int) -> str:
    q = quote_plus(keyword)
    url = FLIPKART_SEARCH.format(q=q)
    _navigate(page, url, timeout_ms)
    page.wait_for_timeout(1500)
    dismiss_flipkart_login(page)
    _raise_if_blocked(page, "Flipkart")

    links = page.locator('a[href*="/p/"]')
    try:
        links.first.wait_for(state="attached", timeout=_result_wait_timeout(timeout_ms))
    except PlaywrightTimeoutError as exc:
        _raise_if_blocked(page, "Flipkart")
        raise CaptchaOrBlockError("Flipkart: search results did not load (page delayed or bot-blocked)") from exc

    # Read candidate metadata in one browser call. Calling Playwright separately
    # for every attribute/text field is especially slow on blocked or degraded pages.
    raw_candidates = links.evaluate_all(
        """(nodes) => nodes.slice(0, 40).map((a) => {
            let card = a;
            for (let i = 0; i < 5 && card.parentElement; i++) {
                card = card.parentElement;
            }
            const img = a.querySelector('img[alt]');
            return {
                href: a.getAttribute('href') || '',
                title: [
                    a.getAttribute('title') || '',
                    a.getAttribute('aria-label') || '',
                    a.innerText || '',
                    img ? (img.getAttribute('alt') || '') : ''
                ].filter(Boolean).join(' '),
                cardText: card ? (card.innerText || '') : (a.innerText || '')
            };
        })"""
    )

    candidates: list[tuple[float, str]] = []
    for item in raw_candidates:
        href = str(item.get("href") or "")
        if "/p/" not in href or "/search?" in href:
            continue
        card_text = str(item.get("cardText") or "")
        # Flipkart sponsored rows typically show a lone "Ad" badge
        if re.search(r"(^|\n)\s*Ad\s*(\n|$)", card_text) or re.search(
            r"\bSponsored\b", card_text, re.I
        ):
            continue

        candidate_text = str(item.get("title") or "") or card_text
        full = _absolute_url("https://www.flipkart.com", href)
        candidates.append(
            (_product_match_score(keyword, candidate_text), full.split("?")[0])
        )

    if candidates:
        best_score, best_url = max(candidates, key=lambda item: item[0])
        if best_score >= 0:
            return best_url
        raise ProductMatchError(
            "Flipkart: no result closely matched the requested product"
        )

    raise ScrapeError("Flipkart: no organic (non-ad) product found")


def scrape_product_page(page: Page, url: str, site: str, timeout_ms: int) -> dict[str, Any]:
    _navigate(page, url, timeout_ms)
    page.wait_for_timeout(1200)
    if site == "flipkart":
        dismiss_flipkart_login(page)
    else:
        _recover_amazon_continue_interstitial(page, timeout_ms, url)
    _raise_if_blocked(page, site)

    ready_selector = "#productTitle" if site == "amazon" else "h1"
    try:
        page.locator(ready_selector).first.wait_for(
            state="attached", timeout=min(timeout_ms, 6000)
        )
    except PlaywrightTimeoutError:
        # JSON-LD may still provide a usable product result.
        pass

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

    # Do not scan arbitrary Amazon body text for a price. Coupons, exchange
    # values, accessories and alternate sellers can all appear before the buybox.

    delivery = _delivery_text(page, "amazon" if site == "amazon" else "flipkart")
    available = _infer_available(delivery, jsonld)
    ram, storage = _extract_memory_specs(page, title, site)
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
        "ram": ram,
        "storage": storage,
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
        "ram": None,
        "storage": None,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Main scrape (tenacity retry)
# ---------------------------------------------------------------------------
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

            # Product image URLs remain available in DOM/JSON-LD without downloading
            # the image bytes, which keeps Chromium's memory and load time bounded.
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
        spec_left, spec_right = st.columns(2)
        spec_left.caption("RAM")
        spec_left.markdown(f"**{data.get('ram') or 'Not specified'}**")
        spec_right.caption("STORAGE")
        spec_right.markdown(f"**{data.get('storage') or 'Not specified'}**")
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

        timeout_s = st.slider("Page timeout (seconds)", 10, 90, 60)

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
