#!/usr/bin/env python3
"""
CFDI Verifier API Service
FastAPI service with async job processing for verifying Mexican tax invoices.
Supports both XML upload and Folio Fiscal data input methods.
"""

import asyncio
import base64
import logging
import os
import tempfile
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from playwright.async_api import async_playwright
from twocaptcha import TwoCaptcha

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("cfdi-verifier")

# Load environment variables
load_dotenv(Path(__file__).parent / ".env")

app = FastAPI(
    title="CFDI Verifier API",
    description="API for verifying Mexican digital tax invoices (CFDI) against SAT",
    version="2.0.0",
)

# In-memory job storage (use Redis/DB in production)
jobs: dict[str, dict] = {}


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class VerifyXMLRequest(BaseModel):
    xml_content: Optional[str] = None
    xml_base64: Optional[str] = None
    webhook_url: Optional[str] = None
    max_retries: int = 3

    model_config = {
        "json_schema_extra": {
            "example": {
                "xml_content": "<?xml version='1.0'?><cfdi:Comprobante>...</cfdi:Comprobante>",
                "webhook_url": "https://your-server.com/webhook",
                "max_retries": 3
            }
        }
    }


class VerifyFolioRequest(BaseModel):
    id: str  # Folio Fiscal (UUID)
    re: str  # RFC Emisor
    rr: str  # RFC Receptor
    tt: Optional[str] = None  # Total amount (not used by SAT form, kept for compatibility)
    webhook_url: Optional[str] = None
    max_retries: int = 3

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18",
                "re": "DORA990310A30",
                "rr": "REGL960120LPA",
                "webhook_url": "https://your-server.com/webhook",
                "max_retries": 3
            }
        }
    }


class JobResponse(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    message: str


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    created_at: str
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None


# ---------- CAPTCHA Solving ----------

async def solve_captcha(image_bytes: bytes) -> str:
    """Solve CAPTCHA using 2Captcha service."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise ValueError("TWOCAPTCHA_API_KEY not set")

    solver = TwoCaptcha(api_key)
    base64_image = base64.standard_b64encode(image_bytes).decode("utf-8")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: solver.normal(base64_image))
    return result["code"]


# ---------- Verification by Folio Fiscal ----------

async def verify_by_folio(
    folio_fiscal: str,
    rfc_emisor: str,
    rfc_receptor: str,
    total: Optional[str] = None,
    max_retries: int = 3
) -> dict:
    """Verify CFDI by Folio Fiscal (UUID, RFC emisor, RFC receptor)."""
    logger.info(f"Starting verification for folio: {folio_fiscal}")
    logger.info(f"RFC Emisor: {rfc_emisor}, RFC Receptor: {rfc_receptor}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for attempt in range(max_retries):
            try:
                logger.info(f"Attempt {attempt + 1}/{max_retries}")
                await page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(500)

                # Fill form (already on Folio Fiscal tab by default)
                logger.info("Filling form fields...")
                await page.locator("#ctl00_MainContent_TxtUUID").fill(folio_fiscal)
                await page.locator("#ctl00_MainContent_TxtRfcEmisor").fill(rfc_emisor)
                await page.locator("#ctl00_MainContent_TxtRfcReceptor").fill(rfc_receptor)

                # Solve CAPTCHA
                logger.info("Solving CAPTCHA...")
                captcha_img = page.locator("#ctl00_MainContent_ImgCaptcha")
                captcha_bytes = await captcha_img.screenshot()
                captcha_text = await solve_captcha(captcha_bytes)
                logger.info(f"CAPTCHA solution: {captcha_text}")

                await page.locator("#ctl00_MainContent_TxtCaptchaNumbers").fill(captcha_text)
                logger.info("Submitting form...")
                await page.get_by_role("button", name="Verificar CFDI").click()
                await page.wait_for_timeout(2000)

                page_content = await page.content()
                logger.info(f"Page contains 'Vigente': {'Vigente' in page_content}")
                logger.info(f"Page contains 'Cancelado': {'Cancelado' in page_content}")

                if "Vigente" in page_content or "Cancelado" in page_content:
                    logger.info("CFDI found, extracting results...")
                    results = await extract_results(page)
                    await browser.close()
                    return results
                elif "incorrecto" in page_content.lower():
                    logger.warning(f"CAPTCHA incorrect, retrying...")
                    if attempt < max_retries - 1:
                        await page.reload()
                        continue
                else:
                    logger.info("Extracting results (unknown status)...")
                    results = await extract_results(page)
                    await browser.close()
                    return results

            except Exception as e:
                if attempt < max_retries - 1:
                    continue
                raise

        await browser.close()
        raise Exception(f"Failed after {max_retries} attempts")


# ---------- Verification by XML ----------

async def verify_by_xml(xml_content: str, max_retries: int = 3) -> dict:
    """Verify CFDI by uploading XML file."""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(xml_content)
        xml_path = f.name

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            for attempt in range(max_retries):
                try:
                    await page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                    await page.wait_for_load_state("networkidle")

                    await page.get_by_role("radio", name="Consulta por archivo XML").click()
                    await page.wait_for_timeout(500)

                    async with page.expect_file_chooser() as fc_info:
                        await page.get_by_text("Buscar").click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(xml_path)
                    await page.wait_for_timeout(500)

                    captcha_img = page.locator("#ctl00_MainContent_ImgCaptchaXml")
                    captcha_bytes = await captcha_img.screenshot()
                    captcha_text = await solve_captcha(captcha_bytes)

                    await page.locator("#ctl00_MainContent_TxtCaptchaNumbersXml").fill(captcha_text)
                    await page.get_by_role("button", name="Verificar CFDI").click()
                    await page.wait_for_timeout(2000)

                    page_content = await page.content()

                    if "Vigente" in page_content or "CFDI válido" in page_content:
                        results = await extract_results(page)
                        await browser.close()
                        return results
                    elif "incorrecto" in page_content.lower():
                        if attempt < max_retries - 1:
                            await page.reload()
                            continue
                    else:
                        results = await extract_results(page)
                        await browser.close()
                        return results

                except Exception as e:
                    if attempt < max_retries - 1:
                        continue
                    raise

            await browser.close()
            raise Exception(f"Failed after {max_retries} attempts")

    finally:
        Path(xml_path).unlink(missing_ok=True)


async def extract_results(page) -> dict:
    """Extract verification results from SAT page."""

    results = {
        "valid": False,
        "message": "",
        "folio_fiscal": "",
        "rfc_emisor": "",
        "nombre_emisor": "",
        "rfc_receptor": "",
        "nombre_receptor": "",
        "fecha_expedicion": "",
        "fecha_certificacion": "",
        "pac_certificador": "",
        "total": "",
        "efecto": "",
        "estado": "",
        "estatus_cancelacion": "",
        "fecha_cancelacion": "",
    }

    try:
        page_content = await page.content()
        logger.info("Extracting results from page...")

        # Check validity - Vigente or Cancelado both mean the CFDI exists/existed
        if "Vigente" in page_content:
            results["valid"] = True
            results["message"] = "CFDI vigente - válido y activo"
        elif "Cancelado" in page_content:
            results["valid"] = True  # It was valid, just cancelled
            results["message"] = "CFDI cancelado"
        else:
            results["message"] = "CFDI no encontrado o inválido"

        # Get all tables
        tables = page.locator("table")
        table_count = await tables.count()
        logger.info(f"Found {table_count} tables")

        # Parse each table row by row
        all_rows = page.locator("table tr")
        row_count = await all_rows.count()
        logger.info(f"Found {row_count} total rows")

        for i in range(row_count):
            row = all_rows.nth(i)
            cells = row.locator("td")
            cell_count = await cells.count()

            if cell_count == 0:
                continue

            # Get all cell texts
            cell_texts = []
            for j in range(cell_count):
                text = (await cells.nth(j).inner_text()).strip()
                cell_texts.append(text)

            logger.info(f"Row {i}: {cell_texts}")

            # Match patterns based on cell content
            if cell_count >= 4:
                # Emisor/Receptor row (RFC, Nombre, RFC, Nombre)
                if cell_texts[0] and len(cell_texts[0]) >= 12 and len(cell_texts[0]) <= 13:
                    if not cell_texts[0].startswith("RFC") and not results["rfc_emisor"]:
                        results["rfc_emisor"] = cell_texts[0]
                        results["nombre_emisor"] = cell_texts[1]
                        results["rfc_receptor"] = cell_texts[2]
                        results["nombre_receptor"] = cell_texts[3]
                        logger.info(f"Found emisor/receptor: {cell_texts}")

                # Folio/Fechas row
                if "-" in cell_texts[0] and "T" in cell_texts[1]:
                    results["folio_fiscal"] = cell_texts[0]
                    results["fecha_expedicion"] = cell_texts[1]
                    results["fecha_certificacion"] = cell_texts[2] if cell_count > 2 else ""
                    results["pac_certificador"] = cell_texts[3] if cell_count > 3 else ""
                    logger.info(f"Found folio/fechas: {cell_texts}")

            if cell_count >= 3:
                # Total/Efecto/Estado row
                if cell_texts[0].startswith("$"):
                    results["total"] = cell_texts[0]
                    results["efecto"] = cell_texts[1]
                    results["estado"] = cell_texts[2]
                    logger.info(f"Found total/estado: {cell_texts}")

            if cell_count >= 2:
                # Cancelacion row
                if "Cancelado" in cell_texts[0] and "T" in cell_texts[1]:
                    results["estatus_cancelacion"] = cell_texts[0]
                    results["fecha_cancelacion"] = cell_texts[1]
                    logger.info(f"Found cancelacion: {cell_texts}")

        logger.info(f"Final results: {results}")

    except Exception as e:
        logger.error(f"Error extracting results: {e}")

    return results


# ---------- Webhook ----------

async def send_webhook(webhook_url: str, job_id: str, job_data: dict):
    """Send job result to webhook URL."""
    try:
        payload = {
            "job_id": job_id,
            "status": job_data["status"],
            "created_at": job_data["created_at"],
            "completed_at": job_data["completed_at"],
            "result": job_data["result"],
            "error": job_data["error"],
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
    except Exception as e:
        print(f"Webhook failed for job {job_id}: {e}")


# ---------- Background Job Processing ----------

async def process_folio_job(job_id: str, folio: str, rfc_e: str, rfc_r: str, total: str, webhook_url: Optional[str], max_retries: int):
    """Background task for Folio verification."""
    jobs[job_id]["status"] = JobStatus.PROCESSING

    try:
        result = await verify_by_folio(folio, rfc_e, rfc_r, total, max_retries)
        jobs[job_id]["status"] = JobStatus.COMPLETED
        jobs[job_id]["result"] = result
        jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        jobs[job_id]["status"] = JobStatus.FAILED
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()

    # Send webhook if configured
    if webhook_url:
        await send_webhook(webhook_url, job_id, jobs[job_id])


async def process_xml_job(job_id: str, xml_content: str, webhook_url: Optional[str], max_retries: int):
    """Background task for XML verification."""
    jobs[job_id]["status"] = JobStatus.PROCESSING

    try:
        result = await verify_by_xml(xml_content, max_retries)
        jobs[job_id]["status"] = JobStatus.COMPLETED
        jobs[job_id]["result"] = result
        jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        jobs[job_id]["status"] = JobStatus.FAILED
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["completed_at"] = datetime.utcnow().isoformat()

    # Send webhook if configured
    if webhook_url:
        await send_webhook(webhook_url, job_id, jobs[job_id])


# ---------- API Endpoints ----------

class VerifyFolioResponse(BaseModel):
    valid: bool
    message: str
    folio_fiscal: str = ""
    rfc_emisor: str = ""
    nombre_emisor: str = ""
    rfc_receptor: str = ""
    nombre_receptor: str = ""
    fecha_expedicion: str = ""
    fecha_certificacion: str = ""
    pac_certificador: str = ""
    total: str = ""
    efecto: str = ""
    estado: str = ""
    estatus_cancelacion: str = ""
    fecha_cancelacion: str = ""
    error: Optional[str] = None


@app.post("/verify/folio", response_model=VerifyFolioResponse, tags=["Verification"])
async def verify_by_folio_sync(request: VerifyFolioRequest):
    """
    Verify CFDI using Folio Fiscal data (synchronous - waits for result).

    Required fields:
    - **id**: Folio Fiscal (UUID)
    - **re**: RFC Emisor
    - **rr**: RFC Receptor
    - **tt**: Total amount

    Returns the verification result directly (takes ~20-40 seconds).
    """
    try:
        result = await verify_by_folio(
            request.id,
            request.re,
            request.rr,
            request.tt,
            request.max_retries
        )

        # Send webhook if configured
        if request.webhook_url:
            await send_webhook(request.webhook_url, "sync", {
                "status": "completed",
                "created_at": datetime.utcnow().isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "result": result,
                "error": None
            })

        return VerifyFolioResponse(**result)

    except Exception as e:
        if request.webhook_url:
            await send_webhook(request.webhook_url, "sync", {
                "status": "failed",
                "created_at": datetime.utcnow().isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "result": None,
                "error": str(e)
            })
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/verify/folio/async", response_model=JobResponse, tags=["Verification"])
async def verify_by_folio_async(request: VerifyFolioRequest, background_tasks: BackgroundTasks):
    """
    Verify CFDI using Folio Fiscal data (async - returns job_id immediately).

    Use this for batch processing or when you can't hold the connection.
    Poll /jobs/{job_id} for results or provide webhook_url.
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": JobStatus.PENDING,
        "created_at": datetime.utcnow().isoformat(),
        "method": "folio",
        "result": None,
        "error": None,
        "completed_at": None,
    }

    background_tasks.add_task(
        process_folio_job,
        job_id,
        request.id,
        request.re,
        request.rr,
        request.tt,
        request.webhook_url,
        request.max_retries
    )

    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=jobs[job_id]["created_at"],
        message="Verification job created. Poll /jobs/{job_id} for results."
    )


@app.post("/verify/xml", response_model=VerifyFolioResponse, tags=["Verification"])
async def verify_by_xml_sync(request: VerifyXMLRequest):
    """
    Verify CFDI by uploading XML content (synchronous - waits for result).

    Provide either:
    - **xml_content**: Raw XML string
    - **xml_base64**: Base64 encoded XML

    Returns the verification result directly (takes ~20-40 seconds).
    """
    xml_content = request.xml_content
    if not xml_content and request.xml_base64:
        xml_content = base64.b64decode(request.xml_base64).decode("utf-8")

    if not xml_content:
        raise HTTPException(status_code=400, detail="xml_content or xml_base64 required")

    try:
        result = await verify_by_xml(xml_content, request.max_retries)

        if request.webhook_url:
            await send_webhook(request.webhook_url, "sync", {
                "status": "completed",
                "created_at": datetime.utcnow().isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "result": result,
                "error": None
            })

        return VerifyFolioResponse(**result)

    except Exception as e:
        if request.webhook_url:
            await send_webhook(request.webhook_url, "sync", {
                "status": "failed",
                "created_at": datetime.utcnow().isoformat(),
                "completed_at": datetime.utcnow().isoformat(),
                "result": None,
                "error": str(e)
            })
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/verify/xml/async", response_model=JobResponse, tags=["Verification"])
async def verify_by_xml_async(request: VerifyXMLRequest, background_tasks: BackgroundTasks):
    """
    Verify CFDI by uploading XML content (async - returns job_id immediately).

    Use this for batch processing or when you can't hold the connection.
    """
    xml_content = request.xml_content
    if not xml_content and request.xml_base64:
        xml_content = base64.b64decode(request.xml_base64).decode("utf-8")

    if not xml_content:
        raise HTTPException(status_code=400, detail="xml_content or xml_base64 required")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": JobStatus.PENDING,
        "created_at": datetime.utcnow().isoformat(),
        "method": "xml",
        "result": None,
        "error": None,
        "completed_at": None,
    }

    background_tasks.add_task(
        process_xml_job,
        job_id,
        xml_content,
        request.webhook_url,
        request.max_retries
    )

    return JobResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        created_at=jobs[job_id]["created_at"],
        message="Verification job created. Poll /jobs/{job_id} for results."
    )


# Keep old endpoint for backwards compatibility
@app.post("/verify", response_model=VerifyFolioResponse, tags=["Verification"], deprecated=True)
async def verify_legacy(request: VerifyXMLRequest):
    """Legacy endpoint - use /verify/xml instead."""
    return await verify_by_xml_sync(request)


@app.get("/jobs/{job_id}", response_model=JobResult, tags=["Jobs"])
async def get_job_status(job_id: str):
    """Get the status and result of a verification job."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = jobs[job_id]
    return JobResult(
        job_id=job_id,
        status=job["status"],
        created_at=job["created_at"],
        completed_at=job["completed_at"],
        result=job["result"],
        error=job["error"],
    )


@app.get("/jobs", tags=["Jobs"])
async def list_jobs(limit: int = 10):
    """List recent jobs."""
    sorted_jobs = sorted(
        jobs.items(),
        key=lambda x: x[1]["created_at"],
        reverse=True
    )[:limit]

    return [
        {
            "job_id": job_id,
            "status": job["status"],
            "method": job.get("method", "xml"),
            "created_at": job["created_at"],
        }
        for job_id, job in sorted_jobs
    ]


@app.delete("/jobs/{job_id}", tags=["Jobs"])
async def delete_job(job_id: str):
    """Delete a job from memory."""
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    del jobs[job_id]
    return {"message": "Job deleted"}


@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "cfdi-verifier", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
