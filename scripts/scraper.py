"""
Fetches all programmes from silecpdcentre.sg/calas/ and their detail pages,
then writes events.json for the dashboard frontend.

Usage:
    python scripts/scraper.py

Requires:
    pip install -r requirements.txt
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── constants ─────────────────────────────────────────────────────────────────

BASE_URL    = "https://www.silecpdcentre.sg"
LISTING_URL = f"{BASE_URL}/calas/"
DETAIL_BASE = f"{BASE_URL}/EventDetails/?EventID="
OUT_FILE    = Path(__file__).parent.parent / "events.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CALAS-Dashboard-Scraper/1.0; "
        "+https://github.com/your-username/calas-dashboard)"
    )
}

DELAY_SECONDS = 0.6   # polite delay between requests (~100 req/min max)
TIMEOUT       = 30


# ── helpers ───────────────────────────────────────────────────────────────────

MONTH_MAP = {
    "Jan": 1, "Feb": 2,  "Mar": 3,  "Apr": 4,
    "May": 5, "Jun": 6,  "Jul": 7,  "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_date(raw: str) -> str | None:
    """
    Parse a date string like "Wednesday 17 Jun 2026 - 01:00 PM"
    and return "2026-06-17", or None if parsing fails.
    """
    if not raw:
        return None
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", raw)
    if not m:
        return None
    day, mon_str, year = m.group(1), m.group(2), m.group(3)
    month = MONTH_MAP.get(mon_str)
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def field_after(soup: BeautifulSoup, label: str) -> str | None:
    """
    Find the element whose exact text matches `label` (leaf / near-leaf nodes
    only), then return the text of its next sibling or parent's next sibling.
    """
    for el in soup.find_all(True):
        if len(el.find_all(True, recursive=False)) > 2:
            continue
        if el.get_text(strip=True) == label:
            sibling = el.find_next_sibling()
            if sibling:
                text = sibling.get_text(separator=" ", strip=True)
                if text:
                    return text
            if el.parent:
                parent_sibling = el.parent.find_next_sibling()
                if parent_sibling:
                    text = parent_sibling.get_text(separator=" ", strip=True)
                    if text:
                        return text
    return None


def detect_format(title: str) -> str:
    t = title.lower()
    if "[webinar & in-person]" in t:
        return "Hybrid"
    if "[webinar]" in t or "[webinar &" in t:
        return "Webinar / Online"
    return "In-person"


# ── listing page ──────────────────────────────────────────────────────────────

def scrape_listing() -> list[dict]:
    print("Fetching listing page…")
    resp = requests.get(LISTING_URL, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    seen   = set()
    events = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"EventID=(\d+)", href, re.IGNORECASE)
        if not m:
            continue
        event_id = int(m.group(1))
        if event_id in seen:
            continue
        seen.add(event_id)

        raw_title = a.get_text(strip=True)
        if not raw_title:
            continue

        events.append({
            "id":        event_id,
            "title":     raw_title,
            "mandatory": "*" in raw_title,
        })

    print(f"  Found {len(events)} events on listing page.")
    return events


# ── detail page ───────────────────────────────────────────────────────────────

def scrape_detail(event: dict) -> dict:
    url  = f"{DETAIL_BASE}{event['id']}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    body_text = soup.get_text(separator=" ")

    # ── dates ──
    from_m = re.search(r"From\s+([\w\s,]+\d{4})", body_text)
    to_m   = re.search(r"\bTo\s+([\w\s,]+\d{4})", body_text)
    date_from = parse_date(from_m.group(1)) if from_m else None
    date_to   = parse_date(to_m.group(1))   if to_m   else None

    # ── structured fields ──
    venue          = field_after(soup, "Venue")
    organiser      = field_after(soup, "Organiser")
    practice_area  = field_after(soup, "Practice Area")
    mec_segment    = field_after(soup, "MEC segment")

    # ── points — label is "Public MEC Points" or "Public CPD Points" ──
    mec_pts_raw = field_after(soup, "Public MEC Points")
    cpd_pts_raw = field_after(soup, "Public CPD Points")

    pts_value: float | None = None
    pts_type  = "cpd"

    def try_float(s: str | None) -> float | None:
        if not s:
            return None
        try:
            return float(re.search(r"[\d.]+", s).group())
        except (AttributeError, ValueError):
            return None

    mec_val = try_float(mec_pts_raw)
    cpd_val = try_float(cpd_pts_raw)

    if mec_val is not None:
        pts_value = mec_val
        pts_type  = "mec"
    elif cpd_val is not None:
        pts_value = cpd_val
        pts_type  = "cpd"

    # ── event outline ──
    outline = None
    for el in soup.find_all(True):
        if len(el.find_all(True, recursive=False)) == 0:
            if el.get_text(strip=True) == "Event Outline":
                sibling = el.find_next_sibling()
                if sibling:
                    outline = sibling.get_text(separator=" ", strip=True) or None
                if not outline and el.parent:
                    ps = el.parent.find_next_sibling()
                    if ps:
                        outline = ps.get_text(separator=" ", strip=True) or None
                break

    # ── external link ──
    event_link = None
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if (h.startswith("http")
                and "silecpdcentre" not in h
                and "sile.edu" not in h):
            event_link = h
            break

    clean_title = re.sub(r"\*", "", event["title"]).strip()

    return {
        "id":            event["id"],
        "title":         clean_title,
        "mandatory":     event["mandatory"],
        "format":        detect_format(event["title"]),
        "dateFrom":      date_from,
        "dateTo":        date_to,
        "venue":         venue,
        "organiser":     organiser,
        "practiceArea":  practice_area,
        "mecSegment":    mec_segment,
        "ptsValue":      pts_value,
        "ptsType":       pts_type,
        "outline":       outline,
        "eventLink":     event_link or f"{DETAIL_BASE}{event['id']}",
        "detailUrl":     f"{DETAIL_BASE}{event['id']}",
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    start = time.time()

    # 1. listing page
    listing = scrape_listing()

    # 2. detail pages with polite delay
    results: list[dict] = []
    errors:  list[dict] = []

    for i, ev in enumerate(listing, start=1):
        label = f"[{i:>3}/{len(listing)}] Event {ev['id']}"
        print(f"  {label} … ", end="", flush=True)
        try:
            time.sleep(DELAY_SECONDS)
            detail = scrape_detail(ev)
            results.append(detail)
            pts_info = f"{detail['ptsType'].upper()} {detail['ptsValue'] or '—'} pts"
            print(f"OK  ({pts_info})")
        except Exception as exc:
            print(f"ERROR: {exc}")
            errors.append({"id": ev["id"], "title": ev["title"], "error": str(exc)})
            # Push a skeleton so the event still appears in the dashboard
            results.append({
                "id":            ev["id"],
                "title":         re.sub(r"\*", "", ev["title"]).strip(),
                "mandatory":     ev["mandatory"],
                "format":        "Unknown",
                "dateFrom":      None,
                "dateTo":        None,
                "venue":         None,
                "organiser":     None,
                "practiceArea":  None,
                "mecSegment":    None,
                "trainingLevel": None,
                "ptsValue":      None,
                "ptsType":       "cpd",
                "outline":       None,
                "eventLink":     f"{DETAIL_BASE}{ev['id']}",
                "detailUrl":     f"{DETAIL_BASE}{ev['id']}",
            })

    # 3. sort by dateFrom ascending, nulls last
    results.sort(key=lambda e: (e["dateFrom"] is None, e["dateFrom"] or ""))

    # 4. write output
    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "count":       len(results),
        "errorCount":  len(errors),
        "events":      results,
    }

    OUT_FILE.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s — {len(results)} events written to events.json")

    if errors:
        ids = ", ".join(str(e["id"]) for e in errors)
        print(f"  {len(errors)} error(s): {ids}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()