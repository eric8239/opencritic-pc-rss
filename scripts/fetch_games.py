import html
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

API_URL = "https://opencritic-api.p.rapidapi.com/game"
API_HOST = "opencritic-api.p.rapidapi.com"
IMG_BASE_URL = "https://img.opencritic.com/"

# --- Configuration ---
PLATFORMS = "pc"
SCORE_THRESHOLD = 80         # Matches OpenCritic's rounded score badge
MIN_REVIEWS = 0              # Raise (e.g., 10 or 15) to filter low-review-count games
LOOKBACK_DAYS = 90           # Upper date bound if release volume slows down
PAGE_SIZE = 20               # OpenCritic returns 20 games per page
MAX_PAGES = 3                # 3 pages/day = max 93 req/mo (use 2 for 62 req/mo)
MAX_FEED_ITEMS = 50          # Rolling RSS window (reader dedupes by GUID)

STATE_PATH = Path("state/seen_ids.json")
FEED_PATH = Path("docs/feed.xml")


def fetch_page(api_key: str, skip: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "platforms": PLATFORMS,
        "sort": "date",
        "order": "desc",
        "skip": skip,
    })
    req = urllib.request.Request(
        f"{API_URL}?{params}",
        headers={
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": API_HOST,
            "User-Agent": "opencritic-rss-bot/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_image_url(game: dict) -> str:
    images = game.get("images") or {}
    # Prefer wide banner art; fall back to box art if banner is missing
    for kind in ("banner", "box"):
        variant = images.get(kind) or {}
        for size in ("og", "sm"):
            path = variant.get(size)
            if path and "undefined" not in path:
                return f"{IMG_BASE_URL}{path.lstrip('/')}"
    return ""


def fetch_qualifying_games(api_key: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    matches = []

    for page in range(MAX_PAGES):
        batch = fetch_page(api_key, skip=page * PAGE_SIZE)
        if not batch:
            break

        reached_cutoff = False
        for game in batch:
            release_dt = parse_iso_date(game.get("firstReleaseDate"))
            if not release_dt or release_dt > now:
                continue

            if release_dt < cutoff:
                reached_cutoff = True
                break

            raw_score = game.get("topCriticScore")
            num_reviews = game.get("numReviews") or 0

            if raw_score is None or raw_score < 0:
                continue
            if round(float(raw_score)) < SCORE_THRESHOLD:
                continue
            if num_reviews < MIN_REVIEWS:
                continue

            matches.append(game)

        if reached_cutoff:
            break

        if page < MAX_PAGES - 1:
            time.sleep(0.35)

    return matches


def build_rss(state: dict[str, dict]) -> str:
    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            "xmlns:media": "http://search.yahoo.com/mrss/",
        },
    )
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = f"OpenCritic PC Releases ({SCORE_THRESHOLD}+)"
    ET.SubElement(channel, "link").text = "https://opencritic.com/browse/pc/last90"
    ET.SubElement(channel, "description").text = (
        f"Recent PC releases scoring {SCORE_THRESHOLD}+ on OpenCritic."
    )
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))

    items = sorted(
        state.values(),
        key=lambda x: x["discovered_at"],
        reverse=True,
    )[:MAX_FEED_ITEMS]

    for g in items:
        item = ET.SubElement(channel, "item")
        display_score = round(g["score"])
        tier = f" ({g['tier']})" if g.get("tier") else ""
        rec = (
            f" | {round(g['percent_recommended'])}% recommend"
            if g.get("percent_recommended") is not None and g["percent_recommended"] >= 0
            else ""
        )
        reviews = f" | {g['num_reviews']} reviews" if g.get("num_reviews") else ""

        ET.SubElement(item, "title").text = f"[{display_score}] {g['name']}"
        ET.SubElement(item, "link").text = g["url"]

        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = f"opencritic-{g['id']}"

        pub_dt = datetime.fromisoformat(g["discovered_at"])
        ET.SubElement(item, "pubDate").text = format_datetime(pub_dt)

        summary_text = html.escape(
            f"Top Critic Score: {display_score}{tier}{rec}{reviews}. "
            f"Release Date: {g['release_date']}."
        )

        image_url = g.get("image", "")
        if image_url:
            safe_img = html.escape(image_url, quote=True)
            safe_alt = html.escape(g["name"], quote=True)
            description_html = (
                f'<p><img src="{safe_img}" alt="{safe_alt}" /></p>'
                f"<p>{summary_text}</p>"
            )
            ET.SubElement(
                item,
                "media:content",
                {"url": image_url, "medium": "image"},
            )
        else:
            description_html = f"<p>{summary_text}</p>"

        ET.SubElement(item, "description").text = description_html

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode"
    )


def main() -> None:
    api_key = os.environ.get("RAPIDAPI_KEY")
    if not api_key:
        sys.exit("Error: RAPIDAPI_KEY environment variable is not set.")

    state: dict[str, dict] = {}
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    qualifying = fetch_qualifying_games(api_key)
    now_iso = datetime.now(timezone.utc).isoformat()
    new_count = 0

    for g in qualifying:
        gid = str(g["id"])
        if gid in state:
            continue

        release_dt = parse_iso_date(g.get("firstReleaseDate"))
        state[gid] = {
            "id": gid,
            "name": g.get("name", f"Game {gid}"),
            "score": round(float(g["topCriticScore"]), 1),
            "percent_recommended": g.get("percentRecommended"),
            "num_reviews": g.get("numReviews", 0),
            "tier": g.get("tier", ""),
            "release_date": release_dt.strftime("%Y-%m-%d") if release_dt else "Unknown",
            "url": g.get("url") or f"https://opencritic.com/game/{gid}",
            "image": extract_image_url(g),
            "discovered_at": now_iso,
        }
        new_count += 1

    print(f"Scanned qualifying games: {len(qualifying)} | Net-new: {new_count}")

    if new_count == 0 and FEED_PATH.exists():
        print("No new games; skipping file writes.")
        return

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(build_rss(state), encoding="utf-8")


if __name__ == "__main__":
    main()
