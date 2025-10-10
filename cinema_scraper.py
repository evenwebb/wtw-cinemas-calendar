import re
import datetime
import logging
import json
import os
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import time


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
error_handler = logging.FileHandler("cinema_log.txt")
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


def load_cache() -> dict:
    """Load the film details cache from disk."""
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
    except Exception as e:
        logger.warning("Failed to load cache: %s", e)
        return {}


def save_cache(cache: dict) -> None:
    """Save the film details cache to disk."""
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
        logger.info("Saved cache with %d entries", len(cache))
    except Exception as e:
        logger.warning("Failed to save cache: %s", e)


def fetch_with_retries(url: str, retries: int = 3, timeout: int = 60) -> requests.Response:
    """Return HTTP response, retrying with exponential backoff on errors."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
    }
    delay = 1
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
            delay *= 2


def parse_date(text: str) -> datetime.date | None:
    """Parse date from various formats found on the cinema website."""
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


def fetch_film_details(film_url: str, cache: dict) -> dict:
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
            # Synopsis is usually longer than 50 characters
            if len(text) > 50 and not any(skip in text.lower() for skip in ['cookie', 'privacy', 'terms', 'wheelchair', 'audio description']):
                details['synopsis'] = text
                break

        # Try to find synopsis in other places if not found
        if not details['synopsis']:
            for div in soup.find_all('div'):
                text = div.get_text(strip=True)
                if 50 < len(text) < 500 and not any(skip in text.lower() for skip in ['cookie', 'privacy', 'terms']):
                    details['synopsis'] = text
                    break

        logger.info("Film details extracted: runtime=%s, cast=%s, synopsis_length=%d",
                   details['runtime'], bool(details['cast']), len(details['synopsis']))

        # Add to cache with timestamp using base URL as key
        cache[base_url] = details.copy()
        cache[base_url]['cached_at'] = datetime.datetime.now().isoformat()

    except Exception as e:
        logger.warning("Failed to fetch film details from %s: %s", film_url, e)

    return details


def extract_films(url: str, cinema_name: str, cache: dict) -> list[tuple[datetime.date, str, str, str, dict]]:
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

    films: list[tuple[datetime.date, str, str, str, dict]] = []

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

    # Fold lines at 75 characters (RFC 5545 recommends 75 octets)
    # Continuation lines must start with a single space
    if len(full_line) <= 75:
        return full_line

    # Split into chunks of 75 characters (first line) and 74 characters (continuation lines)
    result = []
    result.append(full_line[:75])
    remaining = full_line[75:]

    while remaining:
        # Continuation lines start with space, leaving 74 chars for content
        result.append(' ' + remaining[:74])
        remaining = remaining[74:]

    return '\n'.join(result)


def generate_alarm(alarm_config: dict, release_date: datetime.date) -> str:
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


def make_ics_event(release_date: datetime.date, film_title: str, cinema_name: str, film_url: str = "", film_details: dict = None) -> str:
    """Return an iCalendar VEVENT string for a film release."""
    # Cinema releases are all-day events
    # iCal DTEND should be the day after the event for all-day events
    dtend = release_date + datetime.timedelta(days=1)

    summary = f"{film_title} @ WTW {cinema_name}"

    # Build rich description
    description_parts = []

    # Add title with runtime if available
    if film_details and film_details.get('runtime'):
        description_parts.append(f"{film_title} ({film_details['runtime']})")
    else:
        description_parts.append(film_title)

    # Add cast if available
    if film_details and film_details.get('cast'):
        description_parts.append(f"Starring: {film_details['cast']}")

    # Add synopsis if available
    if film_details and film_details.get('synopsis'):
        description_parts.append(f"\n{film_details['synopsis']}")

    # Add location info
    description_parts.append(f"\n🎬 Film release at WTW Cinemas {cinema_name}")

    # Add booking info if URL available
    if film_url:
        description_parts.append("🎟️ Click the URL to book tickets")

    description = "\n".join(description_parts)

    # Build the event
    event = (
        "BEGIN:VEVENT\n"
        f"DTSTART;VALUE=DATE:{release_date.strftime('%Y%m%d')}\n"
        f"DTEND;VALUE=DATE:{dtend.strftime('%Y%m%d')}\n"
        f"SUMMARY:{summary}\n"
        + escape_and_fold_ical_text(description, "DESCRIPTION:") + "\n"
        + f"LOCATION:WTW Cinemas {cinema_name}\n"
    )

    # Add URL if available (for booking tickets)
    if film_url:
        event += f"URL:{film_url}\n"

    # Add alarms if notifications are enabled
    if NOTIFICATIONS.get('enabled', False):
        for alarm in NOTIFICATIONS.get('alarms', []):
            event += generate_alarm(alarm, release_date)

    event += "END:VEVENT\n"

    return event


def main() -> None:
    """Main function to scrape films and generate iCal file."""
    all_films: list[tuple[datetime.date, str, str, str, dict]] = []

    # Load cache
    cache = load_cache()

    # Get list of enabled cinemas
    enabled_cinemas = {k: v for k, v in CINEMAS.items() if v['enabled']}

    if not enabled_cinemas:
        logger.error("No cinemas enabled in configuration")
        print("Error: No cinemas enabled. Please enable at least one cinema in the CINEMAS configuration.")
        return

    print(f"Scraping {len(enabled_cinemas)} cinema(s): {', '.join([c['name'] for c in enabled_cinemas.values()])}\n")

    # Scrape each enabled cinema
    for cinema_id, cinema_info in enabled_cinemas.items():
        cinema_name = cinema_info['name']
        cinema_url = cinema_info['url']

        try:
            films = extract_films(cinema_url, cinema_name, cache)
            all_films.extend(films)
            print(f"✓ {cinema_name}: Found {len(films)} film(s)")
        except Exception as e:
            logger.error("Error scraping %s: %s", cinema_name, e)
            print(f"✗ {cinema_name}: Error - {e}")

    # Save updated cache
    save_cache(cache)

    if not all_films:
        logger.warning("No films found across any cinema")
        print("\nWarning: No films found across any cinema")
        return

    # Sort films by release date, then by cinema name
    all_films.sort(key=lambda x: (x[0], x[2]))

    # Generate iCal events
    events: list[str] = []
    for release_date, title, cinema_name, film_url, film_details in all_films:
        events.append(make_ics_event(release_date, title, cinema_name, film_url, film_details))

    # Build cinema names list for calendar description
    cinema_names = ', '.join([c['name'] for c in enabled_cinemas.values()])

    # Create iCalendar file
    ical = (
        "BEGIN:VCALENDAR\n"
        "VERSION:2.0\n"
        "PRODID:-//WTW Cinemas//EN\n"
        "CALSCALE:GREGORIAN\n"
        "X-WR-CALNAME:WTW Cinema Film Releases\n"
        f"X-WR-CALDESC:Upcoming film releases at WTW Cinemas ({cinema_names})\n"
        + "".join(events)
        + "END:VCALENDAR\n"
    )

    output_file = "wtw_cinema.ics"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(ical)

    print(f"\n✓ Created {output_file} with {len(all_films)} film release(s)\n")

    # Group films by date for display
    from itertools import groupby
    for release_date, date_group in groupby(all_films, key=lambda x: x[0]):
        films_on_date = list(date_group)
        print(f"{release_date.strftime('%d %B %Y')}:")
        for _, title, cinema_name, _, _ in films_on_date:
            print(f"  • {title} @ {cinema_name}")


if __name__ == "__main__":
    main()
