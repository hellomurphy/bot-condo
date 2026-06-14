"""Provider registry — maps provider_id strings to scraper modules.

To add a new provider:
1. Create scraper/<name>.py with PROVIDER_ID, PROVIDER_NAME, URL_PATTERN, scrape_project()
2. Import it here and add to PROVIDERS.
"""
from scraper import propertyhub as _ph

PROVIDERS: dict = {
    _ph.PROVIDER_ID: _ph,
}
