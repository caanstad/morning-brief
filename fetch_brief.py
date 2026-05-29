"""
Morgenbrief — fetches news (RSS) and weather (met.no) and writes index.html.

Designed to fail gracefully: if any source is unreachable or malformed,
that section is marked as unavailable and the rest of the page still renders.
"""

from __future__ import annotations

import html
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests

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

# Coordinates for the three locations (met.no needs lat/lon).
LOCATIONS = [
    {"name": "Bønesberget, Bergen",  "lat": 60.3500, "lon": 5.2860},
    {"name": "Holu gård, 3570 Ål",   "lat": 60.6360, "lon": 8.5590},
    {"name": "Hamnavika, Tysnes",    "lat": 60.0150, "lon": 5.5800},
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
    date: str            # ISO date, YYYY-MM-DD
    day_label: str       # e.g. "Tor 29.05"
    rain_mm: float       # total over 24h
    temp_day: Optional[float]    # median of 06-18 local
    temp_night: Optional[float]  # median of 18-06 local


@dataclass
class WeatherSection:
    name: str
    today_hourly: list[dict] = field(default_factory=list)   # next ~12 hours
    forecast: list[DaySummary] = field(default_factory=list)  # next 7 days
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

        # Try RSS 2.0 first (<channel><item>), then Atom (<entry>).
        items = root.findall(".//item")
        if not items:
            # Atom namespace handling
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
            # Atom <link> uses href attribute
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
    """Strip HTML tags from RSS descriptions and collapse whitespace."""
    if not text:
        return ""
    # Very light HTML stripping (RSS descriptions sometimes contain markup).
    import re
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

        # ---- Today's hourly strip: next 12 hours ----
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

        # ---- Aggregate next 7 days ----
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

            # Rain: prefer 1h values; fall back to 6h (every 6h) if 1h not available.
            rain_total = sum(e["rain_1h"] for e in entries if e["rain_1h"] is not None)
            if rain_total == 0:
                # 6h blocks: take entries at 0, 6, 12, 18 (whichever exist)
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
        # Today strip
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

        # 7-day table
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
        body = today_strip + forecast_table

    return (
        f'<section class="weather-card">'
        f'<h3>{html.escape(section.name)}</h3>'
        f'{body}'
        f'</section>'
    )


def render_page(news: list[NewsSection], weather: list[WeatherSection]) -> str:
    now = datetime.now(OSLO)
    date_str = now.strftime("%A %d. %B %Y").capitalize()
    time_str = now.strftime("%H:%M")

    news_html = "".join(render_news_card(s) for s in news)
    weather_html = "".join(render_weather_card(w) for w in weather)

    template = Path(__file__).parent / "template.html"
    html_template = template.read_text(encoding="utf-8")

    return (html_template
            .replace("{{DATE}}", html.escape(date_str))
            .replace("{{UPDATED}}", html.escape(time_str))
            .replace("{{NEWS}}", news_html)
            .replace("{{WEATHER}}", weather_html))


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

    print("Rendering HTML…")
    output_html = render_page(news_sections, weather_sections)

    output_path = Path(__file__).parent / "index.html"
    output_path.write_text(output_html, encoding="utf-8")
    print(f"Wrote {output_path} ({len(output_html)} bytes).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
