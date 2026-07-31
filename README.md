"""
Scrapes the logged-in user's eBird life list and writes it to data/lifelist.json
in the format the gallery site expects.

REQUIRES FINISHING BEFORE FIRST RUN — see the two TODOs below.
eBird's page structure isn't something I can verify without a live login,
so the selectors here are best-guess placeholders. To finish this:

  1. Open https://ebird.org/lifelist in a normal browser while logged in.
  2. Open DevTools -> Network tab, refresh the page.
  3. Look for a request returning JSON (often has "lifelist" or similar in
     the URL). If one exists, it's far more reliable to call that endpoint
     directly than to parse HTML — replace scrape_via_dom() with a direct
     fetch of that endpoint instead.
  4. If no JSON endpoint exists, open DevTools -> Elements, find the table
     row for a single species, and update the CSS selectors marked TODO
     below to match the real class names.
"""

import os
import sys
import json
from datetime import datetime
from playwright.sync_api import sync_playwright

EBIRD_LOGIN_URL = "https://secure.birds.cornell.edu/cassso/login?service=https%3A%2F%2Febird.org%2Flogin%2Fcas%3Fportal%3Debird"
EBIRD_LIFELIST_URL = "https://ebird.org/lifelist"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "lifelist.json")


def login(page, email, password):
    page.goto(EBIRD_LOGIN_URL)
    # TODO: verify these field selectors match the real Cornell login form
    page.fill("#username", email)
    page.fill("#password", password)
    page.click("button[type='submit']")
    page.wait_for_url("https://ebird.org/**", timeout=20000)


def scrape_via_dom(page):
    page.goto(EBIRD_LIFELIST_URL)
    # TODO: replace with the real row/column selectors from DevTools inspection
    page.wait_for_selector(".Table tbody tr", timeout=20000)
    rows = page.query_selector_all(".Table tbody tr")

    birds = []
    for row in rows:
        common_name_el = row.query_selector(".Table-species-name")
        latin_name_el = row.query_selector(".Table-species-sci")
        date_el = row.query_selector(".Table-date")
        location_el = row.query_selector(".Table-location")
        count_el = row.query_selector(".Table-count")

        if not common_name_el:
            continue

        birds.append({
            "commonName": common_name_el.inner_text().strip(),
            "scientificName": latin_name_el.inner_text().strip() if latin_name_el else "",
            "family": "",   # not shown on life list page; filled in by enrich_with_taxonomy()
            "order": "",
            "dateFirstHeard": date_el.inner_text().strip() if date_el else "",
            "location": location_el.inner_text().strip() if location_el else "",
            "count": int(count_el.inner_text().strip()) if count_el and count_el.inner_text().strip().isdigit() else 1,
            "idMethod": "eBird checklist",
        })
    return birds


def main():
    email = os.environ.get("EBIRD_EMAIL")
    password = os.environ.get("EBIRD_PASSWORD")

    if not email or not password:
        print("Missing EBIRD_EMAIL / EBIRD_PASSWORD environment variables.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        login(page, email, password)
        birds = scrape_via_dom(page)

        browser.close()

    if not birds:
        print("No birds scraped — selectors likely need updating. See TODOs at top of file.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_PATH, "w") as f:
        json.dump(birds, f, indent=2)

    print(f"Wrote {len(birds)} species to {OUTPUT_PATH} at {datetime.utcnow().isoformat()}")


if __name__ == "__main__":
    main()
