import os
import re
import csv
import json
import time
import requests
import pandas as pd
import streamlit as st

from bs4 import BeautifulSoup
from datetime import date, datetime
from urllib.parse import urlparse, urljoin
from io import BytesIO

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

BASE_OUTPUT_DIR = os.path.expanduser("~/Desktop/website scraper")
HISTORY_DIR = os.path.join(BASE_OUTPUT_DIR, "logs")
HISTORY_FILE = os.path.join(HISTORY_DIR, "app_scrape_history.csv")

COMMON_PRODUCT_LISTING_PATHS = [
    "/shop/",
    "/products/",
    "/collections/all/",
    "/collections/",
    "/store/",
]

EXCLUDED_URL_PARTS = [
    "/cart",
    "/checkout",
    "/account",
    "/login",
    "/admin",
    "/wp-admin",
]


def normalize_base_url(url):
    if not url.startswith("http"):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def clean_site_name(url):
    domain = urlparse(url).netloc.lower().replace("www.", "")
    return re.sub(r"[^a-zA-Z0-9_-]", "", domain.split(".")[0])


def is_safe_url(url):
    return not any(part in url.lower() for part in EXCLUDED_URL_PARTS)


def ensure_dirs(site_name):
    site_folder = os.path.join(BASE_OUTPUT_DIR, site_name)
    os.makedirs(site_folder, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    return site_folder


def try_shopify(base_url, log):
    products = []
    page = 1

    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"

        try:
            response = requests.get(url, headers=HEADERS, timeout=20)
        except Exception:
            break

        if response.status_code != 200:
            break

        try:
            data = response.json()
        except Exception:
            break

        items = data.get("products", [])

        if not items:
            break

        for product in items:
            variants = product.get("variants", [])

            raw_html = product.get("body_html") or ""
            description = BeautifulSoup(str(raw_html), "lxml").get_text(" ", strip=True)

            products.append({
                "name": product.get("title", ""),
                "price": variants[0].get("price", "") if variants else "",
                "description": description,
                "url": f"{base_url}/products/{product.get('handle', '')}"
            })

        log.append(f"Shopify page {page}: found {len(items)} products")
        page += 1
        time.sleep(0.5)

    return products


def extract_json_ld_products(html, page_url):
    soup = BeautifulSoup(html, "lxml")
    products = []

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            raw = script.string or script.get_text()
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]

            expanded = []
            for item in items:
                expanded.append(item)
                if isinstance(item, dict) and "@graph" in item:
                    expanded.extend(item["@graph"])

            for item in expanded:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("@type", "")
                is_product = "Product" in item_type if isinstance(item_type, list) else item_type == "Product"

                if not is_product:
                    continue

                offers = item.get("offers", {})
                price = ""

                if isinstance(offers, dict):
                    price = offers.get("price", "")
                elif isinstance(offers, list) and offers:
                    price = offers[0].get("price", "")

                products.append({
                    "name": item.get("name", ""),
                    "price": price,
                    "description": item.get("description", ""),
                    "url": page_url,
                })
        except Exception:
            continue

    return products


def crawl_static_product_links(base_url, log):
    found = set()

    for path in COMMON_PRODUCT_LISTING_PATHS:
        page_url = base_url.rstrip("/") + path
        log.append(f"Checking static page: {page_url}")

        try:
            response = requests.get(page_url, headers=HEADERS, timeout=20)
        except Exception:
            continue

        if response.status_code != 200:
            continue

        soup = BeautifulSoup(response.text, "lxml")

        json_products = extract_json_ld_products(response.text, page_url)
        for product in json_products:
            if product.get("url"):
                found.add(product["url"])

        for link in soup.find_all("a", href=True):
            full_url = urljoin(base_url, link["href"])
            clean_url = full_url.split("?")[0].split("#")[0]

            if is_safe_url(clean_url) and ("/product/" in clean_url or "/products/" in clean_url):
                found.add(clean_url.rstrip("/") + "/")

    return list(found)


def crawl_playwright_product_links(base_url, log):
    if not PLAYWRIGHT_AVAILABLE:
        log.append("Playwright is not installed. Hard-site mode unavailable.")
        return []

    found = set()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])

            for path in COMMON_PRODUCT_LISTING_PATHS:
                page_url = base_url.rstrip("/") + path
                log.append(f"Rendering with browser: {page_url}")

                try:
                    page.goto(page_url, wait_until="networkidle", timeout=30000)
                    page.mouse.wheel(0, 2500)
                    time.sleep(1)
                    html = page.content()
                except Exception:
                    continue

                soup = BeautifulSoup(html, "lxml")

                for product in extract_json_ld_products(html, page_url):
                    if product.get("url"):
                        found.add(product["url"])

                for link in soup.find_all("a", href=True):
                    full_url = urljoin(base_url, link["href"])
                    clean_url = full_url.split("?")[0].split("#")[0]

                    if is_safe_url(clean_url) and ("/product/" in clean_url or "/products/" in clean_url):
                        found.add(clean_url.rstrip("/") + "/")

            browser.close()
    except Exception as error:
        log.append(f"Playwright error: {error}")

    return list(found)


def scrape_product_page(url, use_playwright, log):
    html = ""

    if use_playwright and PLAYWRIGHT_AVAILABLE:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                browser.close()
        except Exception:
            html = ""

    if not html:
        response = requests.get(url, headers=HEADERS, timeout=20)
        html = response.text

    json_products = extract_json_ld_products(html, url)
    if json_products and json_products[0].get("name"):
        return json_products[0]

    soup = BeautifulSoup(html, "lxml")
    name = soup.select_one("h1, [class*='product-title'], [class*='ProductTitle']")
    price = soup.select_one("[class*='price'], [class*='Price']")
    desc = soup.select_one("[class*='description'], [class*='Description'], #tab-description")

    return {
        "name": name.get_text(" ", strip=True) if name else "",
        "price": price.get_text(" ", strip=True) if price else "",
        "description": desc.get_text(" ", strip=True) if desc else "",
        "url": url,
    }


def make_excel_bytes(df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Scraped Data")
        sheet = writer.book["Scraped Data"]
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        sheet.column_dimensions["A"].width = 35
        sheet.column_dimensions["B"].width = 15
        sheet.column_dimensions["C"].width = 80
        sheet.column_dimensions["D"].width = 70

    output.seek(0)
    return output


def save_outputs(site_name, df):
    today = date.today().isoformat()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    site_folder = ensure_dirs(site_name)

    csv_path = os.path.join(site_folder, f"{site_name}_{today}.csv")
    xlsx_path = os.path.join(site_folder, f"{site_name}_{today}.xlsx")

    df.to_csv(csv_path, index=False)
    df.to_excel(xlsx_path, index=False)

    history_exists = os.path.exists(HISTORY_FILE)

    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not history_exists:
            writer.writerow(["timestamp", "site", "products", "csv_path", "excel_path"])
        writer.writerow([timestamp, site_name, len(df), csv_path, xlsx_path])

    return csv_path, xlsx_path


def scrape_website(url, use_playwright=False):
    log = []
    base_url = normalize_base_url(url)
    site_name = clean_site_name(base_url)

    log.append(f"Website: {base_url}")
    log.append("Trying Shopify...")

    products = try_shopify(base_url, log)

    if products:
        log.append(f"Shopify detected. Found {len(products)} products.")
    else:
        log.append("Shopify not detected. Trying static product crawl...")
        product_urls = crawl_static_product_links(base_url, log)
        log.append(f"Static crawl found {len(product_urls)} product URLs.")

        if use_playwright:
            log.append("Hard-site mode enabled. Trying browser-rendered crawl...")
            rendered_urls = crawl_playwright_product_links(base_url, log)
            before = len(product_urls)
            product_urls = list(set(product_urls + rendered_urls))
            log.append(f"Browser mode added {len(product_urls) - before} product URLs.")

        products = []
        for product_url in product_urls:
            try:
                product = scrape_product_page(product_url, use_playwright, log)
                products.append(product)
                log.append(f"Scraped: {product_url}")
                time.sleep(0.5)
            except Exception as error:
                log.append(f"Error: {product_url} — {error}")

    df = pd.DataFrame(products)

    if not df.empty:
        df = df.drop_duplicates(subset=["url"])

    csv_path, xlsx_path = save_outputs(site_name, df)
    return site_name, df, csv_path, xlsx_path, log


st.set_page_config(page_title="Website Scraper", layout="wide")

st.title("Website Scraper")
st.caption("Scrape public product data into CSV or Excel.")

with st.expander("Legal / ethical checklist", expanded=False):
    st.write(
        "Only scrape public data. Do not scrape login-only, customer, checkout, account, private, "
        "or restricted pages. Do not bypass CAPTCHA, paywalls, or bot protections."
    )

url = st.text_input("Website URL", placeholder="https://summerfridays.com")
use_playwright = st.checkbox("Hard-site mode: render JavaScript pages with browser automation", value=False)

if use_playwright and not PLAYWRIGHT_AVAILABLE:
    st.warning("Playwright is not installed yet. Install it before using hard-site mode.")

run_button = st.button("Scrape Website", type="primary")

if run_button:
    if not url.strip():
        st.error("Enter a website URL first.")
    else:
        with st.spinner("Scraping..."):
            site_name, df, csv_path, xlsx_path, log = scrape_website(url.strip(), use_playwright=use_playwright)

        with st.expander("Run Log", expanded=True):
            for line in log:
                st.write(line)

        if df.empty:
            st.warning("No products found. This site may need custom logic, stronger browser rendering, or manual API inspection.")
        else:
            today = date.today().isoformat()
            csv_name = f"{site_name}_{today}.csv"
            xlsx_name = f"{site_name}_{today}.xlsx"

            st.success(f"Found {len(df)} products.")
            st.write(f"Auto-saved CSV: `{csv_path}`")
            st.write(f"Auto-saved Excel: `{xlsx_path}`")
            st.dataframe(df, width="stretch")

            csv_data = df.to_csv(index=False).encode("utf-8")
            excel_data = make_excel_bytes(df)

            col1, col2 = st.columns(2)
            with col1:
                st.download_button("Download CSV", data=csv_data, file_name=csv_name, mime="text/csv")
            with col2:
                st.download_button(
                    "Download Excel",
                    data=excel_data,
                    file_name=xlsx_name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

st.divider()
st.subheader("Scrape History")

if os.path.exists(HISTORY_FILE):
    history_df = pd.read_csv(HISTORY_FILE)
    st.dataframe(history_df.tail(25).sort_values("timestamp", ascending=False), width="stretch")
else:
    st.caption("No scrape history yet.")
