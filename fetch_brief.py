"""
Morgenbrief — fetches news (RSS), weather (met.no), and car listings (FINN.no)
and writes index.html.

Designed to fail gracefully: if any source is unreachable or malformed,
that section is marked as unavailable and the rest of the page still renders.
"""

from __future__ import annotations

import html
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------- Configuration ----------

USER_AGENT = "morning-brief/1.0 (github.com/caanstad/morning-brief)"
OSLO = ZoneInfo("Europe/Oslo")
MAX_ARTICLES_PER_FEED = 5
HTTP_TIMEOUT = 20  # seconds

NEWS_SOURCES = [
    {"name": "VG",      "url": "https://www.vg.no/rss/feed/"},
    {"name": "E24",     "url": "https://e24.no/rss"},
    {"name": "DN",      "url": "https://services.dn.no/api/feed/rss/"},
    {"name": "kode24",  "url": "https://rss.kode24.no/"},
]

LOCATIONS = [
    {"name": "Bønesberget, Bergen",  "lat": 60.3500, "lon": 5.2860},
    {"name": "Holu gård, 3570 Ål",   "lat": 60.6360, "lon": 8.5590},
    {"name": "Hamnavika, Tysnes",    "lat": 60.0150, "lon": 5.5800},
]

# FINN car searches. Each entry shares filters but uses a different sort order.
FINN_SEARCHES = [
    {
        "name": "🆕 Nyeste",
        "url": "https://www.finn.no/mobility/search/car?number_of_seats_from=7&registration_class=1&sort=PUBLISHED_DESC&variant=1.8078.2000555&wheel_drive=2",
        "max_items": 3,
    },
    {
        "name": "💰 Billigste",
        "url": "https://www.finn.no/mobility/search/car?number_of_seats_from=7&registration_class=1&sort=PRICE_ASC&variant=1.8078.2000555&wheel_drive=2",
        "max_items": 3,
    },
]


# ---------- Data structures ----------

@dataclass
class Article:
    title: str
    description: str
    link: str


@dataclass
class NewsSection:
    name: str
    articles: list[Article] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class DaySummary:
    date: str
    day_label: str
    rain_mm: float
    temp_day: Optional[float]
    temp_night: Optional[float]


@dataclass
class WeatherSection:
    name: str
    today_hourly: list[dict] = field(default_factory=list)
    forecast: list[DaySummary] = field(default_factory=list)
    forecast_avg_rain: Optional[float] = None
    error: Optional[str] = None


@dataclass
class CarListing:
    title: str
    details: str   # e.g. "2025 · 36 000 km"
    price: str     # e.g. "479 000 kr" or "Solgt"
    location: str  # e.g. "Nesttun"
    link: str


@dataclass
class CarSection:
    name: str
    search_url: str
    listings: list[CarListing] = field(default_factory=list)
    error: Optional[str] = None


# ---------- News fetching ----------

def fetch_news_feed(source: dict) -> NewsSection:
    section = NewsSection(name=source["name"])
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)

        items = root.findall(".//item")
        if not items:
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//atom:entry", ns)

        for item in items[:MAX_ARTICLES_PER_FEED]:
            title = _first_text(item, ["title", "{http://www.w3.org/2005/Atom}title"])
            desc = _first_text(item, [
                "description",
                "{http://www.w3.org/2005/Atom}summary",
                "{http://www.w3.org/2005/Atom}content",
            ])
            link = _first_text(item, ["link", "{http://www.w3.org/2005/Atom}link"])
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "")

            section.articles.append(Article(
                title=_clean_text(title),
                description=_clean_text(desc),
                link=(link or "").strip(),
            ))

        if not section.articles:
            section.error = "Ingen artikler funnet i RSS-strømmen."

    except requests.HTTPError as e:
        section.error = f"HTTP-feil: {e.response.status_code}"
    except requests.RequestException as e:
        section.error = f"Nettverksfeil: {type(e).__name__}"
    except ET.ParseError as e:
        section.error = f"Klarte ikke å lese RSS-XML: {e}"
    except Exception as e:
        section.error = f"Ukjent feil: {type(e).__name__}: {e}"

    return section


def _first_text(element: ET.Element, tag_candidates: list[str]) -> str:
    for tag in tag_candidates:
        found = element.find(tag)
        if found is not None and found.text:
            return found.text
    return ""


def _clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------- Weather fetching ----------

def fetch_weather(location: dict) -> WeatherSection:
    section = WeatherSection(name=location["name"])
    try:
        url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
        resp = requests.get(
            url,
            params={"lat": location["lat"], "lon": location["lon"]},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        timeseries = data["properties"]["timeseries"]
        now = datetime.now(OSLO)

        for entry in timeseries[:12]:
            t = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(OSLO)
            instant = entry["data"]["instant"]["details"]
            next_1h = entry["data"].get("next_1_hours", {}).get("details", {})
            symbol = entry["data"].get("next_1_hours", {}).get("summary", {}).get("symbol_code", "")
            section.today_hourly.append({
                "time": t.strftime("%H:%M"),
                "temp": instant.get("air_temperature"),
                "rain": next_1h.get("precipitation_amount", 0.0),
                "symbol": symbol,
            })

        by_date: dict[str, list[dict]] = {}
        for entry in timeseries:
            t = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(OSLO)
            date_key = t.date().isoformat()
            by_date.setdefault(date_key, []).append({
                "datetime": t,
                "temp": entry["data"]["instant"]["details"].get("air_temperature"),
                "rain_1h": entry["data"].get("next_1_hours", {}).get("details", {}).get("precipitation_amount"),
                "rain_6h": entry["data"].get("next_6_hours", {}).get("details", {}).get("precipitation_amount"),
            })

        today_date = now.date()
        for i in range(7):
            date = today_date + timedelta(days=i)
            key = date.isoformat()
            if key not in by_date:
                continue
            entries = by_date[key]

            rain_total = sum(e["rain_1h"] for e in entries if e["rain_1h"] is not None)
            if rain_total == 0:
                seen_blocks = set()
                fallback = 0.0
                for e in entries:
                    h = e["datetime"].hour
                    block = h // 6
                    if e["rain_6h"] is not None and block not in seen_blocks:
                        fallback += e["rain_6h"]
                        seen_blocks.add(block)
                rain_total = fallback

            day_temps = [e["temp"] for e in entries
                         if e["temp"] is not None and 6 <= e["datetime"].hour < 18]
            night_temps = [e["temp"] for e in entries
                           if e["temp"] is not None and (e["datetime"].hour >= 18 or e["datetime"].hour < 6)]

            section.forecast.append(DaySummary(
                date=key,
                day_label=date.strftime("%a %d.%m"),
                rain_mm=round(rain_total, 1),
                temp_day=round(statistics.median(day_temps), 1) if day_temps else None,
                temp_night=round(statistics.median(night_temps), 1) if night_temps else None,
            ))

        if section.forecast:
            section.forecast_avg_rain = round(
                sum(d.rain_mm for d in section.forecast) / len(section.forecast), 1
            )

        if not section.forecast:
            section.error = "Ingen prognosedata mottatt."

    except requests.HTTPError as e:
        section.error = f"HTTP-feil fra met.no: {e.response.status_code}"
    except requests.RequestException as e:
        section.error = f"Nettverksfeil mot met.no: {type(e).__name__}"
    except (KeyError, ValueError) as e:
        section.error = f"Uventet svar fra met.no: {type(e).__name__}"
    except Exception as e:
        section.error = f"Ukjent feil: {type(e).__name__}: {e}"

    return section


# ---------- FINN car listing fetching ----------

def fetch_finn_section(search: dict) -> CarSection:
    section = CarSection(name=search["name"], search_url=search["url"])
    try:
        resp = requests.get(
            search["url"],
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "nb-NO,nb;q=0.9,no;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # FINN search results: find every unique link to /mobility/item/<id>,
        # then walk up to a containing element and extract data from its text.
        seen_ids: set[str] = set()
        item_links = soup.find_all("a", href=re.compile(r"/mobility/item/\d+"))

        for link_el in item_links:
            if len(section.listings) >= search["max_items"]:
                break

            href = link_el.get("href", "")
            m = re.search(r"/mobility/item/(\d+)", href)
            if not m:
                continue
            item_id = m.group(1)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)

            # Walk up to the article-like container holding this listing's data.
            container = link_el.find_parent("article")
            if container is None:
                container = link_el.find_parent(
                    lambda tag: tag.name in ("section", "div") and tag.find(["h2", "h3"])
                )
            if container is None:
                container = link_el.parent

            text = container.get_text(separator="\n", strip=True) if container else ""

            # Title (typically inside an <h2> or <h3>)
            title_el = container.find(["h2", "h3"]) if container else None
            title = title_el.get_text(strip=True) if title_el else "Bil"

            # Price — look for "XXX XXX kr" or "Solgt"
            price = ""
            price_match = re.search(r"(\d[\d\s]{2,})\s*kr\b", text)
            if price_match:
                price = re.sub(r"\s+", " ", price_match.group(0)).strip()
            elif "Solgt" in text:
                price = "Solgt"

            # Year + km — pattern like "2025 ∙ 36 000 km"
            details = ""
            details_match = re.search(
                r"((?:19|20)\d{2})\s*[∙·•|]\s*([\d\s]{1,10}km)",
                text,
            )
            if details_match:
                year = details_match.group(1)
                km = re.sub(r"\s+", " ", details_match.group(2)).strip()
                details = f"{year} · {km}"

            # Location — first part of the line containing "Forhandler"/"Privat"/"Smidig"
            location = ""
            for line in text.split("\n"):
                line = line.strip()
                if any(marker in line for marker in ("Forhandler", "Privat", "Smidig")):
                    parts = re.split(r"[∙·•]", line)
                    if parts:
                        location = parts[0].strip()
                        break

            # Build full link
            if href.startswith("/"):
                href = "https://www.finn.no" + href

            section.listings.append(CarListing(
                title=title,
                details=details,
                price=price,
                location=location,
                link=href,
            ))

        if not section.listings:
            section.error = "Ingen treff funnet (kan være endret HTML-struktur eller blokkering)."

    except requests.HTTPError as e:
        section.error = f"HTTP-feil fra FINN: {e.response.status_code}"
    except requests.RequestException as e:
        section.error = f"Nettverksfeil mot FINN: {type(e).__name__}"
    except Exception as e:
        section.error = f"Ukjent feil: {type(e).__name__}: {e}"

    return section


# ---------- Rendering ----------

def render_news_card(section: NewsSection) -> str:
    if section.error:
        body = f'<p class="error">⚠️ Kunne ikke hente: {html.escape(section.error)}</p>'
    elif not section.articles:
        body = '<p class="error">⚠️ Ingen artikler tilgjengelig.</p>'
    else:
        items = []
        for art in section.articles:
            title_html = html.escape(art.title) if art.title else "(uten tittel)"
            desc_html = html.escape(art.description) if art.description else ""
            if art.link:
                title_html = f'<a href="{html.escape(art.link)}" target="_blank" rel="noopener">{title_html}</a>'
            items.append(
                f'<li><div class="art-title">{title_html}</div>'
                f'<div class="art-desc">{desc_html}</div></li>'
            )
        body = f'<ul class="articles">{"".join(items)}</ul>'

    return (
        f'<section class="news-card">'
        f'<h3>{html.escape(section.name)}</h3>'
        f'{body}'
        f'</section>'
    )


def render_weather_card(section: WeatherSection) -> str:
    if section.error:
        body = f'<p class="error">⚠️ Kunne ikke hente vær: {html.escape(section.error)}</p>'
    else:
        hourly_cells = []
        for h in section.today_hourly:
            temp = f"{h['temp']:.0f}°" if h['temp'] is not None else "–"
            rain = h['rain'] or 0
            rain_str = f"{rain:.1f}mm" if rain > 0 else ""
            hourly_cells.append(
                f'<div class="hour"><div class="hour-time">{html.escape(h["time"])}</div>'
                f'<div class="hour-temp">{temp}</div>'
                f'<div class="hour-rain">{rain_str}</div></div>'
            )
        today_strip = f'<div class="today-strip">{"".join(hourly_cells)}</div>'

        rows = []
        for d in section.forecast:
            temp_day = f"{d.temp_day:.1f}°" if d.temp_day is not None else "–"
            temp_night = f"{d.temp_night:.1f}°" if d.temp_night is not None else "–"
            rain = f"{d.rain_mm:.1f}" if d.rain_mm > 0 else "0"
            rows.append(
                f'<tr><td>{html.escape(d.day_label)}</td>'
                f'<td>{rain} mm</td>'
                f'<td>{temp_day}</td>'
                f'<td>{temp_night}</td></tr>'
            )
        forecast_table = (
            '<table class="forecast">'
            '<thead><tr><th>Dag</th><th>Regn</th><th>Dag</th><th>Natt</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
        if section.forecast_avg_rain is not None:
            avg_html = (
                f'<div class="forecast-avg">Gjennomsnitt nedbør neste '
                f'{len(section.forecast)} dager: '
                f'<strong>{section.forecast_avg_rain:.1f} mm/dag</strong></div>'
            )
        else:
            avg_html = ''
        body = today_strip + forecast_table + avg_html

    return (
        f'<section class="weather-card">'
        f'<h3>{html.escape(section.name)}</h3>'
        f'{body}'
        f'</section>'
    )


def render_car_card(section: CarSection) -> str:
    if section.error:
        body = f'<p class="error">⚠️ Kunne ikke hente: {html.escape(section.error)}</p>'
    elif not section.listings:
        body = '<p class="error">⚠️ Ingen treff.</p>'
    else:
        items = []
        for car in section.listings:
            title_html = html.escape(car.title)
            if car.link:
                title_html = f'<a href="{html.escape(car.link)}" target="_blank" rel="noopener">{title_html}</a>'

            meta_parts = []
            if car.details:
                meta_parts.append(html.escape(car.details))
            if car.price:
                meta_parts.append(f'<strong>{html.escape(car.price)}</strong>')
            if car.location:
                meta_parts.append(html.escape(car.location))
            meta_html = " · ".join(meta_parts)

            items.append(
                f'<li><div class="car-title">{title_html}</div>'
                f'<div class="car-meta">{meta_html}</div></li>'
            )
        body = f'<ul class="cars">{"".join(items)}</ul>'

    search_link = (
        f'<a class="see-all" href="{html.escape(section.search_url)}" '
        f'target="_blank" rel="noopener">Se alle treff på FINN →</a>'
    )

    return (
        f'<section class="car-card">'
        f'<h3>{html.escape(section.name)}</h3>'
        f'{body}'
        f'{search_link}'
        f'</section>'
    )


def render_page(news: list[NewsSection], weather: list[WeatherSection],
                cars: list[CarSection]) -> str:
    now = datetime.now(OSLO)
    date_str = now.strftime("%A %d. %B %Y").capitalize()
    time_str = now.strftime("%H:%M")

    news_html = "".join(render_news_card(s) for s in news)
    weather_html = "".join(render_weather_card(w) for w in weather)
    cars_html = "".join(render_car_card(c) for c in cars)

    template = Path(__file__).parent / "template.html"
    html_template = template.read_text(encoding="utf-8")

    return (html_template
            .replace("{{DATE}}", html.escape(date_str))
            .replace("{{UPDATED}}", html.escape(time_str))
            .replace("{{NEWS}}", news_html)
            .replace("{{WEATHER}}", weather_html)
            .replace("{{CARS}}", cars_html))


# ---------- Main ----------

def main() -> int:
    print(f"[{datetime.now(OSLO).isoformat()}] Starting morning brief generation…")

    print("Fetching news feeds…")
    news_sections = [fetch_news_feed(src) for src in NEWS_SOURCES]
    for s in news_sections:
        status = f"{len(s.articles)} articles" if not s.error else f"ERROR: {s.error}"
        print(f"  {s.name}: {status}")

    print("Fetching weather…")
    weather_sections = [fetch_weather(loc) for loc in LOCATIONS]
    for w in weather_sections:
        status = f"{len(w.forecast)} days" if not w.error else f"ERROR: {w.error}"
        print(f"  {w.name}: {status}")

    print("Fetching car listings…")
    car_sections = [fetch_finn_section(s) for s in FINN_SEARCHES]
    for c in car_sections:
        status = f"{len(c.listings)} listings" if not c.error else f"ERROR: {c.error}"
        print(f"  {c.name}: {status}")

    print("Rendering HTML…")
    output_html = render_page(news_sections, weather_sections, car_sections)

    output_path = Path(__file__).parent / "index.html"
    output_path.write_text(output_html, encoding="utf-8")
    print(f"Wrote {output_path} ({len(output_html)} bytes).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
