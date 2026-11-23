import argparse
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

# --- Configuration ---
PLATFORMS = "pc"
SCORE_THRESHOLD = 80         # Minimum Top Critic Score (matched against OpenCritic's rounded score)
MIN_REVIEWS = 0              # Set e.g. 10 or 15 to filter out lightly reviewed titles
LOOKBACK_DAYS = 90           # Rolling release window
PAGE_SIZE = 20               # OpenCritic returns 20 games per page
MAX_PAGES = 20               # ~10 pages covers 90 days; 20 gives comfortable headroom
MAX_FEED_ITEMS = 50          # Rolling RSS window (deduped by GUID)

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
            if not release_dt:
                continue

            # Skip upcoming/unreleased games (e.g., next few days)
            if release_dt > now:
                continue

            if release_dt < cutoff:
                reached_cutoff = True
                break

            raw_score = game.get("topCriticScore")
            num_reviews = game.get("numReviews") or 0

            # OpenCritic returns -1 when there aren't enough reviews for a score
            if raw_score is None or raw_score < 0:
                continue
            if round(float(raw_score)) < SCORE_THRESHOLD:
                continue
            if num_reviews < MIN_REVIEWS:
                continue

            matches.append(game)

        if reached_cutoff:
            break

        # Stay well below RapidAPI's 4 req/sec limit
        time.sleep(0.35)

    return matches


def build_rss(state: dict[str, dict]) -> str:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = f"OpenCritic PC Releases ({SCORE_THRESHOLD}+)"
    ET.SubElement(channel, "link").text = "https://opencritic.com/browse/pc/last90"
    ET.SubElement(channel, "description").text = (
        f"PC releases from the last {LOOKBACK_DAYS} days scoring "
        f"{SCORE_THRESHOLD}+ on OpenCritic."
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

        ET.SubElement(item, "description").text = (
            f"Top Critic Score: {display_score}{tier}{rec}{reviews}. "
            f"Release Date: {g['release_date']}."
        )

    ET.indent(rss, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        rss, encoding="unicode"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Record current qualifying games in state without writing them to the RSS feed.",
    )
    args = parser.parse_args()

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

        name = g.get("name", f"Game {gid}")
        release_dt = parse_iso_date(g.get("firstReleaseDate"))
        state[gid] = {
            "id": gid,
            "name": name,
            "score": round(float(g["topCriticScore"]), 1),
            "percent_recommended": g.get("percentRecommended"),
            "num_reviews": g.get("numReviews", 0),
            "tier": g.get("tier", ""),
            "release_date": release_dt.strftime("%Y-%m-%d") if release_dt else "Unknown",
            "url": g.get("url") or f"https://opencritic.com/game/{gid}",
            "discovered_at": now_iso,
            "seeded": args.seed,
        }
        new_count += 1

    print(f"Scanned qualifying games: {len(qualifying)} | Net-new: {new_count}")

    if new_count == 0 and FEED_PATH.exists():
        print("No new games; skipping file writes.")
        return

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    feed_items = {k: v for k, v in state.items() if not v.get("seeded")}
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(build_rss(feed_items), encoding="utf-8")


if __name__ == "__main__":
    main()
