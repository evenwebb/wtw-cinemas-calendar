# WTW Cinema's Movie Schedule Scraper

This repository contains a Python script that scrapes upcoming film releases from the Cornwall cinema chain, WTW Cinemas, across multiple locations (St Austell, Newquay, Wadebridge, and Truro) and converts them into a ready-to-import iCalendar (`.ics`) file. The script can be run for free using GitHub Actions. Clone the repository, edit the configuration, and enable actions to push changes to the repository in repository settings.

Running the scraper generates `wtw_cinema.ics` which can be added to Google Calendar, Outlook, Apple Calendar or any other iCalendar compatible application.

## Features

* **Multi-cinema support**: Scrape from any combination of WTW Cinema locations (St Austell, Newquay, Wadebridge, Truro)
* **Rich event descriptions**: Automatically fetches and includes runtime, synopsis, and cast information for each film
* **Smart caching**: Film details are cached locally to avoid unnecessary re-scraping (7-day cache expiry)
* **Configurable notifications**: Optional calendar reminders (day before, day of, 1 week before, or custom)
* **Direct booking links**: Each calendar event includes a URL to the film's booking page
* Fetches the latest film releases directly from the WTW Cinemas website using `requests`
* Parses film titles and release dates with BeautifulSoup and regular expressions
* Event titles show both film name and cinema location (e.g., "Tron: Ares @ WTW St Austell")
* Handles multiple date formats used on the website
* Deduplicates entries to avoid duplicate calendar events
* Writes a standards-compliant `.ics` file and logs any parsing errors to `cinema_log.txt`

## Requirements

* Python 3.10 or newer
* `requests`
* `beautifulsoup4`

Install the dependencies with:

```bash
pip install -r requirements.txt
```

## Usage

From the repository root run:

```bash
python cinema_scraper.py
```

The script will fetch the latest film releases from the WTW Cinemas website, create `wtw_cinema.ics` in the current directory and print a confirmation message. Any parse errors are recorded in `cinema_log.txt` for inspection.

A typical entry in the generated calendar looks like:

```text
BEGIN:VEVENT
DTSTART;VALUE=DATE:20251010
DTEND;VALUE=DATE:20251011
SUMMARY:Tron: Ares @ WTW St Austell
DESCRIPTION:Tron: Ares (119 min)

A highly sophisticated Program called Ares is sent from the digital world into the real world on a dangerous mission, marking humankind's first encounter with A.I. beings.

🎬 Film release at WTW Cinemas St Austell
🎟️ Click the URL to book tickets
LOCATION:WTW Cinemas St Austell
URL:https://wtwcinemas.co.uk/film/tron-ares/?screen=st-austell
END:VEVENT
```

Each calendar event includes:
- **Runtime**: Film duration (e.g., "119 min") when available
- **Synopsis**: Full plot description from the film's detail page
- **Cast**: Starring actors when available
- **Booking URL**: Direct link to purchase tickets for the specific cinema

## GitHub Actions

The repository includes a GitHub Actions workflow (`.github/workflows/scrape_cinema.yml`) that automatically runs the scraper daily at 09:00 UTC. You can also trigger it manually from the GitHub Actions tab.

The workflow will:
1. Install Python and dependencies
2. Run the scraper
3. Commit and push the updated `.ics` file if there are changes

## Configuration

### Selecting Cinemas

The scraper supports all four WTW Cinema locations. You can choose which cinemas to include by editing the `CINEMAS` configuration at the top of `cinema_scraper.py`:

```python
CINEMAS = {
    'st-austell': {
        'enabled': True,  # Set to False to disable
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
```

Simply set `'enabled': False` for any cinema you don't want to include in your calendar.

### Notification Settings

The scraper supports calendar notifications/reminders. By default, no notifications are set. You can enable notifications by editing the `NOTIFICATIONS` configuration in `cinema_scraper.py`.

**Setting Your Notification Time:**

First, set your preferred notification time at the top of the notification settings:

```python
NOTIFICATION_TIME = '09:00'  # 9:00 AM - Change to your preferred time
```

This time will be used for ALL notifications unless you override it for a specific alarm. Examples:
- `'09:00'` = 9:00 AM
- `'18:00'` = 6:00 PM
- `'12:30'` = 12:30 PM

**Available preset options:**

**Option 1: No notifications (default)**
```python
NOTIFICATIONS = {
    'enabled': False,
    'alarms': []
}
```

**Option 2: Day before (uses NOTIFICATION_TIME)**
```python
NOTIFICATIONS = {
    'enabled': True,
    'alarms': [
        {'days_before': 1, 'description': 'Film releases tomorrow'}
    ]
}
```

**Option 3: Day of event (uses NOTIFICATION_TIME)**
```python
NOTIFICATIONS = {
    'enabled': True,
    'alarms': [
        {'days_before': 0, 'description': 'Films out today!'}
    ]
}
```

**Option 4: Multiple reminders (all use NOTIFICATION_TIME)**
```python
NOTIFICATIONS = {
    'enabled': True,
    'alarms': [
        {'days_before': 7, 'description': 'Film releases in 1 week'},
        {'days_before': 1, 'description': 'Film releases tomorrow'},
        {'days_before': 0, 'description': 'Films out today!'}
    ]
}
```

**Option 5: Custom time for specific notification (overrides NOTIFICATION_TIME)**
```python
NOTIFICATIONS = {
    'enabled': True,
    'alarms': [
        {'days_before': 1, 'description': 'Evening reminder', 'time': '18:00'},  # 6pm
        {'days_before': 0, 'description': 'Morning reminder'}  # Uses NOTIFICATION_TIME
    ]
}
```

**Notification format:**
- `days_before`: Number of days before the event (0 = day of event, 1 = day before, etc.)
- `description`: Custom message for the notification
- `time`: (Optional) Override the default NOTIFICATION_TIME for this specific alarm (format: 'HH:MM' in 24-hour format)

### Other Configuration Options

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `CINEMAS` | All 4 locations enabled | Dictionary of cinema locations to scrape |
| `NOTIFICATION_TIME` | `'09:00'` | Default time of day for all notifications (24-hour format) |
| `NOTIFICATIONS` | No notifications | Calendar notification/alarm settings |
| `CACHE_FILE` | `.film_cache.json` | Location of the film details cache file |
| `CACHE_EXPIRY_DAYS` | `7` | Number of days to keep cached film details before refreshing |
| `DATE_PATTERN` | Regex for "Expected: DD Month YYYY" | Primary date format to parse |
| `ALT_DATE_PATTERN` | Regex for "Expected at WTW Cinemas from the DDth Month" | Alternative date format |

## How it Works

1. **Cache Loading**: The script loads previously cached film details from `.film_cache.json` (if it exists)
2. **Cinema Selection**: The script reads the `CINEMAS` configuration to determine which locations to scrape
3. **Fetching**: For each enabled cinema, the script fetches the HTML from the cinema website
4. **Parsing**: BeautifulSoup extracts film data from `div.content` elements containing:
   - `<h2>` tags for film titles
   - `<p>` tags for release dates
   - `<a>` tags for film URLs
5. **Film Details**: For each film, the script either:
   - Uses cached data if available and less than 7 days old
   - Fetches the individual film page to extract:
     - Runtime (using regex pattern matching)
     - Synopsis (from paragraph elements)
     - Cast information (when available)
6. **Cache Saving**: Newly fetched film details are added to the cache with timestamps
7. **Date Parsing**: Regular expressions extract dates from various formats
8. **Deduplication**: The script checks for duplicate (date, title, cinema) tuples before adding events
9. **iCal Generation**: Each film is converted to an iCalendar VEVENT with:
   - Rich description including runtime, synopsis, and cast
   - Cinema location
   - Direct booking URL

### Caching Behavior

The scraper implements intelligent caching to minimize unnecessary web requests:

- **First run**: Fetches all film details from the website and saves to `.film_cache.json`
- **Subsequent runs**: Uses cached data for films already fetched within the last 7 days
- **Cache expiry**: Entries older than 7 days are automatically removed and re-fetched
- **Performance**: Reduces scraping time by ~90% on subsequent runs when data hasn't changed

To force a refresh of all data, simply delete the `.film_cache.json` file before running the script.

## Example Output

When scraping all four cinemas:

```
Scraping 4 cinema(s): St Austell, Newquay, Wadebridge, Truro

✓ St Austell: Found 4 film(s)
✓ Newquay: Found 4 film(s)
✓ Wadebridge: Found 4 film(s)
✓ Truro: Found 4 film(s)

✓ Created wtw_cinema.ics with 16 film release(s)

10 October 2025:
  • Tron: Ares @ Newquay
  • Tron: Ares @ St Austell
  • Tron: Ares @ Truro
  • Tron: Ares @ Wadebridge
17 October 2025:
  • Gabby's Dollhouse: The Movie @ Newquay
  • Gabby's Dollhouse: The Movie @ St Austell
  • Gabby's Dollhouse: The Movie @ Truro
  • Gabby's Dollhouse: The Movie @ Wadebridge
```

## Related Projects

This scraper was inspired by the [Penrice Academy Calendar Scraper](example/) which scrapes term dates and converts them to iCal format.

## License

This project is provided as-is for personal use. Please respect the cinema's website terms of service when scraping.
