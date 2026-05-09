"""
domain_crawler.py — Website crawler for the Domain Registry feature.

First crawl: full BFS crawl up to crawl_depth pages, stores everything.
Subsequent crawls: incremental — only updates pages whose content has changed
  (detected via SHA-256 hash of extracted body text). Unchanged pages are skipped.

Scheduled: once per month (1st of month, 2 AM) via APScheduler.
On-demand: POST /api/admin/domains/{id}/crawl triggers immediately in a background thread.

Dependencies (add to requirements.txt):
    beautifulsoup4>=4.12
    lxml>=5.0
"""

import hashlib
import logging
import re
import time
import json
from collections import deque
from typing import Optional
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

import database as db

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CRAWL_DELAY_SECONDS = 1.0          # polite rate limit — 1 request per second
REQUEST_TIMEOUT     = 10           # seconds per HTTP request
MAX_BODY_EXCERPT    = 2000         # chars stored in domain_pages.body_excerpt
MAX_CONTEXT_TOKENS  = 1500         # approx token budget for AI context block
USER_AGENT          = (
    "GDC-Marketing-Bot/1.0 "
    "(site intelligence crawler for internal campaign AI; "
    "contact: info@graftondentalcare.com)"
)

# CTA patterns — button/link texts that indicate a call-to-action
_CTA_PATTERNS = re.compile(
    r"\b(book|schedule|appointment|call|contact|get started|free consult|"
    r"request|learn more|new patient|emergency|financing|insurance|"
    r"invisalign|implant|whitening|veneers|all-on-4|all on 4|smile)\b",
    re.IGNORECASE,
)

# Tags whose text content we skip (navigation noise)
_SKIP_TAGS = {"script", "style", "noscript", "header", "footer", "nav", "aside"}


# ── Robots.txt helper ─────────────────────────────────────────────────────────

def _build_robots_parser(base_url: str) -> Optional[RobotFileParser]:
    """Fetch and parse robots.txt for base_url. Returns None on failure."""
    robots_url = base_url.rstrip("/") + "/robots.txt"
    try:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp
    except Exception as e:
        logger.warning(f"domain_crawler: could not fetch robots.txt for {base_url}: {e}")
        return None


def _is_allowed(rp: Optional[RobotFileParser], url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# ── Page extraction ───────────────────────────────────────────────────────────

def extract_page_data(url: str, html: str, base_url: str) -> dict:
    """
    Parse raw HTML and return a dict with:
        page_title, meta_description, h1_text, body_excerpt,
        word_count, has_form, cta_phrases, internal_links
    """
    soup = BeautifulSoup(html, "lxml")

    # Title
    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else ""

    # Meta description
    meta_desc = ""
    for m in soup.find_all("meta"):
        if m.get("name", "").lower() == "description":
            meta_desc = m.get("content", "").strip()
            break

    # H1
    h1_tag = soup.find("h1")
    h1_text = h1_tag.get_text(strip=True) if h1_tag else ""

    # Body text — skip noisy tags, collapse whitespace
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    body_text = soup.get_text(separator=" ", strip=True)
    body_text = re.sub(r"\s{2,}", " ", body_text).strip()
    word_count = len(body_text.split())
    body_excerpt = body_text[:MAX_BODY_EXCERPT]

    # Forms
    has_form = bool(soup.find("form"))

    # CTA phrases — unique, lowercased matched phrases from buttons/links
    cta_set = set()
    for el in soup.find_all(["a", "button"]):
        text = el.get_text(strip=True)
        if text and len(text) < 60 and _CTA_PATTERNS.search(text):
            cta_set.add(text[:60])
    cta_phrases = sorted(cta_set)[:20]  # cap at 20

    # Internal links — same-domain hrefs, deduped
    parsed_base = urlparse(base_url)
    internal_set = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(url, href)
        full, _ = urldefrag(full)  # strip fragment
        parsed_full = urlparse(full)
        # same netloc (ignore www. prefix differences)
        if _normalize_netloc(parsed_full.netloc) == _normalize_netloc(parsed_base.netloc):
            internal_set.add(full.rstrip("/"))
    internal_links = sorted(internal_set)[:50]  # cap at 50

    return {
        "page_title":       page_title,
        "meta_description": meta_desc,
        "h1_text":          h1_text,
        "body_excerpt":     body_excerpt,
        "word_count":       word_count,
        "has_form":         has_form,
        "cta_phrases":      cta_phrases,
        "internal_links":   internal_links,
    }


def _normalize_netloc(netloc: str) -> str:
    """Strip www. prefix and lowercase for comparison."""
    return re.sub(r"^www\.", "", netloc.lower())


# ── Main crawler ──────────────────────────────────────────────────────────────

def crawl_domain(domain_id: int) -> None:
    """
    BFS crawl of a registered domain.

    - Respects robots.txt
    - Rate-limited to CRAWL_DELAY_SECONDS between requests
    - Crawls up to domain.crawl_depth pages
    - Stores results in domain_pages (upsert) and domain_crawl_log
    - Updates domain_registry.crawl_status, last_crawled_at, pages_found
    """
    domain = db.get_domain(domain_id)
    if not domain:
        logger.error(f"domain_crawler: domain {domain_id} not found")
        return

    domain_url = domain["domain_url"].rstrip("/")
    max_pages   = int(domain.get("crawl_depth") or 20)

    is_first_crawl = not domain.get("last_crawled_at")
    crawl_mode     = "full" if is_first_crawl else "incremental"
    logger.info(
        f"domain_crawler: starting {crawl_mode} crawl of {domain_url} "
        f"(max {max_pages} pages)"
    )

    # Mark as running
    db.update_domain(domain_id, crawl_status="running")
    log_id = db.insert_crawl_log(domain_id)

    # For incremental crawls, load existing page hashes so we can skip unchanged pages.
    # For first crawl, existing_hashes is empty — everything gets written.
    existing_hashes: dict[str, str] = {}
    if not is_first_crawl:
        for p in db.list_domain_pages(domain_id):
            existing_hashes[p["page_url"].rstrip("/") + "/"] = p.get("content_hash", "")

    rp = _build_robots_parser(domain_url)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    visited        = set()
    queue          = deque([domain_url + "/"])
    pages_crawled  = 0   # new or updated
    pages_unchanged = 0  # skipped (hash matched)
    pages_failed   = 0

    try:
        while queue and (pages_crawled + pages_unchanged) < max_pages:
            url = queue.popleft()
            url, _ = urldefrag(url)
            url = url.rstrip("/") + "/"

            if url in visited:
                continue
            visited.add(url)

            if not _is_allowed(rp, url):
                logger.debug(f"domain_crawler: robots.txt disallows {url}")
                continue

            # Polite delay (skip for first request)
            if pages_crawled + pages_unchanged > 0:
                time.sleep(CRAWL_DELAY_SECONDS)

            try:
                resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                if resp.status_code != 200:
                    logger.debug(f"domain_crawler: {url} → HTTP {resp.status_code}")
                    pages_failed += 1
                    continue

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    continue  # skip PDFs, images, etc.

                # Incremental check — hash the raw HTML to detect changes
                content_hash = hashlib.sha256(resp.text.encode("utf-8", errors="replace")).hexdigest()
                if not is_first_crawl and existing_hashes.get(url) == content_hash:
                    pages_unchanged += 1
                    # Still enqueue links from this page so we discover new pages
                    # but we do it cheaply from cached internal_links in DB
                    continue

                data = extract_page_data(url, resp.text, domain_url)
                data["content_hash"] = content_hash
                db.upsert_domain_page(domain_id=domain_id, page_url=url, **data)
                pages_crawled += 1

                # Enqueue newly discovered internal links
                for link in data["internal_links"]:
                    link_norm = link.rstrip("/") + "/"
                    if link_norm not in visited:
                        queue.append(link_norm)

            except requests.RequestException as e:
                logger.warning(f"domain_crawler: request failed for {url}: {e}")
                pages_failed += 1

        # Success
        total_pages = pages_crawled + pages_unchanged
        db.update_domain(
            domain_id,
            crawl_status="done",
            last_crawled_at=db._now(),
            pages_found=total_pages,
        )
        db.finish_crawl_log(
            log_id,
            pages_crawled=pages_crawled,
            pages_failed=pages_failed,
            status="done",
        )
        logger.info(
            f"domain_crawler: finished {domain_url} ({crawl_mode}) — "
            f"{pages_crawled} updated, {pages_unchanged} unchanged, {pages_failed} failed"
        )

    except Exception as e:
        logger.error(f"domain_crawler: crawl of {domain_url} crashed: {e}", exc_info=True)
        db.update_domain(domain_id, crawl_status="error")
        db.finish_crawl_log(log_id, status="error", error_msg=str(e)[:500])


# ── AI context builder ────────────────────────────────────────────────────────

def build_site_context(domain_id: int, max_pages: int = 10) -> str:
    """
    Return a compact markdown block summarising the crawled site.
    Injected into campaign-generation AI prompts.

    Format (≈1500 tokens max):
        ## Website Intelligence: <domain_url>
        ### <page_title> (<page_url>)
        Meta: ...
        H1: ...
        CTA phrases: Book Appointment, New Patients, ...
        Excerpt: first 300 chars...
        ---
    """
    domain = db.get_domain(domain_id)
    if not domain:
        return ""

    pages = db.list_domain_pages(domain_id)
    if not pages:
        return ""

    domain_url   = domain.get("domain_url", "")
    domain_label = domain.get("label") or domain_url

    lines = [f"## Website Intelligence: {domain_label} ({domain_url})"]

    # Sort: homepage first, then pages with forms (high-value), then by word count desc
    def _page_sort_key(p):
        parsed = urlparse(p["page_url"])
        is_home = parsed.path in ("", "/", "//")
        return (0 if is_home else 1, 0 if p["has_form"] else 1, -p["word_count"])

    sorted_pages = sorted(pages, key=_page_sort_key)[:max_pages]

    for p in sorted_pages:
        lines.append(f"\n### {p['page_title'] or p['page_url']}")
        lines.append(f"URL: {p['page_url']}")
        if p.get("meta_description"):
            lines.append(f"Meta: {p['meta_description'][:150]}")
        if p.get("h1_text"):
            lines.append(f"H1: {p['h1_text'][:100]}")
        ctas = p.get("cta_phrases") or []
        if ctas:
            lines.append(f"CTAs: {', '.join(ctas[:8])}")
        if p.get("has_form"):
            lines.append("Has form: yes")
        excerpt = (p.get("body_excerpt") or "")[:300]
        if excerpt:
            lines.append(f"Content: {excerpt}")
        lines.append("---")

    context = "\n".join(lines)

    # Hard cap — truncate if over budget (rough 4 chars/token estimate)
    char_limit = MAX_CONTEXT_TOKENS * 4
    if len(context) > char_limit:
        context = context[:char_limit] + "\n...(truncated)"

    return context


def build_site_context_for_url(domain_url: str) -> str:
    """
    Convenience wrapper — looks up domain by URL and returns context block.
    Returns empty string if domain not registered or not yet crawled.
    """
    # Normalise: strip trailing slash, ensure scheme
    url = domain_url.rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url

    # Try exact match first, then scheme-agnostic
    domain = db.get_domain_by_url(url)
    if not domain:
        # Try without www
        parsed = urlparse(url)
        alt = parsed._replace(netloc=_normalize_netloc(parsed.netloc))
        domain = db.get_domain_by_url(alt.geturl())

    if not domain:
        return ""

    return build_site_context(domain["id"])


# ── Scheduler entry point ─────────────────────────────────────────────────────

def domain_crawl_scheduled_job() -> None:
    """
    Called by APScheduler on the 1st of every month at 2 AM.
    Crawls any enabled domain that either:
      - has never been crawled (last_crawled_at is empty) — full crawl, or
      - was last crawled more than 30 days ago — incremental crawl (diff only).
    Skips domains with crawl_status='running' (prevents overlap).
    """
    from datetime import datetime, timezone, timedelta

    domains = db.list_domains()
    now     = datetime.now(timezone.utc)
    cutoff  = timedelta(days=30)

    for d in domains:
        if not d.get("crawl_enabled"):
            continue
        if d.get("crawl_status") == "running":
            logger.info(f"domain_crawl_job: {d['domain_url']} still running — skipping")
            continue

        last = d.get("last_crawled_at") or ""
        needs_crawl = False
        if not last:
            needs_crawl = True
        else:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                needs_crawl = (now - last_dt) >= cutoff
            except Exception:
                needs_crawl = True

        if needs_crawl:
            logger.info(f"domain_crawl_job: crawling {d['domain_url']}")
            try:
                crawl_domain(d["id"])
            except Exception as e:
                logger.error(f"domain_crawl_job: error crawling {d['domain_url']}: {e}")
