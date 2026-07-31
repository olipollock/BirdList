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
