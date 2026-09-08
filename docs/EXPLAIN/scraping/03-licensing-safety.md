# Licensing Safety & Ingestion Security

## License Compliance

### Allowed Licenses (Default)
```python
SCRAPER_ALLOW_LICENSES = os.getenv('SCRAPER_ALLOW_LICENSES', 'CC0,CC-BY,CC-BY-SA,CC-BY-NC').split(',')
```

| License | Commercial | Modify | Attribution | ShareAlike | EchoFlow Use |
|---------|------------|--------|-------------|------------|--------------|
| **CC0** | ✅ | ✅ | No | No | ✅ Safest |
| **CC-BY** | ✅ | ✅ | Yes | No | ✅ |
| **CC-BY-SA** | ✅ | ✅ | Yes | **Yes** | ⚠️ Derivative must be SA |
| **CC-BY-NC** | **No** | ✅ | Yes | No | ❌ Non-commercial only |

### CC-BY-SA Implication
If EchoFlow uses CC-BY-SA audio:
- Derivative work (HLS transcodes) **must be licensed CC-BY-SA**
- This applies to the **entire platform** if mixed
- **Recommendation:** Avoid CC-BY-SA for commercial platform

### CC-BY-NC Implication
- **Cannot use commercially** — EchoFlow is commercial
- Must exclude from production

---

## License Enforcement

### Management Command (Strict)
```python
# scrape_audio.py:52-58
lic_upper = str(lic_raw).upper() if lic_raw else ''
if lic_upper and lic_upper != 'UNKNOWN' and not any(a in lic_upper for a in allowed):
    self.stdout.write(self.style.WARNING(f'Skipping: license "{lic_raw}" not allowed'))
    continue
```

### Celery Task (Lenient)
```python
# tasks.py:710-796 — NO license check
# Assumes pre-filtered or manual review
```

### Gap
**Inconsistent enforcement** — management command filters, Celery task doesn't.

### Fix (2026-09-07)
The Celery `scrape_and_import` task now applies the same family-based license
check as the management command. Both paths call `license_allows_commercial()`,
which honors the `SCRAPER_ALLOW_NC` env var. CC-BY-NC and REMARC-NC items are
skipped unless the operator has set `SCRAPER_ALLOW_NC=True`. This closes the
gap documented above.

### License Family Normalization
Every source connector returns license strings in different vocabularies
(`by`, `CC BY 3.0`, `http://creativecommons.org/licenses/by-nc/3.0/`,
`RemArc-NC`, `Pixabay`). The substring matcher in the old code would silently
drop most of these. `ai_ml/scrapers/base.py::normalize_license()` maps them
to canonical families (`CC-BY`, `CC-BY-NC`, `CC-BY-NC-ND`, `CC0`, `PD`,
`REMARC-NC`, `PIXABAY`, `OTHER`, `UNKNOWN`).

### NC + SA Runtime Gates
Two boolean columns on `AudioClip` (`is_noncommercial`, `requires_share_alike`)
are populated at import time via `license_features()`. Feed and suggestion
queries exclude these clips unless:
- `is_noncommercial=True` AND the operator has flipped `SCRAPER_ALLOW_NC=True`
  (item still excluded; gate is data-only); OR
- `requires_share_alike=True` AND the operator has called
  `/clips/{id}/approve-moderation/` to flip `moderation_approved=True` after
  manual review.

The gate is feed-level, not import-level, so operators can flip
`SCRAPER_ALLOW_NC` without losing ingested NC content. SA items require
per-item operator approval.

---

## Provenance Tracking

### Stored Metadata
```python
# AudioClip fields populated by uploader.save_clip()
source_name = 'wikimedia' | 'internet_archive' | 'freesound' | 'kaggle'
source_url = 'https://commons.wikimedia.org/wiki/File:...'
license = 'CC0' | 'CC-BY' | 'CC-BY-SA' | 'CC-BY-NC' | 'unknown'
attribution_text = source_url  # For display
imported_via_scraper = True
original_source_id = 'File:Example.ogg' | 'archive_org_id' | 'freesound_123'
```

### Use Cases
- **Legal audit:** Prove license compliance
- **Attribution display:** Show in UI
- **Takedown requests:** Identify source
- **License changes:** Re-evaluate catalog

---

## Robots.txt Compliance

### Implementation (`base.py:RobotsTxtChecker`)
```python
class RobotsTxtChecker:
    def allowed(self, url, user_agent=None):
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = self.parsers.get(base) or RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            return True  # Permissive default
        return rp.can_fetch(user_agent or settings.SCRAPER_USER_AGENT, url)
```

### Behavior
- Caches parsers per domain
- On read failure → **permissive** (allows)
- Checks before every download

### Tested Sources
| Source | robots.txt | Compliance |
|--------|------------|------------|
| Wikimedia Commons | Allows bots with UA | ✅ |
| Internet Archive | Allows archive.org bots | ✅ |
| Freesound | API-based (no scrape) | N/A |
| Kaggle | Local files | N/A |

---

## Rate Limiting

### Implementation (`base.py:RateLimiter`)
```python
class RateLimiter:
    def __init__(self, max_per_min=30):
        self.max_per_min = max_per_min
        self.min_interval = 60.0 / float(max_per_min)
        self.last_access = {}
    
    def wait(self, url):
        host = urlparse(url).netloc
        last = self.last_access.get(host)
        if last:
            elapsed = time.time() - last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self.last_access[host] = time.time()
```

### Default: 30 requests/minute per host
```python
SCRAPER_MAX_DOWNLOADS_PER_MIN = int(os.getenv('SCRAPER_MAX_DOWNLOADS_PER_MIN', '30'))
```

### Per-Source Limits
| Source | Effective Rate | Notes |
|--------|---------------|-------|
| Wikimedia | 30/min | API-based, fast |
| Internet Archive | 30/min | Metadata + download |
| Freesound | API rate limit | Token-based |
| Kaggle | Local | No limit |

---

## Download Safety

### Size Limit
```python
MAX_DOWNLOAD_BYTES = 50_000_000  # 50 MB
```

### Content-Type Validation
```python
content_type = resp.headers.get('Content-Type', '')
if 'audio' not in content_type and not url.lower().endswith(audio_extensions):
    raise RuntimeError(f"URL does not appear to be audio (Content-Type: {content_type})")
```

### Streaming Download (No Memory Buffer)
```python
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
total = 0
for chunk in resp.iter_content(chunk_size=8192):
    total += len(chunk)
    if total > MAX_DOWNLOAD_BYTES:
        os.unlink(tmp.name)
        raise RuntimeError("Downloaded file exceeds maximum allowed size")
    tmp.write(chunk)
```

---

## Ingestion Security

### File Type Validation (Upload + Scraper)
```python
# Serializer (user upload)
ALLOWED_EXT = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.webm', '.opus'}
# Scraper normalizer output
target_format='mp3'  # Always MP3
```

### ⚠️ Missing: Magic Byte Validation
```python
# NOT IMPLEMENTED — Security Gap
import magic
def validate_audio_file(file):
    mime = magic.from_buffer(file.read(2048), mime=True)
    if not mime.startswith('audio/'):
        raise ValidationError("Invalid audio file")
```

### Executable Upload Risk
- User uploads `malware.exe` renamed to `song.mp3`
- Passes extension check
- `librosa.load()` → audioread fallback → executes?
- **Mitigation:** `normalize_to_wav()` uses FFmpeg which validates container

### FFmpeg as Validator
```python
# normalize_to_wav() runs:
ffmpeg -y -i input -ac 1 -ar 22050 -f wav output.wav
# If input not valid audio → FFmpeg fails → task fails safely
```

---

## Provenance & Attribution

### Stored Fields
```python
AudioClip(
    source_name='wikimedia',
    source_url='https://commons.wikimedia.org/wiki/File:Example.ogg',
    license='CC0',
    attribution_text='https://commons.wikimedia.org/wiki/File:Example.ogg',
    imported_via_scraper=True,
    original_source_id='Example.ogg'
)
```

### Attribution Display (Not Implemented in UI)
```python
# Frontend should show:
"Source: Wikimedia Commons (CC0)"
# Link to source_url
```

---

## Legal Risk Mitigation

| Risk | Mitigation |
|------|------------|
| License misidentification | Manual review for UNKNOWN, allowlist only |
| Copyrighted content slipped through | DMCA takedown process (not implemented) |
| CC-BY-SA viral license | Avoid in production, flag for review |
| Geoblocked content | Respect robots.txt, but not geo-aware |
| Moral rights (EU) | Attribution stored, not displayed |

---

## Audit Trail

### Database Fields
```python
AudioClip(
    imported_via_scraper=True,
    source_name='internet_archive',
    source_url='https://archive.org/details/example',
    license='CC-BY',
    attribution_text='https://archive.org/details/example',
    original_source_id='example_identifier',
    created_at=DateTimeField(auto_now_add=True)
)
```

### Query for Audit
```sql
SELECT id, title, source_name, license, source_url, created_at
FROM app_audioclip
WHERE imported_via_scraper = true
ORDER BY created_at DESC;
```

---

## Recommendations

### Immediate
1. **Add magic byte validation** to `AudioUploadSerializer.validate_original_file`
2. **Unify license enforcement** — add check to Celery task
3. **Display attribution** in frontend for scraper clips
4. **Add DMCA takedown endpoint** `/api/content/takedown/`

### Future
1. **Content ID / fingerprinting** (Audible Magic, YouTube Content ID)
2. **Automated license verification** (cross-reference with source APIs)
3. **Geographic licensing** (region-specific rights)
4. **Royalty tracking** for commercial licenses

---

*Source: `ai_ml/scrapers/`, `backend/app/management/commands/scrape_audio.py`, `backend/app/tasks.py:710-796`, `backend/EchoFlow/settings.py`*