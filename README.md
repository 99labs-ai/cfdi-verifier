# CFDI Verifier API

API for verifying Mexican digital tax invoices (CFDI) against SAT's official verification service.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/verify/folio` | Verify by Folio Fiscal (sync) |
| POST | `/verify/folio/async` | Verify by Folio Fiscal (async) |
| POST | `/verify/xml` | Verify by XML upload (sync) |
| POST | `/verify/xml/async` | Verify by XML upload (async) |
| GET | `/jobs/{job_id}` | Get async job status |
| GET | `/jobs` | List recent jobs |
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

# Set environment variable
export TWOCAPTCHA_API_KEY=your-api-key

# Run
python api.py
```

### Docker

```bash
# Build and run
docker-compose up --build

# Or manually
docker build -t cfdi-verifier .
docker run -p 8000:8000 -e TWOCAPTCHA_API_KEY=your-key cfdi-verifier
```

## Deploy to DigitalOcean

### Option 1: App Platform (Recommended)

1. Push code to GitHub
2. Go to DigitalOcean App Platform
3. Create new app from GitHub repo
4. Add environment variable: `TWOCAPTCHA_API_KEY`
5. Deploy

### Option 2: Droplet with Docker

```bash
# SSH into droplet
ssh root@your-droplet-ip

# Install Docker
curl -fsSL https://get.docker.com | sh

# Clone repo
git clone https://github.com/YOUR_USERNAME/cfdi-verifier.git
cd cfdi-verifier

# Create .env file
echo "TWOCAPTCHA_API_KEY=your-key" > .env

# Run
docker-compose up -d
```

## API Usage

### Verify by Folio (Recommended)

```bash
curl -X POST https://your-server.com/verify/folio \
  -H "Content-Type: application/json" \
  -d '{
    "id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
    "re": "DORA990310A30",
    "rr": "REGL960120LPA"
  }'
```

### Response

```json
{
  "valid": true,
  "message": "CFDI cancelado",
  "folio_fiscal": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
  "rfc_emisor": "DORA990310A30",
  "nombre_emisor": "ALEJANDRO DOMINGUEZ RAMIREZ",
  "rfc_receptor": "REGL960120LPA",
  "nombre_receptor": "LUIS PEDRO REYES GUZMAN",
  "fecha_expedicion": "2026-01-05T19:15:01",
  "total": "$58,000.00",
  "estado": "Cancelado",
  "estatus_cancelacion": "Cancelado con aceptación"
}
```

### With Webhook

```bash
curl -X POST https://your-server.com/verify/folio \
  -H "Content-Type: application/json" \
  -d '{
    "id": "...",
    "re": "...",
    "rr": "...",
    "webhook_url": "https://your-callback.com/webhook"
  }'
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TWOCAPTCHA_API_KEY` | Your 2Captcha API key (required) |

## Cost

- 2Captcha: ~$2.99 per 1000 verifications
