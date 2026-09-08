# Scraping Sources

## Overview

EchoFlow includes a license-aware scraper for seeding the catalog from public audio archives. Modular source connectors.

**Location:** `ai_ml/scrapers/sources/`

---

## Supported Sources

| Source | Module | Requires | License Enforcement |
|--------|--------|----------|---------------------|
| Wikimedia Commons | `wikimedia_commons.py` | None | MIME type `audio/*` |
| Internet Archive | `internet_archive.py` | None | Allowed-license filter |
| Freesound | `freesound.py` | `FREESOUND_API_KEY` | License filter |
| Kaggle | `kaggle.py` | `SCRAPER_KAGGLE_LOCAL_PATH` | Local file, manual |
| Openverse | `openverse.py` | Optional `SCRAPER_OPENVERSE_API_KEY` | Family-based; NC gated by `SCRAPER_ALLOW_NC` |
| LibriVox | `librivox.py` | None | All PD (CC0) |
| Free Music Archive | `free_music_archive.py` | None | Family-based; IA mirror |
| Pixabay | `pixabay.py` | `SCRAPER_PIXABAY_API_KEY` | Family-based (PIXABAY) |
| Podcast Index | `podcast_index.py` | `SCRAPER_PODCAST_INDEX_API_KEY` + `_SECRET` | Per-show UNKNOWN; gated at moderation |
| Podcast RSS (generic) | `podcast_rss.py` | None (uses default feed) | Per-show UNKNOWN |
| BBC Sound Effects | `bbc_sound_effects.py` | None | RemArc-NC; runtime-gated by `SCRAPER_ALLOW_NC` |
| Musopen | `musopen.py` | None | All PD (CC0) |
| LOC National Jukebox | `loc_national_jukebox.py` | None | All PD (CC0) |
| US Gov Audio | `usgov_audio.py` | None | All PD (CC0); fans out across C-SPAN, NASA, USGS via IA |

---

## 1. Wikimedia Commons (`wikimedia_commons.py`)

### API
```python
API = 'https://commons.wikimedia.org/w/api.php'
params = {
    'action': 'query',
    'format': 'json',
    'list': 'allimages',
    'ailimit': str(limit),
    'aiprop': 'url|mime'
}
```

### Filtering
```python
mime = it.get('mime', '')
if not mime.startswith('audio'):
    continue  # Skip non-audio
```

### Output
```python
{
    'url': direct_file_url,
    'title': filename,
    'page_url': f"https://commons.wikimedia.org/wiki/File:{name}",
    'mime': mime_type
}
```

### License
- No license field from API
- Relies on `SCRAPER_ALLOW_LICENSES` check in management command
- Manual review needed for compliance

---

## 2. Internet Archive (`internet_archive.py`)

### Search
```python
SEARCH = 'https://archive.org/advancedsearch.php'
params = {
    'q': 'mediatype:(audio)',
    'fl': 'identifier,title',
    'rows': str(limit),
    'page': '1',
    'output': 'json'
}
```

### Metadata Fetch (Per Item)
```python
meta_url = f'https://archive.org/metadata/{identifier}'
files = metadata.get('files', [])

for f in files:
    fmt = (f.get('format') or '').lower()
    if any(x in fmt for x in ('mp3', 'vbr mp3', 'wav', 'ogg', 'flac')):
        url = f'https://archive.org/download/{identifier}/{name}'
        break
```

### Retry Logic
```python
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=['GET']
)
session.mount('https://', HTTPAdapter(max_retries=retries))
```

### Output
```python
{
    'url': direct_audio_url,
    'title': title,
    'page_url': f'https://archive.org/details/{identifier}',
    'id': identifier
}
```

### License
- Internet Archive items have license metadata
- Checked against `SCRAPER_ALLOW_LICENSES` in command

---

## 3. Freesound (`freesound.py`)

### Requirements
```python
api_key = getattr(settings, 'FREESOUND_API_KEY', None)
if not api_key:
    logger.warning('Freesound API key not configured; skipping')
    return []
```

### Search
```python
SEARCH = 'https://freesound.org/apiv2/search/text/'
params = {
    'query': 'duration:[0 TO 300]',  # ≤5 minutes
    'fields': 'id,name,previews,license,url',
    'page_size': min(limit, 50)
}
headers = {'Authorization': f'Token {api_key}'}
```

### Preview URLs Only
```python
# Full downloads require OAuth2
# With API token: only preview URLs accessible
preview = item.get('previews', {}).get('preview_hq_mp3') \
         or item.get('previews', {}).get('preview_lq_mp3')
```

### Output
```python
{
    'url': preview_url,
    'title': name,
    'page_url': freesound_url,
    'id': freesound_id,
    'license': license_type  # e.g., "CC0", "CC-BY"
}
```

---

## 4. Kaggle (`kaggle.py`)

### Local File Ingestion
```python
base = getattr(settings, 'SCRAPER_KAGGLE_LOCAL_PATH', None)
if not base or not os.path.isdir(base):
    return []

for root, _, files in os.walk(base):
    for fn in files:
        if fn.lower().endswith(('.mp3', '.wav', '.ogg', '.flac', '.aac')):
            path = os.path.join(root, fn)
            results.append({
                'url': f'file://{path}',  # file:// scheme
                'title': fn,
                'page_url': path,
                'id': path
            })
            if len(results) >= limit:
                return results
```

### Use Case
- Local dataset downloads (e.g., AudioSet, FSD50K)
- Manual license verification required

---

## Source Registry (`scrapers/sources/__init__.py`)

```python
from . import (
    wikimedia_commons, internet_archive, freesound, kaggle,
    openverse, librivox, free_music_archive, pixabay,
    podcast_index, podcast_rss, bbc_sound_effects,
    musopen, loc_national_jukebox, usgov_audio,
)

SOURCES = {
    'wikimedia': wikimedia_commons,
    'internet_archive': internet_archive,
    'freesound': freesound,
    'kaggle': kaggle,
    'openverse': openverse,
    'librivox': librivox,
    'free_music_archive': free_music_archive,
    'pixabay': pixabay,
    'podcast_index': podcast_index,
    'podcast_rss': podcast_rss,
    'bbc_sound_effects': bbc_sound_effects,
    'musopen': musopen,
    'loc_national_jukebox': loc_national_jukebox,
    'usgov_audio': usgov_audio,
}
```

**Usage:**
```python
from ai_ml.scrapers.sources import SOURCES
module = SOURCES.get(source_name)
items = module.fetch_audio(limit=10)
```

---

## Common Infrastructure

### Base (`base.py`)

#### Robots.txt Checker
```python
class RobotsTxtChecker:
    def allowed(self, url, user_agent=None):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self.parsers.get(base) or RobotFileParser()
        rp.set_url(base + "/robots.txt")
        rp.read()
        return rp.can_fetch(user_agent, url)
```

#### Rate Limiter
```python
class RateLimiter:
    def __init__(self, max_per_min=30):
        self.min_interval = 60.0 / max_per_min
        self.last_access = {}
    
    def wait(self, url):
        host = urlparse(url).netloc
        elapsed = time.time() - self.last_access.get(host, 0)
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self.last_access[host] = time.time()
```

#### Session Factory
```python
def get_session():
    s = requests.Session()
    s.headers.update({'User-Agent': 'EchoFlowScraper/1.0 (+contact@example.com)'})
    return s
```

---

## Downloader (`downloader.py`)

```python
def download_audio(url, max_bytes=50_000_000, timeout=30):
    # 1. Check robots.txt
    if not RobotsTxtChecker().allowed(url):
        raise RuntimeError(f"Blocked by robots.txt: {url}")
    
    # 2. Rate limit per host
    RateLimiter(max_per_min=30).wait(url)
    
    # 3. Stream download with size limit
    resp = session.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()
    
    # 4. Validate content type
    content_type = resp.headers.get('Content-Type', '')
    if 'audio' not in content_type and not url.endswith(audio_extensions):
        raise RuntimeError(f"Not audio: {content_type}")
    
    # 5. Check Content-Length
    if content_length and int(content_length) > max_bytes:
        raise RuntimeError("File too large")
    
    # 6. Stream to temp file with running size check
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    for chunk in resp.iter_content(8192):
        total += len(chunk)
        if total > max_bytes:
            os.unlink(tmp.name)
            raise RuntimeError("Downloaded file exceeds limit")
        tmp.write(chunk)
    
    return tmp.name
```

---

## License Enforcement

### Settings
```python
SCRAPER_ALLOW_LICENSES = os.getenv('SCRAPER_ALLOW_LICENSES', 'CC0,CC-BY,CC-BY-SA,CC-BY-NC').split(',')
```

### Management Command Check
```python
allowed = [s.upper() for s in getattr(settings, 'SCRAPER_ALLOW_LICENSES', [])]
lic_upper = str(lic_raw).upper() if lic_raw else ''

if lic_upper and lic_upper != 'UNKNOWN' and not any(a in lic_upper for a in allowed):
    self.stdout.write(self.style.WARNING(f'Skipping: license "{lic_raw}" not allowed'))
    continue

if not lic_upper or lic_upper == 'UNKNOWN':
    self.stdout.write(self.style.WARNING(f'Importing with UNKNOWN license — verify'))
```

### Allowed Licenses (Default)
| License | Commercial Use | Modification | Attribution |
|---------|---------------|--------------|-------------|
| CC0 | ✅ | ✅ | Not required |
| CC-BY | ✅ | ✅ | Required |
| CC-BY-SA | ✅ | ✅ (share alike) | Required |
| CC-BY-NC | ❌ (non-commercial) | ✅ | Required |

---

## Running Scrapers

### Management Command
```bash
# Import 3 clips from Wikimedia, trimmed to 30s
python manage.py scrape_audio --source=wikimedia --limit=3 --clip-length=30

# All sources
python manage.py scrape_audio --source=internet_archive --limit=5
python manage.py scrape_audio --source=freesound --limit=5
python manage.py scrape_audio --source=kaggle --limit=5
```

### Celery Task
```python
from backend.app.tasks import scrape_and_import
scrape_and_import.delay('wikimedia', limit=10)
```

---

## Adding New Sources

1. Create `ai_ml/scrapers/sources/newsource.py`
```python
def fetch_audio(limit=10):
    # Return list of dicts with: url, title, page_url, license, id
    return [...]
```

2. Register in `scrapers/sources/__init__.py`
```python
from . import newsource
SOURCES = {
    ...,
    'newsource': newsource,
}
```

3. Add license/API key to settings if needed

---

*Source: `ai_ml/scrapers/sources/*.py`, `ai_ml/scrapers/base.py`, `ai_ml/scrapers/downloader.py`, `backend/app/management/commands/scrape_audio.py`*