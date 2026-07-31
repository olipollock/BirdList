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


def scrape_via_dom(page):
    page.goto(EBIRD_LIFELIST_URL)
    page.wait_for_selector("li.Observation", timeout=20000)
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

        birds.append({
            "commonName": name_el.inner_text().strip(),
            "speciesCode": code_el.get_attribute("data-species-code"),
            "dateFirstHeard": date_iso,
            "regionCode": location_els[1].inner_text().strip() if len(location_els) > 1 else "",
        })

    return birds


def enrich_with_region_names(birds, api_key):
    """Converts eBird region codes (e.g. 'GB-ENG') into readable names
    (e.g. 'England, United Kingdom') via eBird's public region reference API —
    sanctioned, key-based, general geography data rather than personal data."""
    codes = sorted(set(b["regionCode"] for b in birds if b.get("regionCode")))
    name_map = {}

    for code in codes:
        try:
            resp = requests.get(
                f"https://api.ebird.org/v2/ref/region/info/{code}",
                headers={"X-eBirdApiToken": api_key},
                timeout=15,
            )
            resp.raise_for_status()
            name_map[code] = resp.json().get("result", code)
        except requests.RequestException:
            name_map[code] = code  # fall back to the raw code if lookup fails
        time.sleep(0.3)

    for bird in birds:
        bird["location"] = name_map.get(bird.get("regionCode"), "")
        del bird["regionCode"]

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
    birds = enrich_with_region_names(birds, api_key)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(birds, f, indent=2)

    print(f"Wrote {len(birds)} species to {OUTPUT_PATH} at {datetime.utcnow().isoformat()}")


if __name__ == "__main__":
    main()
