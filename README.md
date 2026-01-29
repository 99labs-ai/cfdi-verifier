# CFDI Verifier API

API for verifying Mexican digital tax invoices (CFDI) against SAT's official verification service.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/verify/folio` | Verify by Folio Fiscal (sync) |
| POST | `/verify/folio/async` | Verify by Folio Fiscal (async) |
| POST | `/verify/xml` | Verify by XML upload (sync) |
| POST | `/verify/xml/async` | Verify by XML upload (async) |
| POST | `/batch/verify` | Submit batch of CFDIs (up to 500) |
| GET | `/batch/{batch_id}` | Get batch status and results |
| GET | `/batch` | List recent batches |
| DELETE | `/batch/{batch_id}` | Cancel batch |
| GET | `/jobs/{job_id}` | Get async job status |
| GET | `/jobs` | List recent jobs |
| GET | `/queue/stats` | Get Celery queue statistics |
| GET | `/health` | Health check |
| GET | `/docs` | Swagger UI |

## Quick Start

### Local Development

```bash
# Create venv and install deps
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Start Redis (required for batch processing)
docker run -d -p 6379:6379 redis:7-alpine

# Set environment variables
export TWOCAPTCHA_API_KEY=your-api-key
export REDIS_URL=redis://localhost:6379/0

# Start API
python api.py

# Start Celery worker (in another terminal)
celery -A celery_app worker --loglevel=info --concurrency=3
```

### Docker

```bash
# Build and run (includes Redis and worker)
docker-compose up --build
```

## API Usage

### Single Verification (Sync)

```bash
curl -X POST https://your-server.com/verify/folio \
  -H "Content-Type: application/json" \
  -d '{
    "id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
    "re": "DORA990310A30",
    "rr": "REGL960120LPA"
  }'
```

### Batch Verification (200+ CFDIs)

```bash
# Submit batch
curl -X POST https://your-server.com/batch/verify \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"id": "UUID-1", "re": "RFC_EMISOR_1", "rr": "RFC_RECEPTOR_1"},
      {"id": "UUID-2", "re": "RFC_EMISOR_2", "rr": "RFC_RECEPTOR_2"},
      ...
    ],
    "webhook_url": "https://your-callback.com/webhook"
  }'

# Response
{
  "batch_id": "abc123",
  "total_items": 200,
  "status": "processing",
  "message": "Batch created. 200 items queued for verification."
}

# Check progress
curl https://your-server.com/batch/abc123

# Get results when complete
curl "https://your-server.com/batch/abc123?include_results=true"
```

### Webhook Payload

When a batch completes, the webhook receives:

```json
{
  "type": "batch_completed",
  "batch_id": "abc123",
  "total": 200,
  "completed": 195,
  "failed": 5,
  "results": [
    {"valid": true, "folio_fiscal": "...", "estado": "Vigente", ...},
    {"valid": true, "folio_fiscal": "...", "estado": "Cancelado", ...},
    ...
  ]
}
```

## Deploy to DigitalOcean

### App Platform (Recommended)

1. Push code to GitHub
2. Go to DigitalOcean App Platform
3. Create new app from GitHub repo
4. Add components:
   - **api** service (HTTP, port 8000)
   - **worker** service (run command: `celery -A celery_app worker --loglevel=info --concurrency=3`)
   - **Redis** database
5. Add environment variable: `TWOCAPTCHA_API_KEY`
6. Deploy

The `.do/app.yaml` file contains the full configuration.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TWOCAPTCHA_API_KEY` | Your 2Captcha API key (required) |
| `REDIS_URL` | Redis connection URL (required for batch) |

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│  FastAPI    │────▶│   Redis     │
└─────────────┘     │   (api)     │     │  (broker)   │
                    └─────────────┘     └──────┬──────┘
                                               │
                    ┌─────────────────────────┼─────────────────────────┐
                    │                         │                         │
               ┌────▼────┐              ┌────▼────┐              ┌────▼────┐
               │ Worker 1│              │ Worker 2│              │ Worker 3│
               │(browser)│              │(browser)│              │(browser)│
               └─────────┘              └─────────┘              └─────────┘
```

- **API**: Receives requests, queues tasks
- **Redis**: Task broker and result backend
- **Workers**: Process verifications with Playwright (3 concurrent by default)

## Cost

- 2Captcha: ~$2.99 per 1000 verifications
- DO Redis: ~$15/month (db-s-1vcpu-1gb)
- DO Services: ~$12/month each (professional-xs)

## Performance

- Single verification: ~30 seconds
- Batch of 200 with 3 workers: ~35 minutes
- Batch of 200 with 5 workers: ~20 minutes

Scale workers by increasing `instance_count` in app.yaml or `--concurrency` flag.
