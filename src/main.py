"""
CFDI Verifier Apify Actor
Verifies Mexican digital tax invoices (CFDI) against SAT's official verification service.
"""

import base64
import os
import tempfile
from pathlib import Path

import httpx
from apify import Actor
from playwright.async_api import async_playwright
from twocaptcha import TwoCaptcha


async def solve_captcha_with_2captcha(image_bytes: bytes) -> str:
    """Use 2Captcha service to solve the CAPTCHA."""
    api_key = os.getenv("TWOCAPTCHA_API_KEY")
    if not api_key:
        raise ValueError("TWOCAPTCHA_API_KEY environment variable not set")

    solver = TwoCaptcha(api_key)
    base64_image = base64.standard_b64encode(image_bytes).decode("utf-8")
    result = solver.normal(base64_image)
    return result["code"]


async def verify_cfdi(xml_content: str, headless: bool = True, max_retries: int = 3) -> dict:
    """
    Verify a CFDI XML against SAT's verification service.

    Args:
        xml_content: The XML content as a string
        headless: Run browser in headless mode
        max_retries: Number of times to retry if CAPTCHA fails

    Returns:
        dict with verification results
    """
    # Write XML to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".xml", delete=False) as f:
        f.write(xml_content)
        xml_path = f.name

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            page = await browser.new_page()

            for attempt in range(max_retries):
                try:
                    Actor.log.info(f"Attempt {attempt + 1}: Loading SAT verification page...")
                    await page.goto("https://verificacfdi.facturaelectronica.sat.gob.mx/")
                    await page.wait_for_load_state("networkidle")

                    # Select XML file consultation mode
                    await page.get_by_role("radio", name="Consulta por archivo XML").click()
                    await page.wait_for_timeout(500)

                    # Upload the XML file
                    Actor.log.info("Uploading XML file...")
                    async with page.expect_file_chooser() as fc_info:
                        await page.get_by_text("Buscar").click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(xml_path)

                    await page.wait_for_timeout(500)

                    # Get CAPTCHA image and solve it
                    Actor.log.info("Solving CAPTCHA with 2Captcha...")
                    captcha_img = page.locator("#ctl00_MainContent_ImgCaptchaXml")
                    captcha_bytes = await captcha_img.screenshot()

                    captcha_text = await solve_captcha_with_2captcha(captcha_bytes)
                    Actor.log.info(f"CAPTCHA solution: {captcha_text}")

                    # Enter CAPTCHA
                    await page.locator("#ctl00_MainContent_TxtCaptchaNumbersXml").fill(captcha_text)

                    # Submit
                    Actor.log.info("Submitting verification request...")
                    await page.get_by_role("button", name="Verificar CFDI").click()
                    await page.wait_for_timeout(2000)

                    # Check for results or error
                    page_content = await page.content()

                    if "CFDI válido" in page_content or "Válido" in page_content:
                        results = await extract_results(page)
                        await browser.close()
                        return results
                    elif "incorrecto" in page_content.lower():
                        Actor.log.warning(f"CAPTCHA incorrect, retrying... ({attempt + 1}/{max_retries})")
                        await page.reload()
                        continue
                    else:
                        results = await extract_results(page)
                        await browser.close()
                        return results

                except Exception as e:
                    Actor.log.error(f"Error on attempt {attempt + 1}: {e}")
                    if attempt < max_retries - 1:
                        continue
                    raise

            await browser.close()
            raise Exception(f"Failed to verify CFDI after {max_retries} attempts")

    finally:
        # Clean up temp file
        Path(xml_path).unlink(missing_ok=True)


async def extract_results(page) -> dict:
    """Extract verification results from the page with proper parsing."""
    results = {
        "valid": False,
        "message": "",
        "folio_fiscal": "",
        "rfc_emisor": "",
        "nombre_emisor": "",
        "rfc_receptor": "",
        "nombre_receptor": "",
        "rfc_terceros": "",
        "nombre_terceros": "",
        "fecha_expedicion": "",
        "fecha_certificacion": "",
        "pac_certificador": "",
        "total": "",
        "efecto": "",
        "estado": "",
        "estatus_cancelacion": "",
        "sello_cfdi": "",
        "sello_sat": "",
    }

    try:
        # Check for success message
        if await page.locator("text=CFDI válido").count() > 0:
            results["valid"] = True
            results["message"] = "CFDI válido almacenado en los controles del SAT"

        # Extract from first validation table
        validation_rows = [
            ("Folio Fiscal", "folio_fiscal"),
            ("Rfc emisor", "rfc_emisor"),
            ("Rfc receptor", "rfc_receptor"),
            ("Sello CFDI", "sello_cfdi"),
            ("Sello del timbre fiscal", "sello_sat"),
        ]

        for label, key in validation_rows:
            try:
                row = page.locator(f"tr:has(td:has-text('{label}'))")
                if await row.count() > 0:
                    cells = row.first.locator("td")
                    if await cells.count() >= 2:
                        results[key] = (await cells.nth(1).inner_text()).strip()
            except Exception:
                pass

        # Extract emisor/receptor info from second table
        try:
            # Find the row with RFC and name data
            data_rows = page.locator("table").nth(1).locator("tr")
            row_count = await data_rows.count()

            for i in range(row_count):
                row = data_rows.nth(i)
                cells = row.locator("td")
                cell_count = await cells.count()

                if cell_count >= 4:
                    texts = []
                    for j in range(cell_count):
                        text = (await cells.nth(j).inner_text()).strip()
                        texts.append(text)

                    # Check if this looks like the emisor/receptor row
                    if len(texts) >= 4 and texts[0] and not texts[0].startswith("RFC"):
                        if not results["rfc_emisor"] or results["rfc_emisor"] == texts[0]:
                            results["rfc_emisor"] = texts[0]
                            results["nombre_emisor"] = texts[1]
                            results["rfc_receptor"] = texts[2]
                            results["nombre_receptor"] = texts[3]
                            break
        except Exception:
            pass

        # Extract terceros info
        try:
            terceros_row = page.locator("tr:has(td:has-text('SACE'))")
            if await terceros_row.count() > 0:
                text = await terceros_row.first.inner_text()
                parts = text.split()
                if len(parts) >= 2:
                    results["rfc_terceros"] = parts[0]
                    results["nombre_terceros"] = " ".join(parts[1:]).replace("\t", " ").strip()
        except Exception:
            pass

        # Extract dates and PAC
        try:
            # Look for the row with dates
            date_row = page.locator(f"tr:has(td:has-text('2026-'))")
            if await date_row.count() > 0:
                cells = date_row.first.locator("td")
                if await cells.count() >= 4:
                    results["folio_fiscal"] = (await cells.nth(0).inner_text()).strip()
                    results["fecha_expedicion"] = (await cells.nth(1).inner_text()).strip()
                    results["fecha_certificacion"] = (await cells.nth(2).inner_text()).strip()
                    results["pac_certificador"] = (await cells.nth(3).inner_text()).strip()
        except Exception:
            pass

        # Extract total, efecto, estado
        try:
            total_row = page.locator("tr:has(td:has-text('$'))")
            if await total_row.count() > 0:
                cells = total_row.first.locator("td")
                if await cells.count() >= 4:
                    results["total"] = (await cells.nth(0).inner_text()).strip()
                    results["efecto"] = (await cells.nth(1).inner_text()).strip()
                    results["estado"] = (await cells.nth(2).inner_text()).strip()
                    results["estatus_cancelacion"] = (await cells.nth(3).inner_text()).strip()
        except Exception:
            pass

    except Exception as e:
        Actor.log.warning(f"Error extracting results: {e}")

    return results


async def main() -> None:
    """Main entry point for the Apify actor."""
    async with Actor:
        # Get input
        actor_input = await Actor.get_input() or {}

        xml_content = actor_input.get("xmlContent")
        xml_url = actor_input.get("xmlUrl")
        xml_files = actor_input.get("xmlFiles", [])
        max_retries = actor_input.get("maxRetries", 3)
        headless = actor_input.get("headless", True)

        # Collect all XML contents to process
        xml_items = []

        if xml_content:
            xml_items.append({"content": xml_content, "source": "input"})

        if xml_url:
            Actor.log.info(f"Downloading XML from: {xml_url}")
            async with httpx.AsyncClient() as client:
                response = await client.get(xml_url)
                response.raise_for_status()
                xml_items.append({"content": response.text, "source": xml_url})

        for item in xml_files:
            if item.get("url"):
                Actor.log.info(f"Downloading XML from: {item['url']}")
                async with httpx.AsyncClient() as client:
                    response = await client.get(item["url"])
                    response.raise_for_status()
                    xml_items.append({
                        "content": response.text,
                        "source": item.get("filename") or item["url"]
                    })
            elif item.get("content"):
                # Check if base64 encoded
                content = item["content"]
                try:
                    content = base64.b64decode(content).decode("utf-8")
                except Exception:
                    pass  # Not base64, use as-is
                xml_items.append({
                    "content": content,
                    "source": item.get("filename") or "inline"
                })

        if not xml_items:
            raise ValueError("No XML content provided. Use xmlContent, xmlUrl, or xmlFiles.")

        Actor.log.info(f"Processing {len(xml_items)} XML file(s)...")

        # Process each XML
        for i, item in enumerate(xml_items):
            Actor.log.info(f"Verifying CFDI {i + 1}/{len(xml_items)}: {item['source']}")

            try:
                results = await verify_cfdi(
                    xml_content=item["content"],
                    headless=headless,
                    max_retries=max_retries
                )
                results["source"] = item["source"]

                # Push to dataset
                await Actor.push_data(results)

                Actor.log.info(f"CFDI {i + 1} verified: {'VALID' if results['valid'] else 'INVALID'}")

            except Exception as e:
                Actor.log.error(f"Failed to verify CFDI {i + 1}: {e}")
                await Actor.push_data({
                    "valid": False,
                    "error": str(e),
                    "source": item["source"]
                })

        Actor.log.info("Done!")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
