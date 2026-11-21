"""Fetch Bellhaven locations. Install dependencies: pip install requests beautifulsoup4"""

import json
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://analyst-assessment-production.up.railway.app"


def get_page(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.content, "html.parser")


def scrape_location(session, url):
    page = get_page(session, url)
    heading = page.select_one(".wrap h1")
    details = {
        label.get_text(strip=True): label.find_next_sibling("dd")
        for label in page.select("dl.detail dt")
    }
    if heading is None or not details.get("Address") or not details.get("Care Offerings"):
        raise ValueError(f"Missing location details: {url}")

    address_lines = list(details["Address"].stripped_strings)
    if len(address_lines) < 2:
        raise ValueError(f"Incomplete address: {url}")

    city_state_zip = re.fullmatch(
        r"(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", address_lines[-1]
    )
    if city_state_zip is None:
        raise ValueError(f"Unrecognized address format: {url}")

    care_offerings = [
        badge.get_text(" ", strip=True)
        for badge in details["Care Offerings"].select(".badge")
    ]
    if not care_offerings:
        raise ValueError(f"Missing care offerings: {url}")

    city, state, zip_code = city_state_zip.groups()
    return {
        "name": heading.get_text(" ", strip=True),
        "address": " ".join(address_lines[:-1]),
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": care_offerings,
        "source_url": url,
    }


def scrape_locations():
    # The homepage links to a new location missing from the directory.
    pending_pages = {BASE_URL + "/", BASE_URL + "/communities"}
    visited_pages = set()
    location_urls = set()

    with requests.Session() as session:
        while pending_pages:
            url = pending_pages.pop()
            visited_pages.add(url)
            page = get_page(session, url)

            location_urls.update(
                urljoin(url, link["href"])
                for link in page.select("a[href^='/communities/']")
            )
            pending_pages.update(
                {
                    urljoin(url, link["href"])
                    for link in page.select(".pager a[href]")
                }
                - visited_pages
            )

        if not location_urls:
            raise ValueError("No Bellhaven locations found")

        return [scrape_location(session, url) for url in sorted(location_urls)]


if __name__ == "__main__":
    print(json.dumps(scrape_locations(), ensure_ascii=False, indent=2))
