"""
Scrapes the logged-in user's eBird life list and writes it to data/lifelist.json
in the format the gallery site expects.

Two data sources are combined:
  1. The user's own life list page (scraped via authenticated Playwright session) —
     gives species name, species code, first-observed date, and location.
  2. eBird's public Taxonomy API (sanctioned, key-based, reference data only —
     not personal data) — gives scientific name, family, and order for each
     species code found in step 1.

Requires two things in the environment:
  EBIRD_EMAIL / EBIRD_PASSWORD — your login, used only for step 1.
  EBIRD_API_KEY — a free key from https://ebird.org/api/keygen, used only for
                   step 2 (public taxonomy lookup).
"""

import os
import sys
import json
import time
import re
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright

EBIRD_LOGIN_URL = "https://secure.birds.cornell.edu/cassso/login?service=https%3A%2F%2Febird.org%2Flogin%2Fcas%3Fportal%3Debird"
EBIRD_LIFELIST_URL = "https://ebird.org/lifelist"
TAXONOMY_API_URL = "https://api.ebird.org/v2/ref/taxonomy/ebird"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "lifelist.json")


def login(page, email, password):
    page.goto(EBIRD_LOGIN_URL)
    page.fill("#input-user-name", email)
    page.fill("#input-password", password)
    page.click("#form-submit")
    page.wait_for_url("https://ebird.org/**", timeout=20000)


def scroll_to_load_all(page, max_scrolls=40):
    """Life lists can lazy-load more rows as you scroll. Keep scrolling until
    the row count stops growing, or we hit the safety cap."""
    previous_count = -1
    for _ in range(max_scrolls):
        current_count = len(page.query_selector_all("li.Observation"))
        if current_count == previous_count:
            break
        previous_count = current_count
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(600)


def sanitize_personal_location(raw_text):
    """Given the visible text of a personal (non-hotspot) location link,
    keeps only the town/region/country portion and discards the rest.

    eBird formats a personal location's saved address as multiple lines,
    e.g.:
        12 Example House, Sample Lane   <- street/house: DISCARDED
        Anytown                         <- town: kept
        England                         <- region: kept
        AB1 2CD                         <- postcode: DISCARDED
        United Kingdom                  <- country: kept

    The raw_text passed in is never stored anywhere — this function reads it
    once, extracts only the safe middle portion, and returns that. The
    caller must not retain raw_text after calling this.
    """
    if not raw_text:
        return None
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
    if len(lines) <= 1:
        return None  # single-line label — ambiguous, safer to skip entirely

    remaining = lines[1:]  # drop the first line (street/house — most identifying)

    def looks_like_postcode(line):
        return len(line) <= 10 and any(ch.isdigit() for ch in line)

    remaining = [line for line in remaining if not looks_like_postcode(line)]
    return ", ".join(remaining) if remaining else None


def scrape_via_dom(page):
    page.goto(EBIRD_LIFELIST_URL)
    try:
        page.wait_for_selector("li.Observation", timeout=20000)
    except Exception:
        page.screenshot(path="debug_failure.png", full_page=True)
        raise
    scroll_to_load_all(page)

    rows = page.query_selector_all("li.Observation")
    birds = []

    for row in rows:
        name_el = row.query_selector(".Heading-main")
        code_el = row.query_selector("a[data-species-code]")
        date_el = row.query_selector(".Observation-meta-date a")
        location_els = row.query_selector_all(".Observation-meta-location a")

        if not name_el or not code_el:
            continue

        date_text = date_el.inner_text().strip() if date_el else ""
        try:
            date_iso = datetime.strptime(date_text, "%d %b %Y").strftime("%Y-%m-%d")
        except ValueError:
            date_iso = date_text  # fall back to raw text if format ever changes

        location_link = location_els[0] if location_els else None
        loc_id = None
        if location_link:
            href = location_link.get_attribute("href") or ""
            match = re.search(r"r=(L\d+)", href)
            if match:
                loc_id = match.group(1)
        region_code = location_els[1].inner_text().strip() if len(location_els) > 1 else ""

        # Read the raw text once, sanitize it immediately, then let it go out
        # of scope — the raw address text (which may contain a street and
        # postcode for personal locations) is never stored.
        raw_location_text = location_link.text_content() if location_link else ""
        personal_location_guess = sanitize_personal_location(raw_location_text)

        birds.append({
            "commonName": name_el.inner_text().strip(),
            "speciesCode": code_el.get_attribute("data-species-code"),
            "dateFirstHeard": date_iso,
            "locId": loc_id,
            "regionCode": region_code,
            "personalLocationGuess": personal_location_guess,
        })

    return birds


def enrich_with_location_names(birds, api_key):
    """Resolves each hotspot ID to coordinates via eBird's public hotspot
    reference API, then reverse-geocodes those coordinates via OpenStreetMap's
    free Nominatim service to get village/town, county, and country.

    For personal (non-hotspot) locations — e.g. a home address — the hotspot
    API has no data. In that case this falls back to the sanitized
    town/region/country guess extracted during scraping, and only falls back
    further to a bare country/region name if even that isn't available."""
    loc_ids = sorted(set(b["locId"] for b in birds if b.get("locId")))
    region_codes = sorted(set(b["regionCode"] for b in birds if b.get("regionCode")))

    precise_location_map = {}
    for loc_id in loc_ids:
        try:
            hotspot_resp = requests.get(
                f"https://api.ebird.org/v2/ref/hotspot/info/{loc_id}",
                headers={"X-eBirdApiToken": api_key},
                timeout=15,
            )
            if hotspot_resp.status_code != 200:
                continue  # not a public hotspot — will fall back below
            hotspot = hotspot_resp.json()
            lat, lng = hotspot.get("latitude"), hotspot.get("longitude")
            if lat is None or lng is None:
                continue

            time.sleep(1)  # Nominatim's usage policy caps at 1 request/second
            geo_resp = requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"format": "jsonv2", "lat": lat, "lon": lng, "zoom": 14, "addressdetails": 1},
                headers={"User-Agent": "BirdList-personal-app (github.com/olipollock/BirdList)"},
                timeout=15,
            )
            geo_resp.raise_for_status()
            address = geo_resp.json().get("address", {})

            settlement = (
                address.get("village") or address.get("town") or
                address.get("city") or address.get("suburb") or
                address.get("hamlet") or ""
            )
            county = address.get("county") or address.get("state_district") or ""
            country = address.get("country") or ""

            precise_location_map[loc_id] = ", ".join(part for part in [settlement, county, country] if part)

        except requests.RequestException:
            continue  # will fall back below

    region_name_map = {}
    for code in region_codes:
        try:
            resp = requests.get(
                f"https://api.ebird.org/v2/ref/region/info/{code}",
                headers={"X-eBirdApiToken": api_key},
                timeout=15,
            )
            resp.raise_for_status()
            region_name_map[code] = resp.json().get("result", code)
        except requests.RequestException:
            region_name_map[code] = code
        time.sleep(0.3)

    for bird in birds:
        precise = precise_location_map.get(bird.get("locId"))
        personal_guess = bird.get("personalLocationGuess")
        fallback = region_name_map.get(bird.get("regionCode"), "")
        bird["location"] = precise or personal_guess or fallback
        del bird["locId"]
        del bird["regionCode"]
        del bird["personalLocationGuess"]

    return birds


def enrich_with_taxonomy(birds, api_key):
    """Looks up scientific name, family, and order for each species code via
    eBird's public taxonomy reference API — sanctioned, key-based, no login."""
    codes = [b["speciesCode"] for b in birds if b.get("speciesCode")]
    taxonomy_map = {}

    # Batch in chunks to keep the URL a reasonable length
    chunk_size = 100
    for i in range(0, len(codes), chunk_size):
        chunk = codes[i:i + chunk_size]
        resp = requests.get(
            TAXONOMY_API_URL,
            params={"species": ",".join(chunk), "fmt": "json"},
            headers={"X-eBirdApiToken": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        for entry in resp.json():
            taxonomy_map[entry["speciesCode"]] = {
                "scientificName": entry.get("sciName", ""),
                "family": entry.get("familyComName", entry.get("familySciName", "")),
                "order": entry.get("order", ""),
            }
        time.sleep(0.5)  # be polite to the API

    for bird in birds:
        tax = taxonomy_map.get(bird.get("speciesCode"), {})
        bird["scientificName"] = tax.get("scientificName", "")
        bird["family"] = tax.get("family", "")
        bird["order"] = tax.get("order", "")
        del bird["speciesCode"]  # not needed in the final output

    return birds


def main():
    email = os.environ.get("EBIRD_EMAIL")
    password = os.environ.get("EBIRD_PASSWORD")
    api_key = os.environ.get("EBIRD_API_KEY")

    if not email or not password:
        print("Missing EBIRD_EMAIL / EBIRD_PASSWORD environment variables.", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print("Missing EBIRD_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page, email, password)
        birds = scrape_via_dom(page)

        browser.close()

    if not birds:
        print("No birds scraped — eBird's page structure may have changed.", file=sys.stderr)
        sys.exit(1)

    birds = enrich_with_taxonomy(birds, api_key)
    birds = enrich_with_location_names(birds, api_key)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(birds, f, indent=2)

    print(f"Wrote {len(birds)} species to {OUTPUT_PATH} at {datetime.utcnow().isoformat()}")


if __name__ == "__main__":
    main()
