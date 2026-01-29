# CFDI Verifier API

API service for verifying Mexican digital tax invoices (CFDI) against SAT's official verification service.

## Features

- **Single Verification** - Verify individual CFDIs by Folio Fiscal or XML upload
- **Batch Processing** - Submit up to 500 CFDIs in a single request
- **Async Jobs** - Non-blocking verification with job polling
- **Webhooks** - Get notified when verifications complete
- **History** - Query past verifications with filters
- **Queue Management** - Celery + Redis for controlled concurrency

## Architecture

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Client  │────▶│ FastAPI  │────▶│  Redis   │────▶│ Workers  │
└──────────┘     └──────────┘     └──────────┘     └──────────┘
                      │                                  │
                      ▼                                  ▼
                ┌──────────┐                      ┌──────────┐
                │ Postgres │◀─────────────────────│ SAT API  │
                └──────────┘                      └──────────┘
```

- **FastAPI** - REST API server
- **Redis** - Task queue broker
- **PostgreSQL** - Request/result persistence
- **Celery Workers** - Browser automation with Playwright
- **2Captcha** - CAPTCHA solving service

## API Endpoints

### Verification

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/verify/folio` | Verify by Folio Fiscal (sync, ~30s) |
| POST | `/verify/folio/async` | Verify by Folio Fiscal (async) |
| POST | `/verify/xml` | Verify by XML upload (sync, ~30s) |
| POST | `/verify/xml/async` | Verify by XML upload (async) |

### Batch Processing

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/batch/verify` | Submit batch (up to 500 items) |
| GET | `/batch/{batch_id}` | Get batch status and progress |
| GET | `/batch` | List recent batches |
| DELETE | `/batch/{batch_id}` | Cancel batch |

### Jobs & History

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/jobs/{job_id}` | Get async job status |
| GET | `/jobs` | List recent jobs |
| GET | `/history` | Query verification history |
| GET | `/history/{job_id}` | Get verification details |

### System

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/stats` | Verification statistics |
| GET | `/queue/stats` | Celery queue statistics |
| GET | `/docs` | Swagger UI |

## Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- 2Captcha API key

### Local Development

```bash
# Clone repository
git clone https://github.com/99labs-ai/cfdi-verifier.git
cd cfdi-verifier

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Set environment variables
export TWOCAPTCHA_API_KEY=your-api-key
export REDIS_URL=redis://localhost:6379/0
export DATABASE_URL=postgresql://localhost/cfdi_verifier

# Start Redis (required)
docker run -d -p 6379:6379 redis:7-alpine

# Start PostgreSQL (required)
docker run -d -p 5432:5432 -e POSTGRES_DB=cfdi_verifier -e POSTGRES_PASSWORD=postgres postgres:15-alpine

# Start API server
python api.py

# Start Celery worker (separate terminal)
celery -A celery_app worker --loglevel=info --concurrency=3
```

### Docker Compose

```bash
# Set your API key
echo "TWOCAPTCHA_API_KEY=your-api-key" > .env

# Start all services
docker-compose up --build
```

## Usage Examples

### Single Verification

```bash
curl -X POST https://your-server.com/verify/folio \
  -H "Content-Type: application/json" \
  -d '{
    "id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
    "re": "DORA990310A30",
    "rr": "REGL960120LPA"
  }'
```

**Response:**
```json
{
  "valid": true,
  "message": "CFDI vigente - válido y activo",
  "folio_fiscal": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
  "rfc_emisor": "DORA990310A30",
  "nombre_emisor": "ALEJANDRO DOMINGUEZ RAMIREZ",
  "rfc_receptor": "REGL960120LPA",
  "nombre_receptor": "LUIS PEDRO REYES GUZMAN",
  "fecha_expedicion": "2026-01-05T19:15:01",
  "total": "$58,000.00",
  "estado": "Vigente"
}
```

### Batch Verification

```bash
# Submit batch
curl -X POST https://your-server.com/batch/verify \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"id": "UUID-1", "re": "RFC_EMISOR_1", "rr": "RFC_RECEPTOR_1"},
      {"id": "UUID-2", "re": "RFC_EMISOR_2", "rr": "RFC_RECEPTOR_2"}
    ],
    "webhook_url": "https://your-server.com/webhook"
  }'

# Response
{
  "batch_id": "abc123-def456",
  "total_items": 200,
  "status": "processing",
  "message": "Batch created. 200 items queued for verification."
}

# Check progress
curl https://your-server.com/batch/abc123-def456

# Get results when complete
curl "https://your-server.com/batch/abc123-def456?include_results=true"
```

### Async with Webhook

```bash
curl -X POST https://your-server.com/verify/folio/async \
  -H "Content-Type: application/json" \
  -d '{
    "id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
    "re": "DORA990310A30",
    "rr": "REGL960120LPA",
    "webhook_url": "https://your-server.com/webhook"
  }'
```

**Webhook Payload:**
```json
{
  "job_id": "abc123",
  "status": "completed",
  "created_at": "2026-01-28T12:00:00",
  "completed_at": "2026-01-28T12:00:30",
  "result": {
    "valid": true,
    "folio_fiscal": "...",
    "estado": "Vigente"
  }
}
```

### Query History

```bash
# Get all verifications for an RFC
curl "https://your-server.com/history?rfc_emisor=DORA990310A30&limit=50"

# Get only valid CFDIs
curl "https://your-server.com/history?valid=true"

# Get failed verifications
curl "https://your-server.com/history?status=failed"
```

## Deployment

### DigitalOcean App Platform

1. Fork/push repo to GitHub
2. Create new app in DO App Platform
3. Add components:
   - **api** - Web service, port 8000
   - **worker** - Worker, command: `celery -A celery_app worker --loglevel=info --concurrency=3`
   - **PostgreSQL** - Dev database
4. Set environment variables:
   - `TWOCAPTCHA_API_KEY` (secret)
   - `REDIS_URL` (your Redis Cloud URL)
   - `DATABASE_URL` (auto-injected from DO PostgreSQL)
5. Deploy

See `.do/app.yaml` for full configuration.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TWOCAPTCHA_API_KEY` | Yes | 2Captcha API key for CAPTCHA solving |
| `REDIS_URL` | Yes | Redis connection URL |
| `DATABASE_URL` | Yes | PostgreSQL connection URL |

## Performance

| Scenario | Time |
|----------|------|
| Single verification | ~30 seconds |
| Batch of 100 (3 workers) | ~17 minutes |
| Batch of 200 (3 workers) | ~35 minutes |
| Batch of 200 (5 workers) | ~20 minutes |

Scale workers by adjusting `--concurrency` flag or adding more worker instances.

## Costs

| Service | Cost |
|---------|------|
| 2Captcha | ~$2.99 per 1000 verifications |
| DO PostgreSQL (dev) | Free |
| DO App Platform (basic) | ~$12/month per service |
| Redis Cloud (free tier) | Free up to 30MB |

## Project Structure

```
cfdi-verifier/
├── api.py              # FastAPI application
├── celery_app.py       # Celery configuration
├── tasks.py            # Celery tasks (browser automation)
├── database.py         # SQLAlchemy setup
├── models.py           # Database models
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container image
├── docker-compose.yml  # Local development setup
├── .do/
│   └── app.yaml        # DigitalOcean App Platform config
└── docs/
    └── proxy-support.md  # Proxy feature scope (not implemented)
```

## License

MIT
