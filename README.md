# Local Amazon / Flipkart price checker

Streamlit app that searches **Amazon.in** and **Flipkart** from a product keyword, skips sponsored ads, opens the first organic product page, and shows live price + deliverability.

Uses **sync Playwright**, a **persistent browser profile** (`./browser_session`), **playwright-stealth**, and **tenacity** retries.

## Install

```powershell
cd C:\Users\Rituraj\Projects\price-compare
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## One-time warm-up (cache Kolkata pincode)

Do this once so cookies and delivery location stick in `./browser_session`:

1. Run: `streamlit run app.py`
2. In the sidebar, enable **Headed (warm-up)**
3. Search any product (e.g. `iPhone 15 128GB`)
4. In the visible Chromium window:
   - On Amazon.in, set delivery pincode to your Kolkata PIN
   - On Flipkart, set the same pincode
   - Complete any captcha / login if prompted
5. Close the app when done

Later runs can stay headless; the profile reuses your saved location.

## Usage

```powershell
streamlit run app.py
```

Enter a product name or keyword and click **Compare prices**. Results appear in two columns (Amazon.in | Flipkart).

## Notes

- Amazon and Flipkart terms typically disallow automated scraping. This is a **local personal** tool and may still hit captchas, layout changes, or blocks.
- Do not commit `browser_session/` (it is gitignored).
- If searches fail often, re-run headed warm-up and solve captchas manually once.
