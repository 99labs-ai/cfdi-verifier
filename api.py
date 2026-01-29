#!/usr/bin/env python3
"""
CFDI Verifier API Service
FastAPI service with async job processing for verifying Mexican tax invoices.
Supports both XML upload and Folio Fiscal data input methods.
"""

import asyncio
import base64
import hashlib
import logging
import os
import tempfile
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List

import httpx
from celery import group, chord
from celery.result import AsyncResult, GroupResult
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from pydantic import BaseModel
from playwright.async_api import async_playwright
from sqlalchemy.orm import Session
from twocaptcha import TwoCaptcha

from celery_app import celery_app
from database import get_db, init_db, SessionLocal
from models import Verification, Batch, VerificationStatus, VerificationMethod
from tasks import verify_folio_task, verify_xml_task, batch_complete_callback

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
    version="3.0.0",
)


@app.on_event("startup")
async def startup_event():
    """Initialize database on startup."""
    logger.info("Initializing database...")
    init_db()
    logger.info("Database initialized")


# In-memory job storage (kept for backwards compatibility, DB is primary)
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


class BatchItem(BaseModel):
    id: str  # Folio Fiscal
    re: str  # RFC Emisor
    rr: str  # RFC Receptor


class BatchRequest(BaseModel):
    items: List[BatchItem]
    webhook_url: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "items": [
                    {"id": "9FD4B473-1EE0-42E2-9D29-5DAEC8057A18", "re": "DORA990310A30", "rr": "REGL960120LPA"},
                    {"id": "ANOTHER-UUID-HERE", "re": "RFC123456789", "rr": "RFC987654321"}
                ],
                "webhook_url": "https://your-server.com/webhook"
            }
        }
    }


class BatchResponse(BaseModel):
    batch_id: str
    total_items: int
    status: str
    created_at: str
    message: str


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: str
    total: int
    completed: int
    failed: int
    pending: int
    results: Optional[List[dict]] = None


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
async def verify_by_folio_sync(request: VerifyFolioRequest, db: Session = Depends(get_db)):
    """
    Verify CFDI using Folio Fiscal data (synchronous - waits for result).

    Required fields:
    - **id**: Folio Fiscal (UUID)
    - **re**: RFC Emisor
    - **rr**: RFC Receptor

    Returns the verification result directly (takes ~20-40 seconds).
    """
    job_id = str(uuid.uuid4())

    # Create DB record
    db_verification = Verification(
        job_id=job_id,
        method=VerificationMethod.FOLIO,
        folio_fiscal=request.id,
        rfc_emisor=request.re,
        rfc_receptor=request.rr,
        webhook_url=request.webhook_url,
        status=VerificationStatus.PROCESSING,
        started_at=datetime.utcnow()
    )
    db.add(db_verification)
    db.commit()

    try:
        result = await verify_by_folio(
            request.id,
            request.re,
            request.rr,
            request.tt,
            request.max_retries
        )

        # Update DB record
        db_verification.status = VerificationStatus.COMPLETED
        db_verification.valid = result.get("valid", False)
        db_verification.sat_response = result
        db_verification.completed_at = datetime.utcnow()
        db.commit()

        # Send webhook if configured
        if request.webhook_url:
            await send_webhook(request.webhook_url, job_id, {
                "status": "completed",
                "created_at": db_verification.created_at.isoformat(),
                "completed_at": db_verification.completed_at.isoformat(),
                "result": result,
                "error": None
            })
            db_verification.webhook_sent = True
            db.commit()

        return VerifyFolioResponse(**result)

    except Exception as e:
        # Update DB record with error
        db_verification.status = VerificationStatus.FAILED
        db_verification.error_message = str(e)
        db_verification.completed_at = datetime.utcnow()
        db.commit()

        if request.webhook_url:
            await send_webhook(request.webhook_url, job_id, {
                "status": "failed",
                "created_at": db_verification.created_at.isoformat(),
                "completed_at": db_verification.completed_at.isoformat(),
                "result": None,
                "error": str(e)
            })
            db_verification.webhook_sent = True
            db.commit()

        raise HTTPException(status_code=500, detail=str(e))


@app.post("/verify/folio/async", response_model=JobResponse, tags=["Verification"])
async def verify_by_folio_async(request: VerifyFolioRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Verify CFDI using Folio Fiscal data (async - returns job_id immediately).

    Use this for batch processing or when you can't hold the connection.
    Poll /jobs/{job_id} for results or provide webhook_url.
    """
    job_id = str(uuid.uuid4())
    created_at = datetime.utcnow()

    # Create DB record
    db_verification = Verification(
        job_id=job_id,
        method=VerificationMethod.FOLIO,
        folio_fiscal=request.id,
        rfc_emisor=request.re,
        rfc_receptor=request.rr,
        webhook_url=request.webhook_url,
        status=VerificationStatus.PENDING
    )
    db.add(db_verification)
    db.commit()

    # Keep in-memory for backwards compatibility
    jobs[job_id] = {
        "status": JobStatus.PENDING,
        "created_at": created_at.isoformat(),
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
        created_at=created_at.isoformat(),
        message="Verification job created. Poll /jobs/{job_id} for results."
    )


@app.post("/verify/xml", response_model=VerifyFolioResponse, tags=["Verification"])
async def verify_by_xml_sync(request: VerifyXMLRequest, db: Session = Depends(get_db)):
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

    job_id = str(uuid.uuid4())
    xml_hash = hashlib.sha256(xml_content.encode()).hexdigest()

    # Create DB record
    db_verification = Verification(
        job_id=job_id,
        method=VerificationMethod.XML,
        xml_hash=xml_hash,
        webhook_url=request.webhook_url,
        status=VerificationStatus.PROCESSING,
        started_at=datetime.utcnow()
    )
    db.add(db_verification)
    db.commit()

    try:
        result = await verify_by_xml(xml_content, request.max_retries)

        # Update DB record
        db_verification.status = VerificationStatus.COMPLETED
        db_verification.valid = result.get("valid", False)
        db_verification.sat_response = result
        db_verification.folio_fiscal = result.get("folio_fiscal")
        db_verification.rfc_emisor = result.get("rfc_emisor")
        db_verification.rfc_receptor = result.get("rfc_receptor")
        db_verification.completed_at = datetime.utcnow()
        db.commit()

        if request.webhook_url:
            await send_webhook(request.webhook_url, job_id, {
                "status": "completed",
                "created_at": db_verification.created_at.isoformat(),
                "completed_at": db_verification.completed_at.isoformat(),
                "result": result,
                "error": None
            })
            db_verification.webhook_sent = True
            db.commit()

        return VerifyFolioResponse(**result)

    except Exception as e:
        db_verification.status = VerificationStatus.FAILED
        db_verification.error_message = str(e)
        db_verification.completed_at = datetime.utcnow()
        db.commit()

        if request.webhook_url:
            await send_webhook(request.webhook_url, job_id, {
                "status": "failed",
                "created_at": db_verification.created_at.isoformat(),
                "completed_at": db_verification.completed_at.isoformat(),
                "result": None,
                "error": str(e)
            })
            db_verification.webhook_sent = True
            db.commit()

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


# ---------- Batch Endpoints (Celery) ----------

# In-memory batch tracking (use Redis in production for persistence)
batches: dict[str, dict] = {}


@app.post("/batch/verify", response_model=BatchResponse, tags=["Batch"])
async def create_batch_verification(request: BatchRequest, db: Session = Depends(get_db)):
    """
    Submit a batch of CFDIs for verification.

    - Accepts up to 500 items per batch
    - Items are processed in parallel (3 concurrent workers)
    - Use /batch/{batch_id} to check progress
    - Optionally provide webhook_url for completion notification
    """
    if len(request.items) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 items per batch")

    if len(request.items) == 0:
        raise HTTPException(status_code=400, detail="At least 1 item required")

    batch_id = str(uuid.uuid4())
    created_at = datetime.utcnow()

    # Create batch record in DB
    db_batch = Batch(
        batch_id=batch_id,
        total_items=len(request.items),
        webhook_url=request.webhook_url,
        status=VerificationStatus.PROCESSING
    )
    db.add(db_batch)
    db.commit()
    db.refresh(db_batch)

    # Create verification records for each item
    for i, item in enumerate(request.items):
        db_verification = Verification(
            job_id=str(uuid.uuid4()),
            method=VerificationMethod.FOLIO,
            folio_fiscal=item.id,
            rfc_emisor=item.re,
            rfc_receptor=item.rr,
            status=VerificationStatus.PENDING,
            batch_id=db_batch.id,
            batch_index=i
        )
        db.add(db_verification)
    db.commit()

    # Create Celery tasks for each item
    tasks = []
    for i, item in enumerate(request.items):
        task = verify_folio_task.s(
            folio_fiscal=item.id,
            rfc_emisor=item.re,
            rfc_receptor=item.rr,
            webhook_url=request.webhook_url,
            batch_id=batch_id,
            item_index=i
        )
        tasks.append(task)

    # Use chord: run all tasks in parallel, then call callback when all complete
    if request.webhook_url:
        job = chord(tasks)(batch_complete_callback.s(batch_id=batch_id, webhook_url=request.webhook_url))
    else:
        job = group(tasks).apply_async()

    # Update batch with Celery group ID
    db_batch.celery_group_id = job.id
    db.commit()

    # Store in memory for backwards compatibility
    batches[batch_id] = {
        "group_id": job.id,
        "total": len(request.items),
        "created_at": created_at.isoformat(),
        "webhook_url": request.webhook_url,
        "items": [{"id": item.id, "re": item.re, "rr": item.rr} for item in request.items]
    }

    logger.info(f"Created batch {batch_id} with {len(request.items)} items")

    return BatchResponse(
        batch_id=batch_id,
        total_items=len(request.items),
        status="processing",
        created_at=created_at.isoformat(),
        message=f"Batch created. {len(request.items)} items queued for verification. Poll /batch/{batch_id} for status."
    )


@app.get("/batch/{batch_id}", response_model=BatchStatusResponse, tags=["Batch"])
async def get_batch_status(batch_id: str, include_results: bool = False):
    """
    Get the status of a batch verification job.

    - Set include_results=true to get individual results (only when completed)
    """
    if batch_id not in batches:
        raise HTTPException(status_code=404, detail="Batch not found")

    batch = batches[batch_id]
    group_result = GroupResult.restore(batch["group_id"], app=celery_app)

    if group_result is None:
        # Try as AsyncResult (for chord)
        async_result = AsyncResult(batch["group_id"], app=celery_app)
        if async_result.ready():
            results = async_result.result
            if isinstance(results, dict) and "results" in results:
                # Chord callback result
                all_results = results["results"]
                completed = sum(1 for r in all_results if r and r.get("valid") is not None)
                failed = len(all_results) - completed

                return BatchStatusResponse(
                    batch_id=batch_id,
                    status="completed",
                    total=batch["total"],
                    completed=completed,
                    failed=failed,
                    pending=0,
                    results=all_results if include_results else None
                )

        return BatchStatusResponse(
            batch_id=batch_id,
            status="processing",
            total=batch["total"],
            completed=0,
            failed=0,
            pending=batch["total"],
            results=None
        )

    # Count completed/failed/pending
    completed = 0
    failed = 0
    results_list = []

    for result in group_result.results:
        if result.ready():
            if result.successful():
                completed += 1
                if include_results:
                    results_list.append(result.result)
            else:
                failed += 1
                if include_results:
                    results_list.append({"error": str(result.result)})
        else:
            if include_results:
                results_list.append(None)

    pending = batch["total"] - completed - failed

    if pending == 0:
        status = "completed"
    elif completed + failed > 0:
        status = "processing"
    else:
        status = "pending"

    return BatchStatusResponse(
        batch_id=batch_id,
        status=status,
        total=batch["total"],
        completed=completed,
        failed=failed,
        pending=pending,
        results=results_list if include_results and status == "completed" else None
    )


@app.get("/batch", tags=["Batch"])
async def list_batches(limit: int = 10):
    """List recent batches."""
    sorted_batches = sorted(
        batches.items(),
        key=lambda x: x[1]["created_at"],
        reverse=True
    )[:limit]

    result = []
    for batch_id, batch in sorted_batches:
        # Get quick status
        group_result = GroupResult.restore(batch["group_id"], app=celery_app)
        if group_result:
            completed = sum(1 for r in group_result.results if r.ready() and r.successful())
            pending = batch["total"] - completed
            status = "completed" if pending == 0 else "processing"
        else:
            status = "processing"
            completed = 0

        result.append({
            "batch_id": batch_id,
            "total": batch["total"],
            "completed": completed,
            "status": status,
            "created_at": batch["created_at"],
        })

    return result


@app.delete("/batch/{batch_id}", tags=["Batch"])
async def cancel_batch(batch_id: str):
    """Cancel a batch and revoke pending tasks."""
    if batch_id not in batches:
        raise HTTPException(status_code=404, detail="Batch not found")

    batch = batches[batch_id]
    group_result = GroupResult.restore(batch["group_id"], app=celery_app)

    if group_result:
        group_result.revoke(terminate=True)

    del batches[batch_id]
    return {"message": f"Batch {batch_id} cancelled"}


@app.get("/queue/stats", tags=["System"])
async def queue_stats():
    """Get Celery queue statistics."""
    inspect = celery_app.control.inspect()

    try:
        active = inspect.active() or {}
        reserved = inspect.reserved() or {}
        stats = inspect.stats() or {}

        total_active = sum(len(tasks) for tasks in active.values())
        total_reserved = sum(len(tasks) for tasks in reserved.values())

        return {
            "workers": list(stats.keys()),
            "active_tasks": total_active,
            "reserved_tasks": total_reserved,
            "batches_in_memory": len(batches)
        }
    except Exception as e:
        return {"error": str(e), "message": "Celery workers may not be running"}


@app.get("/history", tags=["History"])
async def get_verification_history(
    db: Session = Depends(get_db),
    limit: int = 50,
    offset: int = 0,
    folio_fiscal: Optional[str] = None,
    rfc_emisor: Optional[str] = None,
    rfc_receptor: Optional[str] = None,
    status: Optional[str] = None,
    valid: Optional[bool] = None
):
    """
    Query verification history from database.

    Supports filtering by:
    - folio_fiscal: Exact match
    - rfc_emisor: Exact match
    - rfc_receptor: Exact match
    - status: pending, processing, completed, failed
    - valid: true/false
    """
    query = db.query(Verification)

    if folio_fiscal:
        query = query.filter(Verification.folio_fiscal == folio_fiscal)
    if rfc_emisor:
        query = query.filter(Verification.rfc_emisor == rfc_emisor)
    if rfc_receptor:
        query = query.filter(Verification.rfc_receptor == rfc_receptor)
    if status:
        query = query.filter(Verification.status == status)
    if valid is not None:
        query = query.filter(Verification.valid == valid)

    total = query.count()
    verifications = query.order_by(Verification.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "results": [
            {
                "job_id": v.job_id,
                "folio_fiscal": v.folio_fiscal,
                "rfc_emisor": v.rfc_emisor,
                "rfc_receptor": v.rfc_receptor,
                "method": v.method.value if v.method else None,
                "status": v.status.value if v.status else None,
                "valid": v.valid,
                "sat_response": v.sat_response,
                "error_message": v.error_message,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "completed_at": v.completed_at.isoformat() if v.completed_at else None,
            }
            for v in verifications
        ]
    }


@app.get("/history/{job_id}", tags=["History"])
async def get_verification_by_job_id(job_id: str, db: Session = Depends(get_db)):
    """Get a specific verification by job_id."""
    verification = db.query(Verification).filter(Verification.job_id == job_id).first()

    if not verification:
        raise HTTPException(status_code=404, detail="Verification not found")

    return {
        "job_id": verification.job_id,
        "folio_fiscal": verification.folio_fiscal,
        "rfc_emisor": verification.rfc_emisor,
        "rfc_receptor": verification.rfc_receptor,
        "method": verification.method.value if verification.method else None,
        "status": verification.status.value if verification.status else None,
        "valid": verification.valid,
        "sat_response": verification.sat_response,
        "error_message": verification.error_message,
        "webhook_url": verification.webhook_url,
        "webhook_sent": verification.webhook_sent,
        "batch_id": verification.batch.batch_id if verification.batch else None,
        "created_at": verification.created_at.isoformat() if verification.created_at else None,
        "started_at": verification.started_at.isoformat() if verification.started_at else None,
        "completed_at": verification.completed_at.isoformat() if verification.completed_at else None,
    }


@app.get("/stats", tags=["System"])
async def get_stats(db: Session = Depends(get_db)):
    """Get verification statistics."""
    total = db.query(Verification).count()
    completed = db.query(Verification).filter(Verification.status == VerificationStatus.COMPLETED).count()
    failed = db.query(Verification).filter(Verification.status == VerificationStatus.FAILED).count()
    pending = db.query(Verification).filter(Verification.status == VerificationStatus.PENDING).count()
    processing = db.query(Verification).filter(Verification.status == VerificationStatus.PROCESSING).count()

    valid_count = db.query(Verification).filter(Verification.valid == True).count()
    invalid_count = db.query(Verification).filter(Verification.valid == False).count()

    total_batches = db.query(Batch).count()

    return {
        "verifications": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "processing": processing,
        },
        "results": {
            "valid": valid_count,
            "invalid": invalid_count,
        },
        "batches": {
            "total": total_batches,
        }
    }


@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "cfdi-verifier", "version": "3.0.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
