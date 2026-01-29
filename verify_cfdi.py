#!/usr/bin/env python3
"""
CFDI Verification Script for SAT (Mexico)
Uses Playwright for browser automation and 2Captcha for CAPTCHA solving.
"""

import base64
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

# Load environment variables from .env file
load_dotenv(Path(__file__).parent / ".env")


def solve_captcha_with_2captcha(image_bytes: bytes) -> str:
    """Use 2Captcha service to solve the CAPTCHA."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise ValueError("TWOCAPTCHA_API_KEY not found in environment")

    solver = TwoCaptcha(api_key)

    # Convert to base64
    base64_image = base64.standard_b64encode(image_bytes).decode("utf-8")

    result = solver.normal(base64_image)
    return result["code"]


def verify_cfdi(xml_path: str, headless: bool = False, max_retries: int = 3) -> dict:
    """
    Verify a CFDI XML file against SAT's verification service.

    Args:
        xml_path: Path to the XML file to verify
        headless: Run browser in headless mode (default: False for debugging)
        max_retries: Number of times to retry if CAPTCHA fails

    Returns:
        dict with verification results
    """
    xml_file = Path(xml_path).resolve()
    if not xml_file.exists():
        raise FileNotFoundError(f"XML file not found: {xml_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        for attempt in range(max_retries):
            try:
                # Navigate to SAT verification page
                print(f"Attempt {attempt + 1}: Loading SAT verification page...")
                page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                page.wait_for_load_state("networkidle")

                # Select XML file consultation mode
                page.get_by_role("radio", name="Consulta por archivo XML").click()
                page.wait_for_timeout(500)

                # Upload the XML file
                print("Uploading XML file...")
                with page.expect_file_chooser() as fc_info:
                    page.get_by_text("Buscar").click()
                file_chooser = fc_info.value
                file_chooser.set_files(str(xml_file))

                page.wait_for_timeout(500)

                # Get CAPTCHA image and solve it
                print("Solving CAPTCHA with 2Captcha...")
                captcha_img = page.locator("#ctl00_MainContent_ImgCaptchaXml")
                captcha_bytes = captcha_img.screenshot()

                captcha_text = solve_captcha_with_2captcha(captcha_bytes)
                print(f"CAPTCHA solution: {captcha_text}")

                # Enter CAPTCHA
                page.locator("#ctl00_MainContent_TxtCaptchaNumbersXml").fill(captcha_text)

                # Submit
                print("Submitting verification request...")
                page.get_by_role("button", name="Verificar CFDI").click()
                page.wait_for_timeout(2000)

                # Check for results or error
                page_content = page.content()

                if "CFDI válido" in page_content or "Válido" in page_content:
                    # Success - extract results
                    results = extract_results(page)
                    browser.close()
                    return results
                elif "incorrecto" in page_content.lower() or "captcha" in page_content.lower():
                    print(f"CAPTCHA incorrect, retrying... ({attempt + 1}/{max_retries})")
                    page.reload()
                    continue
                else:
                    # Check if there's an error message
                    results = extract_results(page)
                    browser.close()
                    return results

            except Exception as e:
                print(f"Error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    continue
                raise

        browser.close()
        raise Exception(f"Failed to verify CFDI after {max_retries} attempts")


def extract_results(page) -> dict:
    """Extract verification results from the page."""
    results = {
        "valid": False,
        "message": "",
        "details": {}
    }

    try:
        # Check for success message
        if page.locator("text=CFDI válido").count() > 0:
            results["valid"] = True
            results["message"] = "CFDI válido almacenado en los controles del SAT"

        # Extract table data
        tables = page.locator("table")

        # First table - XML Validation
        validation_table = tables.nth(0)
        rows = validation_table.locator("tr")

        for i in range(rows.count()):
            row = rows.nth(i)
            cells = row.locator("td")
            if cells.count() >= 2:
                key = cells.nth(0).inner_text().strip()
                value = cells.nth(1).inner_text().strip()
                if key and value:
                    results["details"][key] = value

        # Second table - Additional details
        if tables.count() > 1:
            details_table = tables.nth(1)
            rows = details_table.locator("tr")

            for i in range(rows.count()):
                row = rows.nth(i)
                cells = row.locator("td, th")
                texts = [cells.nth(j).inner_text().strip() for j in range(cells.count())]

                # Parse key-value pairs
                if len(texts) >= 2:
                    for j in range(0, len(texts) - 1, 2):
                        if texts[j] and texts[j+1] if j+1 < len(texts) else None:
                            results["details"][texts[j]] = texts[j+1]

    except Exception as e:
        print(f"Warning: Error extracting results: {e}")

    return results


def print_results(results: dict):
    """Pretty print the verification results."""
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
    for key, value in results["details"].items():
        print(f"  {key}: {value}")

    print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verify_cfdi.py <path_to_xml_file>")
        print("Example: python verify_cfdi.py /path/to/invoice.xml")
        sys.exit(1)

    xml_path = sys.argv[1]

    print(f"Verifying CFDI: {xml_path}")

    try:
        results = verify_cfdi(xml_path, headless=False)
        print_results(results)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
