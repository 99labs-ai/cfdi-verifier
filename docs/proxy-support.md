# Proxy Support Feature Scope

## Overview
Add optional rotating proxy support to prevent IP blocking when processing high-volume batches.

## Status
**Not implemented** - Scope only

## When to Implement
- Increased CAPTCHA failures
- Timeouts or connection refused from SAT
- IP gets blocked
- Processing >500 verifications/day consistently

## Environment Variables
```
PROXY_ENABLED=false
PROXY_URL=http://user:pass@host:port
PROXY_ROTATION=per_request|per_batch|sticky
```

## Files to Modify

### 1. `celery_app.py` (~2 lines)
- Add proxy config vars

### 2. `tasks.py` (~15 lines)
- Update `verify_folio_task` browser launch with proxy option
- Update `verify_xml_task` browser launch with proxy option

### 3. `api.py` (~15 lines)
- Update `verify_by_folio` browser launch with proxy option
- Update `verify_by_xml` browser launch with proxy option

### 4. New: `proxy.py` (~40 lines)
- `get_proxy()` - Returns proxy URL based on rotation strategy
- `report_proxy_failure()` - Track failed proxies
- Support for proxy list rotation (round-robin or random)

## API Additions

### GET `/proxy/stats`
```json
{
  "enabled": true,
  "rotation": "per_request",
  "total_proxies": 5,
  "failed_proxies": 1,
  "requests_today": 150
}
```

### POST `/proxy/test`
Test proxy connectivity before using in production.

## Database (optional)
```sql
-- Track proxy performance
CREATE TABLE proxy_usage (
  id SERIAL PRIMARY KEY,
  proxy_host VARCHAR(100),
  success_count INT DEFAULT 0,
  failure_count INT DEFAULT 0,
  last_used_at TIMESTAMP,
  blocked_until TIMESTAMP
);
```

## Implementation Example

```python
# proxy.py
import os
from itertools import cycle

PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL")
PROXY_LIST = os.getenv("PROXY_LIST", "").split(",")  # For multiple proxies

_proxy_cycle = cycle(PROXY_LIST) if PROXY_LIST[0] else None

def get_proxy() -> dict | None:
    if not PROXY_ENABLED:
        return None

    if PROXY_URL:
        return {"server": PROXY_URL}

    if _proxy_cycle:
        return {"server": next(_proxy_cycle)}

    return None

# Usage in tasks.py
from proxy import get_proxy

browser = p.chromium.launch(
    headless=True,
    proxy=get_proxy()
)
```

## Proxy Providers

| Provider | Type | Price | Notes |
|----------|------|-------|-------|
| Bright Data | Residential | ~$15/GB | Best quality, most reliable |
| Smartproxy | Residential | ~$10/GB | Good balance |
| IPRoyal | Residential | ~$5/GB | Budget option |
| Webshare | Datacenter | ~$2/GB | Cheapest, may get blocked faster |

## Estimated Effort
- Implementation: ~2 hours
- Testing: ~1 hour

## Dependencies
None (Playwright has built-in proxy support)

## Risks
- Proxy costs add up with high volume
- Some proxies may be already blocked by SAT
- Residential proxies are slower than direct connection
