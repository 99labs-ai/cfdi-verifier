"""
Celery tasks for CFDI verification.
"""
import asyncio
import base64
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

from celery_app import celery_app
from database import SessionLocal
from models import Verification, Batch, VerificationStatus

logger = logging.getLogger("cfdi-tasks")
logging.basicConfig(level=logging.INFO)


def update_verification_status(batch_id: str, item_index: int, status: VerificationStatus,
                                result: dict = None, error: str = None):
    """Update verification record in database."""
    db = SessionLocal()
    try:
        # Find the batch first
        db_batch = db.query(Batch).filter(Batch.batch_id == batch_id).first()
        if db_batch:
            # Find the verification by batch_id and index
            db_verification = db.query(Verification).filter(
                Verification.batch_id == db_batch.id,
                Verification.batch_index == item_index
            ).first()

            if db_verification:
                db_verification.status = status
                if status == VerificationStatus.PROCESSING:
                    db_verification.started_at = datetime.utcnow()
                elif status in [VerificationStatus.COMPLETED, VerificationStatus.FAILED]:
                    db_verification.completed_at = datetime.utcnow()

                if result:
                    db_verification.valid = result.get("valid", False)
                    db_verification.sat_response = result
                if error:
                    db_verification.error_message = error

                # Update batch counts
                if status == VerificationStatus.COMPLETED:
                    db_batch.completed_count += 1
                elif status == VerificationStatus.FAILED:
                    db_batch.failed_count += 1

                # Check if batch is complete
                if db_batch.completed_count + db_batch.failed_count >= db_batch.total_items:
                    db_batch.status = VerificationStatus.COMPLETED
                    db_batch.completed_at = datetime.utcnow()

                db.commit()
                logger.info(f"Updated verification for batch {batch_id}, item {item_index}: {status}")
    except Exception as e:
        logger.error(f"DB error updating verification: {e}")
        db.rollback()
    finally:
        db.close()


def solve_captcha_sync(image_bytes: bytes) -> str:
    """Solve CAPTCHA using 2Captcha service (sync version for Celery)."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise ValueError("TWOCAPTCHA_API_KEY not set")

    solver = TwoCaptcha(api_key)
    base64_image = base64.standard_b64encode(image_bytes).decode("utf-8")
    result = solver.normal(base64_image)
    return result["code"]


def extract_results_sync(page) -> dict:
    """Extract verification results from SAT page (sync version)."""
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
        page_content = page.content()

        if "Vigente" in page_content:
            results["valid"] = True
            results["message"] = "CFDI vigente - válido y activo"
        elif "Cancelado" in page_content:
            results["valid"] = True
            results["message"] = "CFDI cancelado"
        else:
            results["message"] = "CFDI no encontrado o inválido"

        all_rows = page.locator("table tr")
        row_count = all_rows.count()

        for i in range(row_count):
            row = all_rows.nth(i)
            cells = row.locator("td")
            cell_count = cells.count()

            if cell_count == 0:
                continue

            cell_texts = []
            for j in range(cell_count):
                text = cells.nth(j).inner_text().strip()
                cell_texts.append(text)

            if cell_count >= 4:
                if cell_texts[0] and len(cell_texts[0]) >= 12 and len(cell_texts[0]) <= 13:
                    if not cell_texts[0].startswith("RFC") and not results["rfc_emisor"]:
                        results["rfc_emisor"] = cell_texts[0]
                        results["nombre_emisor"] = cell_texts[1]
                        results["rfc_receptor"] = cell_texts[2]
                        results["nombre_receptor"] = cell_texts[3]

                if "-" in cell_texts[0] and "T" in cell_texts[1]:
                    results["folio_fiscal"] = cell_texts[0]
                    results["fecha_expedicion"] = cell_texts[1]
                    results["fecha_certificacion"] = cell_texts[2] if cell_count > 2 else ""
                    results["pac_certificador"] = cell_texts[3] if cell_count > 3 else ""

            if cell_count >= 3:
                if cell_texts[0].startswith("$"):
                    results["total"] = cell_texts[0]
                    results["efecto"] = cell_texts[1]
                    results["estado"] = cell_texts[2]

            if cell_count >= 2:
                if "Cancelado" in cell_texts[0] and "T" in cell_texts[1]:
                    results["estatus_cancelacion"] = cell_texts[0]
                    results["fecha_cancelacion"] = cell_texts[1]

    except Exception as e:
        logger.error(f"Error extracting results: {e}")

    return results


@celery_app.task(bind=True, max_retries=3)
def verify_folio_task(self, folio_fiscal: str, rfc_emisor: str, rfc_receptor: str,
                       webhook_url: str = None, batch_id: str = None, item_index: int = None):
    """
    Celery task to verify CFDI by Folio Fiscal.
    Uses sync Playwright since Celery workers run in separate processes.
    """
    logger.info(f"Starting verification for folio: {folio_fiscal}")

    # Update DB status to processing
    if batch_id is not None and item_index is not None:
        update_verification_status(batch_id, item_index, VerificationStatus.PROCESSING)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        max_captcha_retries = 3
        for attempt in range(max_captcha_retries):
            try:
                logger.info(f"Attempt {attempt + 1}/{max_captcha_retries}")
                page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(500)

                # Fill form
                page.locator("#ctl00_MainContent_TxtUUID").fill(folio_fiscal)
                page.locator("#ctl00_MainContent_TxtRfcEmisor").fill(rfc_emisor)
                page.locator("#ctl00_MainContent_TxtRfcReceptor").fill(rfc_receptor)

                # Solve CAPTCHA
                captcha_img = page.locator("#ctl00_MainContent_ImgCaptcha")
                captcha_bytes = captcha_img.screenshot()
                captcha_text = solve_captcha_sync(captcha_bytes)
                logger.info(f"CAPTCHA solution: {captcha_text}")

                page.locator("#ctl00_MainContent_TxtCaptchaNumbers").fill(captcha_text)
                page.get_by_role("button", name="Verificar CFDI").click()
                page.wait_for_timeout(2000)

                page_content = page.content()

                if "Vigente" in page_content or "Cancelado" in page_content:
                    results = extract_results_sync(page)
                    browser.close()

                    # Update DB with success
                    if batch_id is not None and item_index is not None:
                        update_verification_status(batch_id, item_index, VerificationStatus.COMPLETED, result=results)

                    # Send webhook if configured
                    if webhook_url:
                        send_webhook_sync(webhook_url, {
                            "type": "item_completed" if batch_id else "completed",
                            "batch_id": batch_id,
                            "item_index": item_index,
                            "folio_fiscal": folio_fiscal,
                            "result": results
                        })

                    return results

                elif "incorrecto" in page_content.lower():
                    logger.warning("CAPTCHA incorrect, retrying...")
                    if attempt < max_captcha_retries - 1:
                        page.reload()
                        continue
                else:
                    results = extract_results_sync(page)
                    browser.close()

                    # Update DB with results
                    if batch_id is not None and item_index is not None:
                        update_verification_status(batch_id, item_index, VerificationStatus.COMPLETED, result=results)

                    return results

            except Exception as e:
                logger.error(f"Error on attempt {attempt + 1}: {e}")
                if attempt < max_captcha_retries - 1:
                    page.reload()
                    continue

                # Update DB with failure
                if batch_id is not None and item_index is not None:
                    update_verification_status(batch_id, item_index, VerificationStatus.FAILED, error=str(e))

                browser.close()
                raise self.retry(exc=e, countdown=5)

        # Update DB with failure after all retries
        if batch_id is not None and item_index is not None:
            update_verification_status(batch_id, item_index, VerificationStatus.FAILED, error="Failed after max CAPTCHA attempts")

        browser.close()
        raise Exception(f"Failed after {max_captcha_retries} CAPTCHA attempts")


@celery_app.task(bind=True, max_retries=3)
def verify_xml_task(self, xml_content: str, webhook_url: str = None,
                    batch_id: str = None, item_index: int = None):
    """Celery task to verify CFDI by XML content."""
    logger.info("Starting XML verification")

    # Update DB status to processing
    if batch_id is not None and item_index is not None:
        update_verification_status(batch_id, item_index, VerificationStatus.PROCESSING)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(xml_content)
        xml_path = f.name

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            max_captcha_retries = 3
            for attempt in range(max_captcha_retries):
                try:
                    page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                    page.wait_for_load_state("networkidle")

                    page.get_by_role("radio", name="Consulta por archivo XML").click()
                    page.wait_for_timeout(500)

                    with page.expect_file_chooser() as fc_info:
                        page.get_by_text("Buscar").click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(xml_path)
                    page.wait_for_timeout(500)

                    captcha_img = page.locator("#ctl00_MainContent_ImgCaptchaXml")
                    captcha_bytes = captcha_img.screenshot()
                    captcha_text = solve_captcha_sync(captcha_bytes)

                    page.locator("#ctl00_MainContent_TxtCaptchaNumbersXml").fill(captcha_text)
                    page.get_by_role("button", name="Verificar CFDI").click()
                    page.wait_for_timeout(2000)

                    page_content = page.content()

                    if "Vigente" in page_content or "Cancelado" in page_content:
                        results = extract_results_sync(page)
                        browser.close()

                        # Update DB with success
                        if batch_id is not None and item_index is not None:
                            update_verification_status(batch_id, item_index, VerificationStatus.COMPLETED, result=results)

                        if webhook_url:
                            send_webhook_sync(webhook_url, {
                                "type": "item_completed" if batch_id else "completed",
                                "batch_id": batch_id,
                                "item_index": item_index,
                                "result": results
                            })

                        return results

                    elif "incorrecto" in page_content.lower():
                        if attempt < max_captcha_retries - 1:
                            page.reload()
                            continue
                    else:
                        results = extract_results_sync(page)
                        browser.close()

                        # Update DB with results
                        if batch_id is not None and item_index is not None:
                            update_verification_status(batch_id, item_index, VerificationStatus.COMPLETED, result=results)

                        return results

                except Exception as e:
                    logger.error(f"Error on attempt {attempt + 1}: {e}")
                    if attempt < max_captcha_retries - 1:
                        page.reload()
                        continue

                    # Update DB with failure
                    if batch_id is not None and item_index is not None:
                        update_verification_status(batch_id, item_index, VerificationStatus.FAILED, error=str(e))

                    browser.close()
                    raise self.retry(exc=e, countdown=5)

            # Update DB with failure after all retries
            if batch_id is not None and item_index is not None:
                update_verification_status(batch_id, item_index, VerificationStatus.FAILED, error="Failed after max CAPTCHA attempts")

            browser.close()
            raise Exception(f"Failed after {max_captcha_retries} attempts")

    finally:
        Path(xml_path).unlink(missing_ok=True)


@celery_app.task
def batch_complete_callback(results: list, batch_id: str, webhook_url: str = None):
    """Called when all items in a batch are complete."""
    logger.info(f"Batch {batch_id} complete with {len(results)} results")

    # Update batch webhook_sent status in DB
    db = SessionLocal()
    try:
        db_batch = db.query(Batch).filter(Batch.batch_id == batch_id).first()
        if db_batch:
            db_batch.status = VerificationStatus.COMPLETED
            db_batch.completed_at = datetime.utcnow()

            if webhook_url:
                db_batch.webhook_sent = True

            db.commit()
    except Exception as e:
        logger.error(f"DB error updating batch: {e}")
    finally:
        db.close()

    if webhook_url:
        completed = sum(1 for r in results if r and r.get("valid") is not None)
        failed = len(results) - completed

        send_webhook_sync(webhook_url, {
            "type": "batch_completed",
            "batch_id": batch_id,
            "total": len(results),
            "completed": completed,
            "failed": failed,
            "results": results
        })

    return {"batch_id": batch_id, "total": len(results), "results": results}


def send_webhook_sync(webhook_url: str, payload: dict):
    """Send webhook notification (sync version)."""
    try:
        import requests
        response = requests.post(webhook_url, json=payload, timeout=30)
        response.raise_for_status()
        logger.info(f"Webhook sent successfully to {webhook_url}")
    except Exception as e:
        logger.error(f"Webhook failed: {e}")
