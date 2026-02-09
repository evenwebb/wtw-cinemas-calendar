"""WTW Cinemas Calendar Scraper.

Scrapes upcoming film releases from WTW Cinemas and generates an iCalendar file.
"""
import datetime
import json
import logging
import os
import re
import time
import warnings
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Suppress urllib3/OpenSSL noise when system SSL is older (e.g. some CI or macOS)
warnings.filterwarnings("ignore", message=".*OpenSSL.*", category=UserWarning)

import requests
from bs4 import BeautifulSoup

# ============================================================================
# CONSTANTS
# ============================================================================
# HTTP Request Settings
HTTP_TIMEOUT = 60
HTTP_RETRIES = 3
HTTP_RETRY_DELAY = 1
HTTP_RETRY_MULTIPLIER = 2
USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/119.0.0.0 Safari/537.36'
)

# iCalendar Settings
ICAL_LINE_LENGTH = 75
# Output for GitHub Pages: one .ics per cinema + index.html in docs/
OUTPUT_DIR = "docs"
# Release history for index "History" stats (past 30 days, YTD). One small JSON file, kept for 2 years.
RELEASE_HISTORY_PATH = ".release_history.json"
RELEASE_HISTORY_MAX_DAYS = 730

# Synopsis Extraction Settings
MIN_SYNOPSIS_LENGTH = 50
MAX_SYNOPSIS_LENGTH = 500
SYNOPSIS_SKIP_TERMS = ['cookie', 'privacy', 'terms', 'wheelchair', 'audio description']

# Logging
LOG_FILE = "cinema_log.txt"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
error_handler = logging.FileHandler(LOG_FILE)
error_handler.setLevel(logging.ERROR)
logger.addHandler(error_handler)

# ============================================================================
# CONFIGURATION: Choose which cinemas to scrape
# ============================================================================
# Set to True for the cinemas you want to include in the iCal file
CINEMAS = {
    'st-austell': {
        'enabled': True,
        'name': 'St Austell',
        'url': 'https://wtwcinemas.co.uk/st-austell/coming-soon/'
    },
    'newquay': {
        'enabled': True,
        'name': 'Newquay',
        'url': 'https://wtwcinemas.co.uk/newquay/coming-soon/'
    },
    'wadebridge': {
        'enabled': True,
        'name': 'Wadebridge',
        'url': 'https://wtwcinemas.co.uk/wadebridge/coming-soon/'
    },
    'truro': {
        'enabled': True,
        'name': 'Truro',
        'url': 'https://wtwcinemas.co.uk/truro/coming-soon/'
    }
}

# ============================================================================
# NOTIFICATION SETTINGS
# ============================================================================
# Configure calendar notifications/alarms for film releases
# Set 'enabled' to True and specify when you want to be reminded
#
# NOTIFICATION_TIME: Default time of day for all notifications (24-hour format)
# Set this once and all your notifications will use this time
NOTIFICATION_TIME = '09:00'  # 9:00 AM - Change to your preferred time

# Available notification presets (uncomment one or create custom):
#
# Option 1: No notifications (default)
NOTIFICATIONS = {
    'enabled': False,
    'alarms': []
}

# Option 2: Day before (uses NOTIFICATION_TIME)
# NOTIFICATIONS = {
#     'enabled': True,
#     'alarms': [
#         {'days_before': 1, 'description': 'Film releases tomorrow'}
#     ]
# }

# Option 3: Day of event (uses NOTIFICATION_TIME)
# NOTIFICATIONS = {
#     'enabled': True,
#     'alarms': [
#         {'days_before': 0, 'description': 'Films out today!'}
#     ]
# }

# Option 4: Multiple reminders (all use NOTIFICATION_TIME)
# NOTIFICATIONS = {
#     'enabled': True,
#     'alarms': [
#         {'days_before': 7, 'description': 'Film releases in 1 week'},
#         {'days_before': 1, 'description': 'Film releases tomorrow'},
#         {'days_before': 0, 'description': 'Films out today!'}
#     ]
# }

# Option 5: Custom time for specific notification (overrides NOTIFICATION_TIME)
# NOTIFICATIONS = {
#     'enabled': True,
#     'alarms': [
#         {'days_before': 1, 'description': 'Evening reminder', 'time': '18:00'},  # 6pm
#         {'days_before': 0, 'description': 'Morning reminder'}  # Uses NOTIFICATION_TIME
#     ]
# }

# Alarm format:
# - 'days_before': Number of days before the event (0 = day of event, 1 = day before, etc.)
# - 'description': Text description for the notification
# - 'time': (Optional) Specific time for THIS notification only (format: 'HH:MM')
#           If not specified, uses NOTIFICATION_TIME setting above

# ============================================================================
# CACHE SETTINGS
# ============================================================================
# Cache film details to avoid re-scraping unchanged data
CACHE_FILE = '.film_cache.json'
CACHE_EXPIRY_DAYS = 7  # How many days to keep cached film details

# TMDb (optional enrichment when TMDB_API_KEY is set)
TMDB_CACHE_FILE = '.tmdb_cache.json'
TMDB_CACHE_DAYS = 30
TMDB_DELAY_SEC = 0.2

# Date pattern to match "Expected: DD Month YYYY" format
DATE_PATTERN = re.compile(r"Expected:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
# Alternative pattern for "Expected at WTW Cinemas from the DDth Month"
ALT_DATE_PATTERN = re.compile(r"Expected at WTW Cinemas from the (\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)")


def get_base_film_url(url: str) -> str:
    """Extract base film URL without query parameters.

    Args:
        url: Full URL like 'https://wtwcinemas.co.uk/film/tron-ares/?screen=st-austell'

    Returns:
        Base URL like 'https://wtwcinemas.co.uk/film/tron-ares/'
    """
    if '?' in url:
        return url.split('?')[0]
    return url


def load_cache() -> Dict[str, dict]:
    """Load the film details cache from disk.

    Loads cached film details from CACHE_FILE and removes expired entries.
    Expired entries are older than CACHE_EXPIRY_DAYS.

    Returns:
        Dictionary mapping film URLs to cached film details.
        Returns empty dict if cache file doesn't exist or on error.
    """
    if not os.path.exists(CACHE_FILE):
        return {}

    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)

        # Clean expired entries
        cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=CACHE_EXPIRY_DAYS)).isoformat()
        cache = {url: data for url, data in cache.items() if data.get('cached_at', '') > cutoff_date}

        logger.info("Loaded cache with %d entries", len(cache))
        return cache
    except json.JSONDecodeError as e:
        logger.warning("Cache file is corrupted, starting fresh: %s", e)
        return {}
    except (OSError, IOError) as e:
        logger.warning("Failed to read cache file: %s", e)
        return {}
    except Exception as e:
        logger.warning("Unexpected error loading cache: %s", e)
        return {}


def save_cache(cache: Dict[str, dict]) -> None:
    """Save the film details cache to disk.

    Args:
        cache: Dictionary mapping film URLs to film details to be saved
    """
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info("Saved cache with %d entries", len(cache))
    except (OSError, IOError) as e:
        logger.error("Failed to write cache file: %s", e)
    except Exception as e:
        logger.error("Unexpected error saving cache: %s", e)


def load_release_history() -> set:
    """Load persisted (date, title) set for History stats. Returns set of (date, str)."""
    path = Path(RELEASE_HISTORY_PATH)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = set()
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    d = datetime.datetime.strptime(item[0], "%Y-%m-%d").date()
                    out.add((d, item[1]))
                except (ValueError, TypeError):
                    pass
        return out
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Release history load failed: %s", e)
        return set()


def save_release_history(releases: set) -> None:
    """Persist (date, title) set for History stats. Keeps last RELEASE_HISTORY_MAX_DAYS."""
    path = Path(RELEASE_HISTORY_PATH)
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=RELEASE_HISTORY_MAX_DAYS)
    kept = [(d.isoformat(), t) for (d, t) in releases if d >= cutoff]
    try:
        path.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
        logger.info("Saved release history with %d entries", len(kept))
    except OSError as e:
        logger.warning("Release history save failed: %s", e)


def load_tmdb_cache() -> Dict[str, dict]:
    """Load TMDb cache; drop expired entries."""
    if not os.path.exists(TMDB_CACHE_FILE):
        return {}
    try:
        with open(TMDB_CACHE_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=TMDB_CACHE_DAYS)).isoformat()
        return {k: v for k, v in cache.items() if v.get('cached_at', '') > cutoff}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("TMDb cache load failed: %s", e)
        return {}


def save_tmdb_cache(cache: Dict[str, dict]) -> None:
    """Save TMDb cache to disk."""
    try:
        with open(TMDB_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.warning("TMDb cache save failed: %s", e)


def _tmdb_cache_key(film_title: str) -> str:
    """Stable cache key from film title (normalise for search)."""
    t = re.sub(r"\s*\([^)]*\)$", "", film_title).strip()
    t = re.sub(r"[\s\-:]+", " ", t.lower()).strip()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "unknown"


def _normalize_title_for_match(title: str) -> str:
    """Normalize title for TMDb result matching."""
    if not title:
        return ""
    return re.sub(r"[\s\-:]+", " ", title.lower()).strip()


def _pick_best_tmdb_result(results: List[Dict], search_title: str) -> Optional[Dict]:
    """Pick the TMDb result that best matches our search title."""
    if not results or not search_title:
        return results[0] if results else None
    norm_search = _normalize_title_for_match(search_title)
    if not norm_search:
        return results[0]
    best = None
    best_score = -1
    for r in results:
        title = (r.get("title") or "").strip()
        norm_title = _normalize_title_for_match(title)
        if norm_title == norm_search:
            return r
        score = 0
        if norm_search in norm_title:
            score = 90
        elif norm_title in norm_search:
            score = 30
        else:
            release = (r.get("release_date") or "")[:4]
            try:
                year = int(release) if release else 0
                score = 50 if year >= 2020 else 10
            except ValueError:
                score = 10
        if score > best_score:
            best_score = score
            best = r
    return best if best is not None else results[0]


def enrich_film_tmdb(
    film_title: str,
    film_url: str,
    api_key: str,
    cache: Dict[str, dict],
) -> Dict[str, Any]:
    """Fetch TMDb data for a film; return dict with overview, genres, vote_average, director, cast (first 6 names).
    Uses cache key from normalised title. Returns empty dict on failure or miss.
    """
    search_title = re.sub(r"\s*\([^)]*\)$", "", film_title).strip()
    if not search_title:
        return {}
    cache_key = _tmdb_cache_key(film_title)

    if cache_key in cache:
        entry = cache[cache_key]
        return {
            "overview": entry.get("overview") or "",
            "genres": entry.get("genres") or [],
            "vote_average": entry.get("vote_average"),
            "director": entry.get("director") or "",
            "cast": entry.get("cast") or "",
        }

    time.sleep(TMDB_DELAY_SEC)
    try:
        search_url = "https://api.themoviedb.org/3/search/movie"
        search_r = requests.get(
            search_url,
            params={"api_key": api_key, "query": search_title, "language": "en-GB"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        search_r.raise_for_status()
        data = search_r.json()
        results = data.get("results") or []
        if not results:
            cache[cache_key] = {"overview": "", "genres": [], "vote_average": None, "director": "", "cast": "", "cached_at": datetime.datetime.now().isoformat()}
            return {}
        chosen = _pick_best_tmdb_result(results, search_title)
        if not chosen:
            return {}
        movie_id = chosen.get("id")
        if not movie_id:
            return {}

        time.sleep(TMDB_DELAY_SEC)
        detail_url = f"https://api.themoviedb.org/3/movie/{movie_id}"
        detail_r = requests.get(
            detail_url,
            params={"api_key": api_key, "append_to_response": "credits", "language": "en-GB"},
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        detail_r.raise_for_status()
        movie = detail_r.json()

        GENRE_MAP = {
            28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
            99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
            27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Science Fiction",
            10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
        }
        genre_list = movie.get("genres") or []
        genres = [g.get("name", "").strip() for g in genre_list if g.get("name")]
        if not genres:
            genre_ids = movie.get("genre_ids") or chosen.get("genre_ids") or []
            genres = [GENRE_MAP[g] for g in genre_ids if g in GENRE_MAP]

        overview = (movie.get("overview") or "").strip()
        vote_average = movie.get("vote_average")
        credits = movie.get("credits") or {}
        director_names = []
        for c in credits.get("crew") or []:
            if (c.get("job") or "").strip() == "Director":
                name = (c.get("name") or "").strip()
                if name and name not in director_names:
                    director_names.append(name)
        director_str = ", ".join(director_names[:3])
        cast_names = []
        for c in (credits.get("cast") or [])[:6]:
            name = (c.get("name") or "").strip()
            if name:
                cast_names.append(name)
        cast_str = ", ".join(cast_names)

        out = {
            "overview": overview,
            "genres": genres,
            "vote_average": vote_average,
            "director": director_str,
            "cast": cast_str,
        }
        cache[cache_key] = {**out, "cached_at": datetime.datetime.now().isoformat()}
        return out
    except Exception as e:
        logger.warning("TMDb enrich failed for %s: %s", search_title, e)
        cache[cache_key] = {"overview": "", "genres": [], "vote_average": None, "director": "", "cast": "", "cached_at": datetime.datetime.now().isoformat()}
        return {}


def fetch_with_retries(
    url: str,
    retries: int = HTTP_RETRIES,
    timeout: int = HTTP_TIMEOUT
) -> requests.Response:
    """Return HTTP response, retrying with exponential backoff on errors.

    Args:
        url: The URL to fetch
        retries: Number of retry attempts
        timeout: Request timeout in seconds

    Returns:
        HTTP response object

    Raises:
        requests.RequestException: If all retry attempts fail
    """
    headers = {'User-Agent': USER_AGENT}
    delay = HTTP_RETRY_DELAY

    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            logger.warning("Attempt %d failed: %s", attempt + 1, exc)
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= HTTP_RETRY_MULTIPLIER


def parse_date(text: str) -> Optional[datetime.date]:
    """Parse date from various formats found on the cinema website.

    Supports two formats:
    1. "Expected: DD Month YYYY" (e.g., "Expected: 10 October 2025")
    2. "Expected at WTW Cinemas from the DDth Month" (e.g., "Expected at WTW Cinemas from the 10th October")

    For format 2, assumes current year or next year if the date is in the past.

    Args:
        text: Text containing a date string

    Returns:
        Parsed date object, or None if parsing fails
    """
    # Try the primary format: "Expected: 10 October 2025"
    match = DATE_PATTERN.search(text)
    if match:
        day = int(match.group(1))
        month_str = match.group(2)
        year = int(match.group(3))
    else:
        # Try alternative format: "Expected at WTW Cinemas from the 10th October"
        match = ALT_DATE_PATTERN.search(text)
        if not match:
            return None
        day = int(match.group(1))
        month_str = match.group(2)
        # If year is not in text, assume current or next year based on month
        current_date = datetime.date.today()
        year = current_date.year

    try:
        month = datetime.datetime.strptime(month_str, "%B").month
    except ValueError:
        logger.error("Unrecognised month '%s' in line: %s", month_str, text)
        return None

    try:
        parsed_date = datetime.date(year, month, day)
        # If the date is in the past and we didn't have an explicit year, assume next year
        if parsed_date < datetime.date.today() and not DATE_PATTERN.search(text):
            parsed_date = datetime.date(year + 1, month, day)
        return parsed_date
    except ValueError:
        logger.error("Invalid date detected in line: %s", text)
        return None


def fetch_film_details(film_url: str, cache: Dict[str, dict]) -> Dict[str, str]:
    """Fetch detailed information about a film from its individual page.

    Args:
        film_url: URL to the film's detail page
        cache: Cache dictionary to check for existing data

    Returns:
        Dictionary with film details: runtime, cast, synopsis, director, etc.
    """
    details = {
        'runtime': '',
        'cast': '',
        'synopsis': '',
        'director': ''
    }

    if not film_url:
        return details

    # Use base URL (without ?screen= parameter) as cache key
    # This allows sharing cached details across all cinemas for the same film
    base_url = get_base_film_url(film_url)

    # Check cache first
    if base_url in cache:
        logger.info("Using cached data for: %s", base_url)
        cached_details = cache[base_url].copy()
        cached_details.pop('cached_at', None)  # Remove cache metadata
        return cached_details

    try:
        logger.info("Fetching film details from: %s", film_url)
        response = fetch_with_retries(film_url)
        soup = BeautifulSoup(response.text, "html.parser")

        # Extract runtime - look for text like "119 minutes"
        runtime_pattern = re.compile(r'(\d+)\s*(?:minutes?|mins?)', re.IGNORECASE)
        for text in soup.stripped_strings:
            match = runtime_pattern.search(text)
            if match:
                details['runtime'] = f"{match.group(1)} min"
                break

        # Extract cast - look for "Starring:" label
        for text in soup.stripped_strings:
            if 'starring' in text.lower():
                # Get the next few text nodes
                cast_text = text.split(':', 1)[-1].strip()
                if cast_text and len(cast_text) > 3:
                    details['cast'] = cast_text
                    break

        # Extract synopsis - look for paragraph with substantial text
        # Usually in a div or section describing the film
        for p in soup.find_all('p'):
            text = p.get_text(strip=True)
            # Synopsis is usually longer than MIN_SYNOPSIS_LENGTH characters
            if (len(text) > MIN_SYNOPSIS_LENGTH and
                not any(skip in text.lower() for skip in SYNOPSIS_SKIP_TERMS)):
                details['synopsis'] = text
                break

        # Try to find synopsis in other places if not found
        if not details['synopsis']:
            for div in soup.find_all('div'):
                text = div.get_text(strip=True)
                if (MIN_SYNOPSIS_LENGTH < len(text) < MAX_SYNOPSIS_LENGTH and
                    not any(skip in text.lower() for skip in SYNOPSIS_SKIP_TERMS)):
                    details['synopsis'] = text
                    break

        logger.info("Film details extracted: runtime=%s, cast=%s, synopsis_length=%d",
                   details['runtime'], bool(details['cast']), len(details['synopsis']))

        # Add to cache with timestamp using base URL as key
        cache[base_url] = details.copy()
        cache[base_url]['cached_at'] = datetime.datetime.now().isoformat()

    except requests.RequestException as e:
        logger.warning("Network error fetching film details from %s: %s", film_url, e)
    except Exception as e:
        logger.warning("Unexpected error fetching film details from %s: %s", film_url, e)

    return details


def extract_films(
    url: str,
    cinema_name: str,
    cache: Dict[str, dict]
) -> List[Tuple[datetime.date, str, str, str, Dict[str, str]]]:
    """Extract film releases from the cinema website.

    Args:
        url: Coming soon page URL for the cinema
        cinema_name: Name of the cinema
        cache: Cache dictionary for film details

    Returns:
        List of tuples: (release_date, film_title, cinema_name, film_url, film_details)
    """
    logger.info("Fetching from URL: %s (%s)", url, cinema_name)
    response = fetch_with_retries(url)
    soup = BeautifulSoup(response.text, "html.parser")

    films: List[Tuple[datetime.date, str, str, str, Dict[str, str]]] = []

    # Find all film entries - they are in div.times elements
    # Structure: li > a (with URL) + figcaption > h2 (title) + div.times > p (date)
    times_divs = soup.find_all("div", class_="times")

    for times_div in times_divs:
        # Get parent li element
        parent_li = times_div.parent
        if not parent_li:
            continue

        # Extract film title from h2 (inside figcaption)
        title_elem = parent_li.find("h2")
        if not title_elem:
            continue

        title = title_elem.get_text(strip=True)

        # Remove "(TBC)" or similar suffixes from title
        title = re.sub(r"\s*\([^)]*\)$", "", title)

        # Extract expected date from p tag inside div.times
        date_elem = times_div.find("p")
        if not date_elem:
            continue

        date_text = date_elem.get_text(strip=True)
        release_date = parse_date(date_text)

        # Extract the film URL from the a tag in parent li
        film_url = ""
        link_elem = parent_li.find("a", href=re.compile(r'/film/'))
        if link_elem:
            film_url = link_elem.get('href', '')

        if release_date and title:
            # Fetch detailed information about the film (uses cache if available)
            film_details = fetch_film_details(film_url, cache)

            # Check for duplicates before adding (same film, date, and cinema)
            film_tuple = (release_date, title, cinema_name, film_url, film_details)
            if (release_date, title, cinema_name, film_url) not in [(f[0], f[1], f[2], f[3]) for f in films]:
                films.append(film_tuple)
                logger.info("Found film: %s on %s at %s (URL: %s)", title, release_date, cinema_name, film_url)

    return films


def _format_runtime_display(runtime_str: str) -> str:
    """Format runtime for display: '119 min' -> '2h 1min', '45 min' -> '45 min'."""
    if not runtime_str or not isinstance(runtime_str, str):
        return ""
    m = re.search(r"(\d+)\s*(?:minutes?|mins?)", runtime_str, re.IGNORECASE)
    if not m:
        return runtime_str.strip()
    minutes = int(m.group(1))
    if minutes >= 60:
        h = minutes // 60
        mins = minutes % 60
        if mins:
            return f"{h}h {mins}min"
        return f"{h}h"
    return f"{minutes} min"


def _stars_from_rating(vote_average: Any) -> str:
    """Convert TMDb vote_average (0-10) to 5-star string (★ filled, ☆ empty). E.g. 7.2 -> ★★★★☆."""
    if vote_average is None:
        return ""
    try:
        v = float(vote_average)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return "☆☆☆☆☆"
    if v >= 10:
        return "★★★★★"
    full = min(5, round(v * 0.5))
    return "★" * full + "☆" * (5 - full)


def _cast_first_six_names(cast_str: str) -> str:
    """From WTW-style 'Name1 (Character1), Name2 (Character2)' return first 6 names only, comma-separated."""
    if not cast_str or not isinstance(cast_str, str):
        return ""
    names = []
    for part in cast_str.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([^(]+)(?:\s*\([^)]*\))?\s*$", part)
        if m:
            name = m.group(1).strip()
            if name and name not in names:
                names.append(name)
        else:
            if part not in names:
                names.append(part)
        if len(names) >= 6:
            break
    return ", ".join(names[:6])


def escape_and_fold_ical_text(text: str, prefix: str = "") -> str:
    """Escape and fold text for iCalendar format per RFC 5545.

    Args:
        text: The text to escape and fold
        prefix: Optional prefix for the first line (e.g., "DESCRIPTION:")

    Returns:
        Properly escaped and folded text for iCalendar format
    """
    # Escape special characters per RFC 5545:
    # - Backslash must be escaped as \\
    # - Semicolons and commas should be escaped but not critical for DESCRIPTION
    # - Newlines must be replaced with literal \n
    escaped = text.replace('\\', '\\\\')  # Escape backslashes first
    escaped = escaped.replace('\n', '\\n')  # Replace newlines with literal \n

    # Add the prefix to create the full line
    full_line = prefix + escaped

    # Fold lines at ICAL_LINE_LENGTH characters (RFC 5545 recommends 75 octets)
    # Continuation lines must start with a single space
    if len(full_line) <= ICAL_LINE_LENGTH:
        return full_line

    # Split into chunks of ICAL_LINE_LENGTH (first line) and ICAL_LINE_LENGTH-1 (continuation)
    result = []
    result.append(full_line[:ICAL_LINE_LENGTH])
    remaining = full_line[ICAL_LINE_LENGTH:]

    while remaining:
        # Continuation lines start with space, leaving ICAL_LINE_LENGTH-1 chars for content
        result.append(' ' + remaining[:ICAL_LINE_LENGTH - 1])
        remaining = remaining[ICAL_LINE_LENGTH - 1:]

    return '\n'.join(result)


def generate_alarm(alarm_config: Dict[str, any], release_date: datetime.date) -> str:
    """Generate a VALARM component for iCalendar based on configuration.

    Args:
        alarm_config: Dictionary with alarm settings
        release_date: The date of the film release

    Returns:
        VALARM iCalendar string
    """
    # Get the time to use - either specific time for this alarm or global default
    time_str = alarm_config.get('time', NOTIFICATION_TIME)
    time_parts = time_str.split(':')
    hours = int(time_parts[0])
    minutes = int(time_parts[1]) if len(time_parts) > 1 else 0

    # Calculate the trigger time
    if 'days_before' in alarm_config:
        days = alarm_config['days_before']
        # Calculate trigger as absolute datetime with the specified time
        trigger_datetime = datetime.datetime.combine(release_date, datetime.time(hours, minutes))
        trigger_datetime -= datetime.timedelta(days=days)
        trigger = trigger_datetime.strftime('%Y%m%dT%H%M%S')
        trigger_line = f"TRIGGER;VALUE=DATE-TIME:{trigger}"
    elif 'hours_before' in alarm_config:
        # Legacy support for hours_before
        hours_offset = alarm_config['hours_before']
        if hours_offset < 0:
            # After midnight on the event day
            trigger = f"PT{abs(hours_offset)}H"
            trigger_line = f"TRIGGER:{trigger}"
        else:
            # Before midnight on the event day
            trigger = f"-PT{hours_offset}H"
            trigger_line = f"TRIGGER:{trigger}"
    else:
        # Default: day before at NOTIFICATION_TIME
        trigger_datetime = datetime.datetime.combine(release_date, datetime.time(hours, minutes))
        trigger_datetime -= datetime.timedelta(days=1)
        trigger = trigger_datetime.strftime('%Y%m%dT%H%M%S')
        trigger_line = f"TRIGGER;VALUE=DATE-TIME:{trigger}"

    description = alarm_config.get('description', 'Film Release Reminder')

    return (
        "BEGIN:VALARM\n"
        "ACTION:DISPLAY\n"
        f"DESCRIPTION:{description}\n"
        f"{trigger_line}\n"
        "END:VALARM\n"
    )


def make_ics_event(
    release_date: datetime.date,
    film_title: str,
    cinema_name: str,
    film_url: str = "",
    film_details: Optional[Dict[str, Any]] = None
) -> str:
    """Return an iCalendar VEVENT string for a film release.

    Layout A (WTW-only): title (runtime), description, starring, footer.
    Layout B (TMDb-enriched): title (runtime), star rating, genres, description, starring, footer.
    Runtime is always from WTW. Cast shows first 6 names only (no character names).
    """
    dtend = release_date + datetime.timedelta(days=1)
    summary = f"{film_title} @ WTW {cinema_name}"
    details = film_details or {}

    # Runtime display (always from WTW)
    runtime_display = _format_runtime_display(details.get("runtime") or "")
    if runtime_display:
        title_line = f"{film_title} ({runtime_display})"
    else:
        title_line = film_title

    # Use Layout B if we have TMDb vote_average
    has_tmdb = False
    v = details.get("vote_average")
    if v is not None and isinstance(v, (int, float)):
        has_tmdb = True

    description_parts = [title_line]

    if has_tmdb:
        stars = _stars_from_rating(v)
        if stars:
            description_parts.append(stars)
        genres = details.get("genres")
        if genres:
            if isinstance(genres, list):
                genres_str = ", ".join(str(g).strip() for g in genres if g)
            else:
                genres_str = str(genres).strip()
            if genres_str:
                description_parts.append(genres_str)

    # Description paragraph (TMDb overview or WTW synopsis)
    overview = details.get("overview") or ""
    synopsis = details.get("synopsis") or ""
    desc_text = (overview or synopsis).strip()
    if desc_text:
        description_parts.append(f"\n{desc_text}")

    # Starring: first 6 names only (strip character names from WTW format)
    cast_raw = details.get("cast") or ""
    cast_line = _cast_first_six_names(cast_raw) if cast_raw and ("(" in cast_raw or "," in cast_raw) else (cast_raw.strip() if cast_raw else "")
    if cast_line:
        description_parts.append(f"\nStarring: {cast_line}")

    description_parts.append(f"\nFilm release at WTW Cinemas {cinema_name}")
    if film_url:
        description_parts.append("Book tickets: " + film_url)

    description = "\n".join(description_parts)

    event = (
        "BEGIN:VEVENT\n"
        f"DTSTART;VALUE=DATE:{release_date.strftime('%Y%m%d')}\n"
        f"DTEND;VALUE=DATE:{dtend.strftime('%Y%m%d')}\n"
        f"SUMMARY:{summary}\n"
        + escape_and_fold_ical_text(description, "DESCRIPTION:") + "\n"
        + f"LOCATION:WTW Cinemas {cinema_name}\n"
    )
    if film_url:
        event += f"URL:{film_url}\n"
    if NOTIFICATIONS.get('enabled', False):
        for alarm in NOTIFICATIONS.get('alarms', []):
            event += generate_alarm(alarm, release_date)
    event += "END:VEVENT\n"
    return event


def build_index_html(
    enabled_cinemas: Dict[str, dict],
    films_by_cinema: Dict[str, List[Tuple]],
    stats: Optional[Dict[str, int]] = None,
) -> str:
    """Build the index HTML page with cinema links, how-to, stats, and featured films."""
    cinema_list_html = []
    for cinema_id, info in enabled_cinemas.items():
        name = info["name"]
        count = len(films_by_cinema.get(cinema_id, []))
        ics_url = f"wtw-{cinema_id}.ics"
        cinema_list_html.append(
            f"""      <li class="card">
        <div class="card-icon">🎬</div>
        <h2>{name}</h2>
        <p class="meta">{count} upcoming premiere{'' if count == 1 else 's'}</p>
        <a href="{ics_url}" class="btn"><span class="btn-text-short">Subscribe</span><span class="btn-text-full">Subscribe to calendar</span></a>
      </li>"""
        )

    # Featured films: unique (date, title) sorted by date, first 6
    unique_films = set()
    for cinema_films in films_by_cinema.values():
        for f in cinema_films:
            unique_films.add((f[0], f[1]))  # (release_date, title)
    featured = sorted(unique_films, key=lambda x: x[0])[:6]
    featured_html = "".join(
        f'<div class="featured-film"><span class="date">{d.strftime("%d %b %Y")}</span>{title}</div>'
        for d, title in featured
    )

    stats_html = ""
    if stats:
        past_30 = stats.get("past_30_days", 0)
        ytd_past = stats.get("ytd_past", 0)
        this_month = stats.get("this_month", 0)
        this_year = stats.get("this_year", 0)
        total_upcoming = stats.get("total_upcoming", 0)
        stats_html = f"""
    <section class="stats-section">
      <h2>Premiere stats</h2>
      <p class="stats-intro">Each calendar tracks when each new film premieres at that cinema. Counts are unique films (each film counted once even if at multiple cinemas).</p>
      <p class="stats-group-title">History</p>
      <div class="stats-grid">
        <div class="stat-pill"><span class="value">{past_30}</span><span class="label">last 30 days</span></div>
        <div class="stat-pill"><span class="value">{ytd_past}</span><span class="label">this year (so far)</span></div>
      </div>
      <p class="stats-group-title">Upcoming</p>
      <div class="stats-grid">
        <div class="stat-pill"><span class="value">{this_month}</span><span class="label">this month</span></div>
        <div class="stat-pill"><span class="value">{this_year}</span><span class="label">this year</span></div>
        <div class="stat-pill"><span class="value">{total_upcoming}</span><span class="label">total upcoming</span></div>
      </div>
    </section>"""

    # JavaScript regex for protocol - use variable to avoid backslash in f-string
    js_protocol_re = r"/^https?:\/\//"
    cinema_list_joined = chr(10).join(cinema_list_html)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WTW Cinemas – Movie Premieres</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #0a0a0f;
      --card-bg: #12121a;
      --surface: #12121a;
      --surface-2: #12121a;
      --surface-3: #1a1a24;
      --border: rgba(168,85,247,0.25);
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --cyan: #00d4ff;
      --purple: #a855f7;
      --accent: #00d4ff;
      --accent-dim: rgba(0,212,255,0.15);
      --accent-glow: rgba(0,212,255,0.25);
      --radius: 16px;
      --radius-sm: 10px;
      --radius-lg: 24px;
      --transition: 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      font-family: 'Space Grotesk', system-ui, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.6;
      min-height: 100vh;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }}
    .bg-mesh {{
      position: fixed;
      inset: 0;
      background:
        radial-gradient(ellipse 100% 80% at 50% -30%, var(--accent-dim) 0%, transparent 50%),
        radial-gradient(ellipse 60% 50% at 80% 100%, rgba(0,212,255,0.08) 0%, transparent 40%),
        radial-gradient(ellipse 40% 40% at 10% 90%, rgba(168,85,247,0.05) 0%, transparent 50%);
      pointer-events: none;
      z-index: 0;
    }}
    .bg-grid {{
      position: fixed;
      inset: 0;
      background-image: linear-gradient(rgba(255,255,255,0.02) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.02) 1px, transparent 1px);
      background-size: 60px 60px;
      pointer-events: none;
      z-index: 0;
    }}
    .page {{ position: relative; z-index: 1; max-width: 1100px; margin: 0 auto; padding: 2rem 1.25rem 4rem; }}
    @media (min-width: 640px) {{ .page {{ padding: 3rem 2rem 5rem; }} }}
    .hero {{
      text-align: center;
      padding: 3rem 0 4rem;
      animation: fadeUp 0.8s ease-out;
    }}
    @keyframes fadeUp {{
      from {{ opacity: 0; transform: translateY(20px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .hero .badge {{
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.15em;
      text-transform: uppercase;
      color: var(--accent);
      background: var(--accent-dim);
      padding: 0.4rem 1rem;
      border-radius: 100px;
      margin-bottom: 1.25rem;
    }}
    .hero .badge::before {{
      content: '';
      width: 6px;
      height: 6px;
      background: var(--accent);
      border-radius: 50%;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.5; }}
    }}
    .hero h1 {{
      font-size: clamp(2.25rem, 6vw, 3.5rem);
      font-weight: 800;
      letter-spacing: -0.04em;
      line-height: 1.1;
      margin-bottom: 1rem;
      background: linear-gradient(90deg, var(--cyan), var(--purple));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
    }}
    .hero .tagline {{
      font-size: 1.125rem;
      color: var(--text-muted);
      max-width: 42rem;
      margin: 0 auto;
      font-weight: 400;
      line-height: 1.7;
    }}
    .section-label {{
      font-size: 0.8rem;
      font-weight: 600;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 1rem;
    }}
    .cinemas {{
      list-style: none;
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.75rem;
      margin-bottom: 4rem;
    }}
    @media (min-width: 580px) {{ .cinemas {{ gap: 1rem; }} }}
    @media (min-width: 900px) {{ .cinemas {{ grid-template-columns: repeat(4, 1fr); gap: 1.25rem; }} }}
    .card {{
      background: linear-gradient(135deg, rgba(255,255,255,0.04) 0%, rgba(255,255,255,0.01) 100%);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.75rem;
      transition: all var(--transition);
      position: relative;
      overflow: hidden;
      animation: fadeUp 0.6s ease-out backwards;
    }}
    .card:nth-child(1) {{ animation-delay: 0.1s; }}
    .card:nth-child(2) {{ animation-delay: 0.15s; }}
    .card:nth-child(3) {{ animation-delay: 0.2s; }}
    .card:nth-child(4) {{ animation-delay: 0.25s; }}
    .card::before {{
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--cyan), transparent);
      opacity: 0;
      transition: opacity var(--transition);
    }}
    .card:hover {{
      border-color: rgba(0,212,255,0.4);
      transform: translateY(-4px);
      box-shadow: 0 20px 40px rgba(0,0,0,0.4), 0 0 0 1px rgba(0,212,255,0.1);
    }}
    .card:hover::before {{ opacity: 1; }}
    .card-icon {{
      width: 48px;
      height: 48px;
      background: var(--accent-dim);
      border-radius: var(--radius-sm);
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 1rem;
      font-size: 1.5rem;
    }}
    .card h2 {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 0.25rem; }}
    .card .meta {{ font-size: 0.875rem; color: var(--text-muted); margin-bottom: 1rem; }}
    @media (max-width: 579px) {{
      .card {{ padding: 1rem; }}
      .card-icon {{ width: 36px; height: 36px; font-size: 1.25rem; margin-bottom: 0.5rem; }}
      .card h2 {{ font-size: 1rem; }}
      .card .meta {{ font-size: 0.8rem; margin-bottom: 0.75rem; }}
      .card .btn {{ padding: 0.6rem 1rem; font-size: 0.85rem; }}
      .card .btn .btn-text-short {{ display: inline; }}
      .card .btn .btn-text-full {{ display: none; }}
    }}
    @media (min-width: 580px) {{
      .card .btn .btn-text-short {{ display: none; }}
      .card .btn .btn-text-full {{ display: inline; }}
    }}
    .card .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
      width: 100%;
      padding: 0.75rem 1.25rem;
      background: linear-gradient(135deg, var(--cyan), var(--purple));
      color: var(--bg);
      font-weight: 600;
      font-size: 0.9rem;
      border-radius: var(--radius-sm);
      text-decoration: none;
      transition: all var(--transition);
    }}
    .card .btn:hover {{
      background: linear-gradient(135deg, #20dfff, #b366ff);
      transform: scale(1.02);
      box-shadow: 0 4px 20px var(--accent-glow);
    }}
    .stats-section {{
      margin-bottom: 4rem;
      animation: fadeUp 0.6s ease-out 0.3s backwards;
    }}
    .stats-section h2 {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 1rem; }}
    .stats-intro {{
      font-size: 0.95rem;
      color: var(--text-muted);
      margin-bottom: 1.5rem;
      max-width: 40rem;
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 1rem;
    }}
    .stat-pill {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.25rem;
      text-align: center;
      transition: all var(--transition);
    }}
    .stat-pill:hover {{
      border-color: rgba(0,212,255,0.2);
      background: var(--surface-3);
    }}
    .stat-pill .value {{
      font-size: 2rem;
      font-weight: 800;
      color: var(--accent);
      letter-spacing: -0.03em;
      line-height: 1.2;
      font-family: 'JetBrains Mono', monospace;
    }}
    .stat-pill .label {{ font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem; display: block; }}
    .stats-group-title {{
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--text-muted);
      margin: 1.5rem 0 0.75rem;
    }}
    .howto-section {{
      margin-bottom: 4rem;
      animation: fadeUp 0.6s ease-out 0.4s backwards;
    }}
    .howto-section h2 {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 1rem; }}
    .accordion {{
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
    }}
    .accordion-item {{ border-bottom: 1px solid var(--border); }}
    .accordion-item:last-child {{ border-bottom: none; }}
    .accordion-trigger {{
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 1rem 1.25rem;
      background: var(--surface-2);
      border: none;
      color: var(--text);
      font-family: inherit;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
      text-align: left;
      transition: background var(--transition);
    }}
    .accordion-trigger:hover {{ background: var(--surface-3); }}
    .accordion-trigger::after {{
      content: '+';
      font-size: 1.25rem;
      font-weight: 400;
      color: var(--accent);
      transition: transform var(--transition);
    }}
    .accordion-item.open .accordion-trigger::after {{ transform: rotate(45deg); }}
    .accordion-panel {{
      max-height: 0;
      overflow: hidden;
      transition: max-height var(--transition);
    }}
    .accordion-item.open .accordion-panel {{ max-height: 200px; }}
    .accordion-content {{
      padding: 1rem 1.25rem 1.25rem;
      background: var(--surface);
      font-size: 0.95rem;
      color: var(--text-muted);
      line-height: 1.7;
    }}
    .featured-section {{
      margin-bottom: 4rem;
      animation: fadeUp 0.6s ease-out 0.5s backwards;
    }}
    .featured-section h2 {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 1rem; }}
    .featured-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
      gap: 1rem;
    }}
    .featured-film {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      padding: 1rem;
      font-size: 0.9rem;
      transition: all var(--transition);
    }}
    .featured-film:hover {{
      border-color: rgba(0,212,255,0.4);
      transform: translateY(-2px);
    }}
    .featured-film .date {{
      font-size: 0.75rem;
      color: var(--accent);
      font-weight: 600;
      margin-bottom: 0.5rem;
      display: block;
    }}
    footer {{
      text-align: center;
      padding: 2rem;
      color: var(--text-muted);
      font-size: 0.85rem;
      border-top: 1px solid rgba(255,255,255,0.06);
      margin-top: 4rem;
      animation: fadeUp 0.6s ease-out 0.6s backwards;
    }}
    footer a {{
      color: var(--cyan);
      text-decoration: none;
    }}
    footer a:hover {{
      color: var(--purple);
      text-decoration: underline;
    }}
    .footer-disclaimer {{
      font-size: 0.85rem;
      margin: 0 auto 0.5rem;
      max-width: 36rem;
      line-height: 1.6;
    }}
    .footer-links {{
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 0.5rem 1rem;
      margin-bottom: 0.75rem;
      align-items: center;
    }}
  </style>
</head>
<body>
  <div class="bg-mesh"></div>
  <div class="bg-grid"></div>
  <div class="page">
    <header class="hero">
      <div class="badge">Cornwall · Live</div>
      <h1>WTW Cinemas Movie Premieres</h1>
      <p class="tagline">Subscribe to new movie premieres at your nearest WTW cinema. Never miss a release date. Pick your cinema below and add the calendar to your phone or desktop.</p>
    </header>

    <p class="section-label">Choose your cinema</p>
    <ul class="cinemas">
{cinema_list_joined}
    </ul>
{stats_html}
    <section class="howto-section">
      <h2>How to use</h2>
      <div class="accordion">
        <div class="accordion-item open">
          <button type="button" class="accordion-trigger" aria-expanded="true">Google Calendar</button>
          <div class="accordion-panel">
            <div class="accordion-content">Go to <strong>Add other calendars</strong> &gt; <strong>From URL</strong>, paste the calendar link, then click <strong>Add calendar</strong>. Or open the link above on the device where you're signed in.</div>
          </div>
        </div>
        <div class="accordion-item">
          <button type="button" class="accordion-trigger" aria-expanded="false">Apple Calendar</button>
          <div class="accordion-panel">
            <div class="accordion-content">Go to <strong>File</strong> &gt; <strong>New Calendar Subscription</strong>, paste the calendar URL, then click <strong>Subscribe</strong>. Or click the link above to add it.</div>
          </div>
        </div>
        <div class="accordion-item">
          <button type="button" class="accordion-trigger" aria-expanded="false">Outlook</button>
          <div class="accordion-panel">
            <div class="accordion-content">Go to <strong>Add calendar</strong> &gt; <strong>Subscribe from web</strong>, paste the calendar URL, then confirm. Or open the link above in Outlook.</div>
          </div>
        </div>
      </div>
    </section>

    <section class="featured-section">
      <h2>Coming soon</h2>
      <div class="featured-grid">
        {featured_html}
      </div>
    </section>

    <footer>
      <p class="footer-disclaimer">An open source fan-made project. Calendars update when new premieres are added. Not affiliated with WTW Cinemas.</p>
      <div class="footer-links">
        <a href="https://wtwcinemas.co.uk/">WTW Cinemas</a>
        <span aria-hidden="true">·</span>
        <a href="https://github.com/evenwebb/wtw-cinemas-calendar">Source</a>
        <span aria-hidden="true">·</span>
        <a href="https://github.com/evenwebb/">evenwebb</a>
      </div>
    </footer>
  </div>
  <script>
  (function() {{
    var ua = navigator.userAgent || '';
    var isIOS = /iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    var isMac = /Macintosh|Mac OS X/.test(ua) && !isIOS;
    var isAndroid = /Android/.test(ua);
    document.querySelectorAll('a[href$=".ics"]').forEach(function(link) {{
      var href = link.getAttribute('href');
      if (!href) return;
      var abs = new URL(href, window.location.href).href;
      if (isIOS || isMac) link.href = abs.replace({js_protocol_re}, 'webcal://');
      else if (isAndroid) link.href = 'https://www.google.com/calendar/render?cid=' + encodeURIComponent(abs);
    }});
    document.querySelectorAll('.accordion-trigger').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var item = this.closest('.accordion-item');
        var wasOpen = item.classList.contains('open');
        document.querySelectorAll('.accordion-item').forEach(function(i) {{ i.classList.remove('open'); }});
        if (!wasOpen) item.classList.add('open');
        item.querySelector('.accordion-trigger').setAttribute('aria-expanded', item.classList.contains('open'));
      }});
    }});
  }})();
  </script>
</body>
</html>"""


def validate_configuration() -> None:
    """Validate the configuration settings.

    Raises:
        ValueError: If configuration is invalid
    """
    # Validate notification time format
    if NOTIFICATIONS.get('enabled', False):
        time_pattern = re.compile(r'^([0-1]?[0-9]|2[0-3]):[0-5][0-9]$')
        if not time_pattern.match(NOTIFICATION_TIME):
            raise ValueError(
                f"Invalid NOTIFICATION_TIME: '{NOTIFICATION_TIME}'. "
                f"Must be in HH:MM format (e.g., '09:00')"
            )

        # Validate individual alarm times
        for alarm in NOTIFICATIONS.get('alarms', []):
            if 'time' in alarm:
                if not time_pattern.match(alarm['time']):
                    raise ValueError(
                        f"Invalid alarm time: '{alarm['time']}'. "
                        f"Must be in HH:MM format (e.g., '18:00')"
                    )

            # Validate alarm has required fields
            if 'days_before' not in alarm and 'hours_before' not in alarm:
                raise ValueError(
                    "Each alarm must have either 'days_before' or 'hours_before'"
                )

    # Validate cache expiry days
    if CACHE_EXPIRY_DAYS < 1:
        raise ValueError(
            f"CACHE_EXPIRY_DAYS must be at least 1, got {CACHE_EXPIRY_DAYS}"
        )

    # Validate at least one cinema is enabled
    if not any(cinema['enabled'] for cinema in CINEMAS.values()):
        raise ValueError(
            "At least one cinema must be enabled in CINEMAS configuration"
        )


def main() -> None:
    """Main function to scrape films and generate iCal file.

    Orchestrates the entire scraping workflow:
    1. Loads film details cache
    2. Scrapes enabled cinemas for film releases
    3. Fetches detailed film information (with caching)
    4. Generates iCalendar file with all events
    5. Saves updated cache
    6. Displays summary of found films

    Exits early if no cinemas are enabled or no films are found.
    """
    # Validate configuration first
    try:
        validate_configuration()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        print(f"Configuration Error: {e}")
        return

    all_films: List[Tuple[datetime.date, str, str, str, Dict[str, Any], str]] = []

    # Load cache
    cache = load_cache()

    # Use cinemas marked enabled in CINEMAS (single source of truth for both local and CI)
    enabled_cinemas = {k: v for k, v in CINEMAS.items() if v["enabled"]}

    if not enabled_cinemas:
        logger.error("No cinemas enabled in configuration")
        print("Error: No cinemas enabled. Please enable at least one cinema in the CINEMAS configuration.")
        return

    print(f"Scraping {len(enabled_cinemas)} cinema(s): {', '.join([c['name'] for c in enabled_cinemas.values()])}\n")

    # Scrape each enabled cinema (append cinema_id to each film for per-cinema output)
    for cinema_id, cinema_info in enabled_cinemas.items():
        cinema_name = cinema_info['name']
        cinema_url = cinema_info['url']

        try:
            films = extract_films(cinema_url, cinema_name, cache)
            for f in films:
                all_films.append((*f, cinema_id))
            print(f"✓ {cinema_name}: Found {len(films)} film(s)")
        except Exception as e:
            logger.error("Error scraping %s: %s", cinema_name, e)
            print(f"✗ {cinema_name}: Error - {e}")

    # Save updated cache
    save_cache(cache)

    # Optional TMDb enrichment (when API key is set). Enrich each unique film once and apply to all cinema entries.
    api_key = (os.environ.get("TMDB_API_KEY") or "").strip()
    if api_key:
        tmdb_cache = load_tmdb_cache()
        unique_by_key: Dict[str, Tuple[str, str, List[int]]] = {}
        for i, (release_date, title, cinema_name, film_url, film_details, cinema_id) in enumerate(all_films):
            key = _tmdb_cache_key(title)
            if key not in unique_by_key:
                unique_by_key[key] = (title, film_url, [i])
            else:
                unique_by_key[key][2].append(i)
        for key, (title, film_url, indices) in unique_by_key.items():
            extra = enrich_film_tmdb(title, film_url, api_key, tmdb_cache)
            if not extra:
                continue
            for i in indices:
                release_date, t, cinema_name, furl, film_details, cinema_id = all_films[i]
                film_details = dict(film_details)
                if extra.get("overview"):
                    film_details["overview"] = extra["overview"]
                if extra.get("genres") is not None:
                    film_details["genres"] = extra["genres"]
                if extra.get("vote_average") is not None:
                    film_details["vote_average"] = extra["vote_average"]
                if extra.get("director"):
                    film_details["director"] = extra["director"]
                if extra.get("cast"):
                    film_details["cast"] = extra["cast"]
                all_films[i] = (release_date, t, cinema_name, furl, film_details, cinema_id)
        save_tmdb_cache(tmdb_cache)
        logger.info("TMDb enrichment applied (Layout B); %d unique films, %d total entries", len(unique_by_key), len(all_films))
    else:
        logger.info("TMDB_API_KEY not set; using WTW-only data (Layout A)")

    if not all_films:
        logger.warning("No films found across any cinema")
        print("\nWarning: No films found across any cinema")
        return

    # Sort films by release date, then by cinema name
    all_films.sort(key=lambda x: (x[0], x[2]))

    # Group by cinema for per-cinema .ics files
    films_by_cinema: Dict[str, List[Tuple]] = {}
    for f in all_films:
        cid = f[5]
        if cid not in films_by_cinema:
            films_by_cinema[cid] = []
        films_by_cinema[cid].append(f)

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    # Remove old .ics files without wtw- prefix (e.g. after rename)
    output_dir = Path(OUTPUT_DIR)
    for old_ics in output_dir.glob("*.ics"):
        if not old_ics.name.startswith("wtw-"):
            old_ics.unlink()
            logger.info("Removed legacy %s", old_ics.name)

    for cinema_id in enabled_cinemas:
        cinema_name = enabled_cinemas[cinema_id]["name"]
        cinema_films = films_by_cinema.get(cinema_id, [])
        events = []
        for release_date, title, cname, film_url, film_details, _ in cinema_films:
            events.append(make_ics_event(release_date, title, cname, film_url, film_details))
        ical = (
            "BEGIN:VCALENDAR\n"
            "VERSION:2.0\n"
            "PRODID:-//WTW Cinemas//EN\n"
            "CALSCALE:GREGORIAN\n"
            f"X-WR-CALNAME:WTW {cinema_name} Movie Premieres\n"
            f"X-WR-CALDESC:Upcoming movie premieres at WTW Cinemas {cinema_name}\n"
            + "".join(events)
            + "END:VCALENDAR\n"
        )
        out_path = Path(OUTPUT_DIR) / f"wtw-{cinema_id}.ics"
        out_path.write_text(ical, encoding="utf-8")
        logger.info("Wrote %s (%d events)", out_path, len(events))

    # Release-date stats: History from persisted file, Upcoming from this run
    today = datetime.date.today()
    unique_releases = set((f[0], f[1]) for f in all_films)  # (release_date, title)
    release_history = load_release_history()
    release_history |= unique_releases
    save_release_history(release_history)
    past_cutoff_30 = today - datetime.timedelta(days=30)
    release_stats = {
        "past_30_days": sum(1 for (d, _) in release_history if past_cutoff_30 <= d <= today),
        "ytd_past": sum(1 for (d, _) in release_history if d.year == today.year and d < today),
        "this_month": sum(1 for (d, _) in unique_releases if d.year == today.year and d.month == today.month and d >= today),
        "this_year": sum(1 for (d, _) in unique_releases if d.year == today.year and d >= today),
        "total_upcoming": sum(1 for (d, _) in unique_releases if d >= today),
    }

    # Index page with links, how-to, and stats
    index_html = build_index_html(enabled_cinemas, films_by_cinema, stats=release_stats)
    Path(OUTPUT_DIR, "index.html").write_text(index_html, encoding="utf-8")
    logger.info("Wrote %s/index.html", OUTPUT_DIR)

    print(f"\n✓ Created {OUTPUT_DIR}/ with {len(films_by_cinema)} calendar(s) and index page\n")

    # Group films by date for display
    for release_date, date_group in groupby(all_films, key=lambda x: x[0]):
        films_on_date = list(date_group)
        print(f"{release_date.strftime('%d %B %Y')}:")
        for _, title, cinema_name, _, _, _ in films_on_date:
            print(f"  • {title} @ {cinema_name}")


if __name__ == "__main__":
    main()
