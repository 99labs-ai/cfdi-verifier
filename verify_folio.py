#!/usr/bin/env python3
"""
CFDI Verification by Folio Fiscal
Verifies using UUID, RFC emisor, and RFC receptor (no file upload needed).
"""

import base64
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

load_dotenv(Path(__file__).parent / ".env")


def solve_captcha(image_bytes: bytes) -> str:
    """Solve CAPTCHA using 2Captcha."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise ValueError("TWOCAPTCHA_API_KEY not set")

    solver = TwoCaptcha(api_key)
    base64_image = base64.standard_b64encode(image_bytes).decode("utf-8")
    result = solver.normal(base64_image)
    return result["code"]


def verify_by_folio(
    folio_fiscal: str,
    rfc_emisor: str,
    rfc_receptor: str,
    total: str = None,
    headless: bool = True,
    max_retries: int = 3
) -> dict:
    """
    Verify CFDI by Folio Fiscal data.

    Args:
        folio_fiscal: UUID of the CFDI (id)
        rfc_emisor: RFC of the issuer (re)
        rfc_receptor: RFC of the receiver (rr)
        total: Total amount (tt) - optional but recommended
        headless: Run browser headless
        max_retries: Max CAPTCHA retries

    Returns:
        dict with verification results
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        for attempt in range(max_retries):
            try:
                print(f"Attempt {attempt + 1}: Loading SAT verification page...")
                page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                page.wait_for_load_state("networkidle")

                # Already on "Consulta por Folio Fiscal" by default
                page.wait_for_timeout(500)

                # Fill in the form fields
                print("Filling form data...")
                page.locator("#ctl00_MainContent_TxtUUID").fill(folio_fiscal)
                page.locator("#ctl00_MainContent_TxtRfcEmisor").fill(rfc_emisor)
                page.locator("#ctl00_MainContent_TxtRfcReceptor").fill(rfc_receptor)

                # Solve CAPTCHA
                print("Solving CAPTCHA...")
                captcha_img = page.locator("#ctl00_MainContent_ImgCaptcha")
                captcha_bytes = captcha_img.screenshot()
                captcha_text = solve_captcha(captcha_bytes)
                print(f"CAPTCHA solution: {captcha_text}")

                # Enter CAPTCHA
                page.locator("#ctl00_MainContent_TxtCaptchaNumbers").fill(captcha_text)

                # Submit
                print("Submitting...")
                page.get_by_role("button", name="Verificar CFDI").click()
                page.wait_for_timeout(2000)

                # Check results
                page_content = page.content()

                if "CFDI válido" in page_content or "Válido" in page_content:
                    results = extract_results(page)
                    browser.close()
                    return results
                elif "incorrecto" in page_content.lower():
                    print(f"CAPTCHA incorrect, retrying...")
                    page.reload()
                    continue
                else:
                    results = extract_results(page)
                    browser.close()
                    return results

            except Exception as e:
                print(f"Error: {e}")
                if attempt < max_retries - 1:
                    continue
                raise

        browser.close()
        raise Exception(f"Failed after {max_retries} attempts")


def extract_results(page) -> dict:
    """Extract verification results."""
    results = {
        "valid": False,
        "message": "",
        "folio_fiscal": "",
        "rfc_emisor": "",
        "nombre_emisor": "",
        "rfc_receptor": "",
        "nombre_receptor": "",
        "total": "",
        "estado": "",
        "fecha_expedicion": "",
        "fecha_certificacion": "",
        "sello_cfdi": "",
        "sello_sat": "",
    }

    try:
        page_content = page.content()

        # Check for valid CFDI indicators
        if "CFDI válido" in page_content or "Vigente" in page_content:
            results["valid"] = True
            results["message"] = "CFDI válido almacenado en los controles del SAT"

        # Extract validation table
        for label, key in [
            ("Folio Fiscal", "folio_fiscal"),
            ("Rfc emisor", "rfc_emisor"),
            ("Rfc receptor", "rfc_receptor"),
            ("Sello CFDI", "sello_cfdi"),
            ("Sello del timbre fiscal", "sello_sat"),
        ]:
            try:
                row = page.locator(f"tr:has(td:has-text('{label}'))")
                if row.count() > 0:
                    cells = row.first.locator("td")
                    if cells.count() >= 2:
                        results[key] = cells.nth(1).inner_text().strip()
            except Exception:
                pass

        # Extract emisor/receptor names
        try:
            table = page.locator("table").nth(1)
            rows = table.locator("tr")
            for i in range(rows.count()):
                cells = rows.nth(i).locator("td")
                if cells.count() >= 4:
                    texts = [cells.nth(j).inner_text() for j in range(4)]
                    if texts[0] and not texts[0].startswith("RFC"):
                        results["rfc_emisor"] = texts[0].strip()
                        results["nombre_emisor"] = texts[1].strip()
                        results["rfc_receptor"] = texts[2].strip()
                        results["nombre_receptor"] = texts[3].strip()
                        break
        except Exception:
            pass

        # Extract total and estado
        try:
            total_row = page.locator("tr:has(td:has-text('$'))")
            if total_row.count() > 0:
                cells = total_row.first.locator("td")
                if cells.count() >= 4:
                    results["total"] = cells.nth(0).inner_text().strip()
                    results["estado"] = cells.nth(2).inner_text().strip()
        except Exception:
            pass

    except Exception:
        pass

    return results


def print_results(results: dict):
    """Pretty print results."""
    print("\n" + "=" * 60)
    print("CFDI VERIFICATION RESULTS")
    print("=" * 60)

    if results["valid"]:
        print("✅ Status: VALID")
    else:
        print("❌ Status: INVALID or ERROR")

    if results["message"]:
        print(f"Message: {results['message']}")

    print("\nDetails:")
    print("-" * 60)
    for key, value in results.items():
        if value and key not in ("valid", "message"):
            print(f"  {key}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python verify_folio.py <folio_fiscal> <rfc_emisor> <rfc_receptor> <total>")
        print("Example: python verify_folio.py 9FD4B473-1EE0-42E2-9D29-5DAEC8057A18 DORA990310A30 REGL960120LPA 58000.00")
        sys.exit(1)

    folio = sys.argv[1]
    rfc_e = sys.argv[2]
    rfc_r = sys.argv[3]
    total = sys.argv[4] if len(sys.argv) > 4 else None

    print(f"Verifying CFDI:")
    print(f"  Folio: {folio}")
    print(f"  RFC Emisor: {rfc_e}")
    print(f"  RFC Receptor: {rfc_r}")
    print(f"  Total: {total}")

    try:
        results = verify_by_folio(folio, rfc_e, rfc_r, total, headless=False)
        print_results(results)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
