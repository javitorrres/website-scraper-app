import os
import re
import uuid
import json
import time
import requests
import pandas as pd
import streamlit as st

from io import BytesIO
from datetime import datetime
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False


BASE_OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_OUTPUT_DIR, "logs")
HISTORY_FILE = os.path.join(LOGS_DIR, "app_scrape_history.csv")

os.makedirs(LOGS_DIR, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


OUTPUT_COLUMNS = [
    "source", "site", "product_name", "variant_name", "brand",
    "price", "compare_at_price", "currency", "availability",
    "product_url", "image_url", "description", "category",
    "sku", "barcode", "vendor", "product_type", "variant_count",
    "variant_id", "product_id", "variants", "tags", "scraped_at",
]


CLEAN_EXPORT_COLUMNS = [
    "product_name", "variant_name", "price", "compare_at_price",
    "currency", "availability", "sku", "barcode", "brand",
    "vendor", "product_type", "image_url", "product_url", "description",
]


HISTORY_COLUMNS = [
    "run_id",
    "scraped_at",
    "user",
    "site",
    "url",
    "total_rows",
    "exported_rows",
    "variant_expansion",
    "export_mode",
    "csv_path",
    "excel_path",
]


def is_running_on_streamlit_cloud():
    return os.path.exists("/home/appuser")


def get_configured_password():
    try:
        secret_password = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        secret_password = ""

    env_password = os.environ.get("APP_PASSWORD", "")

    return str(secret_password or env_password).strip()


def password_gate():
    configured_password = get_configured_password()

    if not configured_password:
        return True

    if st.session_state.get("authenticated") is True:
        return True

    st.title("Website Product Scraper")
    st.write("Enter the team password to access the scraper.")

    entered_password = st.text_input("Password", type="password")
    login_button = st.button("Log in")

    if login_button:
        if entered_password == configured_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False


def clean_text(value):
    if value is None:
        return ""
    value = str(value)
    value = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", value).strip()


def clean_price(value):
    if value is None:
        return ""
    value = str(value).strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d{1,2})?)", value)
    return match.group(1) if match else ""


def price_to_float(value):
    cleaned = clean_price(value)
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except Exception:
        return None


def detect_currency(value, fallback="USD"):
    if not value:
        return fallback
    value = str(value)
    if "$" in value:
        return "USD"
    if "€" in value:
        return "EUR"
    if "£" in value:
        return "GBP"
    if "CAD" in value.upper():
        return "CAD"
    if "AUD" in value.upper():
        return "AUD"
    return fallback


def normalize_url(base_url, possible_url):
    if not possible_url:
        return ""
    possible_url = str(possible_url).strip()
    if possible_url.startswith("//"):
        return "https:" + possible_url
    return urljoin(base_url, possible_url)


def normalize_availability(value):
    if value is None:
        return ""

    value = str(value).lower().strip()

    if value in ["true", "instock", "in stock", "available", "https://schema.org/instock"]:
        return "In stock"

    if value in ["false", "outofstock", "out of stock", "sold out", "https://schema.org/outofstock"]:
        return "Out of stock"

    if "instock" in value or "in stock" in value:
        return "In stock"

    if "outofstock" in value or "out of stock" in value or "sold out" in value:
        return "Out of stock"

    return clean_text(value)


def get_site_name(url):
    return urlparse(url).netloc.replace("www.", "")


def make_empty_product(base_url, source):
    return {
        "source": source,
        "site": get_site_name(base_url),
        "product_name": "",
        "variant_name": "",
        "brand": "",
        "price": "",
        "compare_at_price": "",
        "currency": "USD",
        "availability": "",
        "product_url": "",
        "image_url": "",
        "description": "",
        "category": "",
        "sku": "",
        "barcode": "",
        "vendor": "",
        "product_type": "",
        "variant_count": "",
        "variant_id": "",
        "product_id": "",
        "variants": "",
        "tags": "",
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def dedupe_products(products):
    seen = set()
    deduped = []

    for product in products:
        product_name = clean_text(product.get("product_name", ""))
        variant_name = clean_text(product.get("variant_name", ""))
        product_url = clean_text(product.get("product_url", ""))
        sku = clean_text(product.get("sku", ""))
        variant_id = clean_text(product.get("variant_id", ""))

        if not product_name:
            continue

        key = variant_id or sku or f"{product_url}|{product_name}|{variant_name}"

        if not key.strip("|"):
            continue

        if key in seen:
            continue

        seen.add(key)
        deduped.append(product)

    return deduped


def normalize_products(products):
    normalized = []

    for product in products:
        clean_product = {}

        for column in OUTPUT_COLUMNS:
            clean_product[column] = clean_text(product.get(column, ""))

        if not clean_product["product_name"]:
            continue

        price_val = price_to_float(clean_product.get("price", ""))
        compare_val = price_to_float(clean_product.get("compare_at_price", ""))

        clean_product["price"] = price_val if price_val is not None else ""
        clean_product["compare_at_price"] = compare_val if compare_val is not None else ""
        clean_product["availability"] = normalize_availability(clean_product.get("availability", ""))

        if not clean_product.get("currency"):
            clean_product["currency"] = "USD"

        normalized.append(clean_product)

    return dedupe_products(normalized)


def fetch_url(url, timeout=20):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def build_shopify_variant_rows(base_url, product, expand_variants):
    rows = []

    title = product.get("title") or ""
    if not title:
        return rows

    handle = product.get("handle") or ""
    vendor = product.get("vendor") or ""
    product_type = product.get("product_type") or ""
    tags = product.get("tags") or []
    variants = product.get("variants") or []
    images = product.get("images") or []
    product_id = product.get("id") or ""

    product_url = urljoin(base_url.rstrip("/") + "/", f"products/{handle}") if handle else base_url

    raw_html = product.get("body_html") or ""
    description = BeautifulSoup(str(raw_html), "lxml").get_text(" ", strip=True)

    image_url = images[0].get("src") if images else ""

    variant_rows = []

    for variant in variants:
        variant_rows.append({
            "id": variant.get("id") or "",
            "title": variant.get("title") or "",
            "price": variant.get("price") or "",
            "compare_at_price": variant.get("compare_at_price") or "",
            "sku": variant.get("sku") or "",
            "barcode": variant.get("barcode") or "",
            "available": variant.get("available"),
        })

    tags_text = ", ".join(tags) if isinstance(tags, list) else str(tags)
    variants_json = json.dumps(variant_rows, ensure_ascii=False)

    if expand_variants and variants:
        for variant in variants:
            item = make_empty_product(base_url, "Shopify products.json - variant row")

            variant_title = variant.get("title") or ""
            if variant_title.lower() == "default title":
                variant_title = ""

            item.update({
                "product_name": title,
                "variant_name": variant_title,
                "brand": vendor,
                "price": variant.get("price") or "",
                "compare_at_price": variant.get("compare_at_price") or "",
                "currency": "USD",
                "availability": "In stock" if variant.get("available") else "Out of stock",
                "product_url": product_url,
                "image_url": image_url,
                "description": description,
                "category": product_type,
                "sku": variant.get("sku") or "",
                "barcode": variant.get("barcode") or "",
                "vendor": vendor,
                "product_type": product_type,
                "variant_count": len(variants),
                "variant_id": variant.get("id") or "",
                "product_id": product_id,
                "variants": variants_json,
                "tags": tags_text,
            })

            rows.append(item)

    else:
        item = make_empty_product(base_url, "Shopify products.json - product row")

        available_any = False
        prices = []
        compare_prices = []
        sku = ""
        barcode = ""

        for variant in variants:
            if variant.get("available"):
                available_any = True

            if variant.get("price"):
                prices.append(str(variant.get("price")))

            if variant.get("compare_at_price"):
                compare_prices.append(str(variant.get("compare_at_price")))

            if not sku and variant.get("sku"):
                sku = variant.get("sku")

            if not barcode and variant.get("barcode"):
                barcode = variant.get("barcode")

        clean_prices = [float(clean_price(p)) for p in prices if clean_price(p)]
        clean_compare_prices = [float(clean_price(p)) for p in compare_prices if clean_price(p)]

        item.update({
            "product_name": title,
            "variant_name": "",
            "brand": vendor,
            "price": min(clean_prices) if clean_prices else "",
            "compare_at_price": min(clean_compare_prices) if clean_compare_prices else "",
            "currency": "USD",
            "availability": "In stock" if available_any else "Out of stock",
            "product_url": product_url,
            "image_url": image_url,
            "description": description,
            "category": product_type,
            "sku": sku,
            "barcode": barcode,
            "vendor": vendor,
            "product_type": product_type,
            "variant_count": len(variants),
            "variant_id": "",
            "product_id": product_id,
            "variants": variants_json,
            "tags": tags_text,
        })

        rows.append(item)

    return rows


def scrape_shopify(base_url, log, expand_variants=False):
    products = []
    page = 1

    log.append("Checking Shopify products.json endpoint...")

    while True:
        products_url = urljoin(base_url.rstrip("/") + "/", f"products.json?limit=250&page={page}")

        try:
            response = fetch_url(products_url)
            data = response.json()
        except Exception as error:
            if page == 1:
                log.append(f"Shopify endpoint unavailable or blocked: {error}")
            break

        page_products = data.get("products", [])

        if not page_products:
            break

        log.append(f"Shopify page {page}: found {len(page_products)} products.")

        for product in page_products:
            products.extend(build_shopify_variant_rows(base_url, product, expand_variants))

        page += 1
        time.sleep(0.25)

    return products


def extract_json_ld_products(base_url, soup):
    products = []

    scripts = soup.find_all("script", type="application/ld+json")

    for script in scripts:
        try:
            raw = script.string or script.get_text()
            data = json.loads(raw)
        except Exception:
            continue

        candidates = []

        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            candidates.append(data)

            graph = data.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue

            item_type = candidate.get("@type")

            if isinstance(item_type, list):
                is_product = "Product" in item_type
            else:
                is_product = item_type == "Product"

            if not is_product:
                continue

            product_name = clean_text(candidate.get("name", ""))

            if not product_name:
                continue

            offers = candidate.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            image = candidate.get("image", "")
            if isinstance(image, list):
                image = image[0] if image else ""

            brand = candidate.get("brand", "")
            if isinstance(brand, dict):
                brand = brand.get("name", "")

            price = ""
            currency = "USD"
            availability = ""

            if isinstance(offers, dict):
                price = offers.get("price", "")
                currency = offers.get("priceCurrency", "USD")
                availability = normalize_availability(offers.get("availability", ""))

            item = make_empty_product(base_url, "JSON-LD")

            item.update({
                "product_name": product_name,
                "variant_name": "",
                "brand": brand,
                "price": price,
                "compare_at_price": "",
                "currency": currency,
                "availability": availability,
                "product_url": normalize_url(base_url, candidate.get("url", "")) or base_url,
                "image_url": normalize_url(base_url, image),
                "description": candidate.get("description", ""),
                "category": candidate.get("category", ""),
                "sku": candidate.get("sku", ""),
                "barcode": candidate.get("gtin13", "") or candidate.get("gtin12", "") or candidate.get("gtin", ""),
            })

            products.append(item)

    return products


def scrape_product_page(base_url, product_url, log):
    try:
        response = fetch_url(product_url)
    except Exception:
        return None

    soup = BeautifulSoup(response.text, "lxml")

    json_ld_products = extract_json_ld_products(product_url, soup)
    if json_ld_products:
        product = json_ld_products[0]
        product["source"] = "Product page JSON-LD"
        product["product_url"] = product_url
        return product

    item = make_empty_product(base_url, "Product page fallback")

    title = ""

    selectors = [
        "h1",
        "[class*='product-title']",
        "[class*='ProductTitle']",
        "[class*='product__title']",
        "[data-product-title]",
    ]

    for selector in selectors:
        found = soup.select_one(selector)
        if found:
            title = clean_text(found.get_text())
            if title:
                break

    if not title:
        return None

    price = ""

    price_selectors = [
        "[class*='price']",
        "[data-price]",
        "[itemprop='price']",
        "meta[property='product:price:amount']",
    ]

    for selector in price_selectors:
        found = soup.select_one(selector)
        if found:
            if found.name == "meta":
                price = found.get("content", "")
            else:
                price = clean_text(found.get_text())

            if clean_price(price):
                break

    image_url = ""

    image_selectors = [
        "meta[property='og:image']",
        "meta[name='twitter:image']",
        "img[class*='product']",
        "img",
    ]

    for selector in image_selectors:
        found = soup.select_one(selector)
        if found:
            image_url = found.get("content") or found.get("src") or found.get("data-src") or ""
            image_url = normalize_url(base_url, image_url)
            if image_url:
                break

    description = ""

    desc_selectors = [
        "meta[property='og:description']",
        "meta[name='description']",
        "[class*='description']",
        "[class*='Description']",
        "[class*='product-details']",
    ]

    for selector in desc_selectors:
        found = soup.select_one(selector)
        if found:
            if found.name == "meta":
                description = found.get("content", "")
            else:
                description = clean_text(found.get_text())

            if description:
                break

    item.update({
        "product_name": title,
        "variant_name": "",
        "price": price,
        "currency": detect_currency(price),
        "product_url": product_url,
        "image_url": image_url,
        "description": description,
    })

    return item


def discover_sitemap_urls(base_url, log):
    discovered = []

    sitemap_candidates = [
        urljoin(base_url.rstrip("/") + "/", "sitemap.xml"),
        urljoin(base_url.rstrip("/") + "/", "sitemap_products_1.xml"),
        urljoin(base_url.rstrip("/") + "/", "product-sitemap.xml"),
    ]

    for sitemap_url in sitemap_candidates:
        try:
            response = fetch_url(sitemap_url, timeout=15)
        except Exception:
            continue

        soup = BeautifulSoup(response.text, "xml")
        locs = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

        product_locs = [
            loc for loc in locs
            if "/products/" in loc or "/product/" in loc or "/shop/" in loc
        ]

        if product_locs:
            log.append(f"Sitemap discovery found {len(product_locs)} possible product URLs.")
            discovered.extend(product_locs)

    return list(dict.fromkeys(discovered))


def discover_static_product_urls(base_url, log):
    discovered = []

    crawl_paths = [
        "/shop/",
        "/products/",
        "/collections/all/",
        "/collections/",
        "/store/",
    ]

    for path in crawl_paths:
        page_url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

        try:
            response = fetch_url(page_url, timeout=15)
        except Exception:
            continue

        soup = BeautifulSoup(response.text, "lxml")

        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            full_url = normalize_url(base_url, href)

            if not full_url:
                continue

            if any(token in full_url for token in ["/products/", "/product/", "/shop/"]):
                discovered.append(full_url)

    discovered = list(dict.fromkeys(discovered))

    if discovered:
        log.append(f"Static crawl found {len(discovered)} possible product URLs.")

    return discovered


def scrape_static_fallback(base_url, log):
    product_urls = []

    product_urls.extend(discover_sitemap_urls(base_url, log))
    product_urls.extend(discover_static_product_urls(base_url, log))

    product_urls = list(dict.fromkeys(product_urls))

    if not product_urls:
        log.append("No product URLs found through sitemap/static crawl.")
        return []

    log.append(f"Scraping up to {min(len(product_urls), 150)} product pages for better accuracy...")

    products = []

    for index, product_url in enumerate(product_urls[:150], start=1):
        product = scrape_product_page(base_url, product_url, log)

        if product and clean_text(product.get("product_name", "")):
            products.append(product)

        if index % 25 == 0:
            log.append(f"Checked {index} product pages...")

        time.sleep(0.15)

    return products


def scrape_with_playwright(base_url, log):
    if not PLAYWRIGHT_AVAILABLE:
        log.append("Playwright is not installed in this environment.")
        return []

    log.append("Starting hard-site mode with Playwright...")

    products = []
    discovered_urls = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])

            entry_urls = [
                base_url,
                urljoin(base_url.rstrip("/") + "/", "shop/"),
                urljoin(base_url.rstrip("/") + "/", "products/"),
                urljoin(base_url.rstrip("/") + "/", "collections/all/"),
            ]

            for entry_url in entry_urls:
                try:
                    page.goto(entry_url, wait_until="networkidle", timeout=30000)

                    for _ in range(5):
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(1000)

                    html = page.content()
                    soup = BeautifulSoup(html, "lxml")

                    products.extend(extract_json_ld_products(entry_url, soup))

                    for link in soup.find_all("a", href=True):
                        href = link.get("href", "")
                        full_url = normalize_url(base_url, href)

                        if any(token in full_url for token in ["/products/", "/product/", "/shop/"]):
                            discovered_urls.append(full_url)

                except Exception:
                    continue

            browser.close()

    except Exception as error:
        log.append(f"Playwright failed: {error}")
        return products

    discovered_urls = list(dict.fromkeys(discovered_urls))

    if discovered_urls:
        log.append(f"Playwright discovered {len(discovered_urls)} possible product URLs.")

    for product_url in discovered_urls[:150]:
        product = scrape_product_page(base_url, product_url, log)

        if product and clean_text(product.get("product_name", "")):
            product["source"] = "Playwright discovered page"
            products.append(product)

        time.sleep(0.15)

    return products


def scrape_website(base_url, use_hard_site_mode=False, expand_variants=False):
    log = []
    all_products = []

    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url

    base_url = base_url.strip().rstrip("/")

    log.append(f"Starting scrape: {base_url}")

    shopify_products = scrape_shopify(base_url, log, expand_variants=expand_variants)
    all_products.extend(shopify_products)

    if not shopify_products:
        log.append("No Shopify products found. Trying sitemap/static fallback...")
        all_products.extend(scrape_static_fallback(base_url, log))
    else:
        log.append("Shopify data found. Running static discovery as a secondary accuracy pass...")
        all_products.extend(scrape_static_fallback(base_url, log))

    if use_hard_site_mode:
        all_products.extend(scrape_with_playwright(base_url, log))

    all_products = normalize_products(all_products)

    log.append(f"Finished. Final unique rows: {len(all_products)}")

    return all_products, log


def prepare_dataframe(products):
    df = pd.DataFrame(products)

    for column in OUTPUT_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    df = df[OUTPUT_COLUMNS]

    if not df.empty:
        df["price_numeric"] = df["price"].apply(price_to_float)
        df["compare_at_price_numeric"] = df["compare_at_price"].apply(price_to_float)

    return df


def apply_filters(df, in_stock_only=False, has_image_only=False, min_price=None, max_price=None, search_text=""):
    filtered = df.copy()

    if filtered.empty:
        return filtered

    if in_stock_only:
        filtered = filtered[filtered["availability"].str.lower() == "in stock"]

    if has_image_only:
        filtered = filtered[filtered["image_url"].astype(str).str.strip() != ""]

    if min_price is not None:
        filtered = filtered[(filtered["price_numeric"].isna()) | (filtered["price_numeric"] >= min_price)]

    if max_price is not None:
        filtered = filtered[(filtered["price_numeric"].isna()) | (filtered["price_numeric"] <= max_price)]

    if search_text.strip():
        query = search_text.strip().lower()

        searchable = (
            filtered["product_name"].fillna("").astype(str).str.lower() + " " +
            filtered["variant_name"].fillna("").astype(str).str.lower() + " " +
            filtered["brand"].fillna("").astype(str).str.lower() + " " +
            filtered["vendor"].fillna("").astype(str).str.lower() + " " +
            filtered["product_type"].fillna("").astype(str).str.lower() + " " +
            filtered["sku"].fillna("").astype(str).str.lower()
        )

        filtered = filtered[searchable.str.contains(re.escape(query), na=False)]

    return filtered


def make_export_dataframe(df, export_mode):
    export_df = df.copy()

    for helper_column in ["price_numeric", "compare_at_price_numeric"]:
        if helper_column in export_df.columns:
            export_df = export_df.drop(columns=[helper_column])

    if export_mode == "Clean export":
        columns = [column for column in CLEAN_EXPORT_COLUMNS if column in export_df.columns]
    else:
        columns = [column for column in OUTPUT_COLUMNS if column in export_df.columns]

    return export_df[columns]


def dataframe_to_excel_bytes(df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Products")

    return output.getvalue()


def save_outputs(df, site, export_mode):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_site = re.sub(r"[^a-zA-Z0-9_-]", "_", site)
    mode_slug = "clean" if export_mode == "Clean export" else "raw"

    csv_path = os.path.join(LOGS_DIR, f"{safe_site}_{mode_slug}_{timestamp}.csv")
    excel_path = os.path.join(LOGS_DIR, f"{safe_site}_{mode_slug}_{timestamp}.xlsx")

    df.to_csv(csv_path, index=False)
    df.to_excel(excel_path, index=False)

    return csv_path, excel_path


def save_history(url, total_count, filtered_count, csv_path, excel_path, expand_variants, export_mode, user_name):
    site = get_site_name(url)

    row = {
        "run_id": str(uuid.uuid4()),
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user": clean_text(user_name) or "Unknown",
        "site": site,
        "url": url,
        "total_rows": total_count,
        "exported_rows": filtered_count,
        "variant_expansion": "Yes" if expand_variants else "No",
        "export_mode": export_mode,
        "csv_path": csv_path,
        "excel_path": excel_path,
    }

    if os.path.exists(HISTORY_FILE):
        try:
            history_df = pd.read_csv(HISTORY_FILE)
        except Exception:
            history_df = pd.DataFrame(columns=HISTORY_COLUMNS)
    else:
        history_df = pd.DataFrame(columns=HISTORY_COLUMNS)

    for column in HISTORY_COLUMNS:
        if column not in history_df.columns:
            history_df[column] = ""

    history_df = history_df[HISTORY_COLUMNS]

    new_row_df = pd.DataFrame([row], columns=HISTORY_COLUMNS)
    history_df = pd.concat([history_df, new_row_df], ignore_index=True)

    history_df.to_csv(HISTORY_FILE, index=False)

    return row["run_id"]


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    try:
        history_df = pd.read_csv(HISTORY_FILE)
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)

    for column in HISTORY_COLUMNS:
        if column not in history_df.columns:
            history_df[column] = ""

    history_df = history_df[HISTORY_COLUMNS]

    return history_df


def show_team_instructions():
    with st.expander("Team Instructions", expanded=False):
        st.markdown(
            """
### Basic workflow

1. Enter the website URL.
2. Leave **Hard-site mode** off for normal Shopify stores.
3. Turn **Expand Shopify variants into separate rows** on when you need sizes, colors, SKUs, or variant-level pricing.
4. Use **Clean export** for normal business use.
5. Use **Raw export** only when debugging or reviewing source data.
6. Apply filters before downloading if you only want certain rows.

### Important cloud limitation

The live Streamlit Cloud version does not support hard-site browser rendering right now.

For harder JavaScript-heavy sites that need browser rendering, use the local version on Javi's computer.

### Recommended settings

For most Shopify stores:

- Hard-site mode: off
- Expand variants: on
- Export type: Clean export
- Filters: optional

### Export notes

The app saves files locally and also provides download buttons.

Local saved files are stored in:

```text
~/Desktop/website-scraper-app/logs
```

The scrape history table shows previous runs, user names, row counts, export type, and file paths.

On Streamlit Cloud, this history is temporary. Download the history CSV if you need to keep it.

### Data notes

- Product rows are deduplicated.
- Blank product names are removed.
- Prices are normalized as numbers when possible.
- Variant expansion creates one row per Shopify variant.
"""
        )


st.set_page_config(
    page_title="Website Product Scraper",
    page_icon="",
    layout="wide",
)

if not password_gate():
    st.stop()

st.title("Website Product Scraper")
st.write("Scrape product data from Shopify stores, product pages, sitemaps, and harder JavaScript-rendered sites.")

show_team_instructions()

if get_configured_password():
    with st.sidebar:
        st.success("Password protection enabled.")
        if st.button("Log out"):
            st.session_state["authenticated"] = False
            st.rerun()
else:
    with st.sidebar:
        st.warning("Password protection is not configured.")

url = st.text_input("Website URL", placeholder="https://example.com")

if is_running_on_streamlit_cloud():
    st.info("Hard-site mode is disabled in the cloud version. Use the local version for advanced JavaScript-heavy sites.")

    use_hard_site_mode = st.checkbox(
        "Hard-site mode (disabled in cloud)",
        value=False,
        disabled=True,
        help="Disabled on Streamlit Cloud because browser rendering requires a Playwright browser binary.",
    )
else:
    use_hard_site_mode = st.checkbox(
        "Hard-site mode",
        help="Uses browser rendering with Playwright. Slower, but better for JavaScript-heavy sites.",
    )

expand_variants = st.checkbox(
    "Expand Shopify variants into separate rows",
    value=False,
    help="Recommended for detailed analysis. Creates one row per product variant instead of one row per product.",
)

st.subheader("History Options")

history_col_1, history_col_2 = st.columns([1, 2])

with history_col_1:
    save_scrape_to_history = st.checkbox(
        "Save scrape to local history",
        value=True,
        help="Saves a summary of each scrape to a local CSV file. On Streamlit Cloud, this may reset between sessions.",
    )

with history_col_2:
    user_name = st.text_input(
        "User name for history",
        value="Javi",
        help="Used only for the local scrape history table.",
    )

st.subheader("Export Options")

export_mode = st.radio(
    "Export type",
    options=["Clean export", "Raw export"],
    index=0,
    horizontal=True,
)

with st.expander("Filters", expanded=True):
    filter_col_1, filter_col_2, filter_col_3 = st.columns(3)

    with filter_col_1:
        in_stock_only = st.checkbox("In-stock only", value=False)

    with filter_col_2:
        has_image_only = st.checkbox("Has image only", value=False)

    with filter_col_3:
        sort_option = st.selectbox(
            "Sort results",
            options=[
                "Default",
                "Product name A-Z",
                "Price low to high",
                "Price high to low",
            ],
        )

    search_text = st.text_input("Search within results", placeholder="Search product name, variant, brand, SKU...")

    price_filter_enabled = st.checkbox("Enable price filter", value=False)

    min_price = None
    max_price = None

    if price_filter_enabled:
        price_col_1, price_col_2 = st.columns(2)

        with price_col_1:
            min_price = st.number_input("Minimum price", min_value=0.0, value=0.0, step=1.0)

        with price_col_2:
            max_price = st.number_input("Maximum price", min_value=0.0, value=9999.0, step=1.0)


run_button = st.button("Scrape Website")

if run_button:
    if not url.strip():
        st.error("Enter a website URL first.")
    else:
        with st.spinner("Scraping..."):
            products, run_log = scrape_website(
                url.strip(),
                use_hard_site_mode=use_hard_site_mode,
                expand_variants=expand_variants,
            )

        st.subheader("Run Log")
        for line in run_log:
            st.write(line)

        if not products:
            st.warning("No products found.")
        else:
            df = prepare_dataframe(products)

            filtered_df = apply_filters(
                df,
                in_stock_only=in_stock_only,
                has_image_only=has_image_only,
                min_price=min_price,
                max_price=max_price,
                search_text=search_text,
            )

            if sort_option == "Product name A-Z":
                filtered_df = filtered_df.sort_values(["product_name", "variant_name"], ascending=True)

            elif sort_option == "Price low to high":
                filtered_df = filtered_df.sort_values(
                    by=["price_numeric", "product_name"],
                    ascending=[True, True],
                    na_position="last",
                )

            elif sort_option == "Price high to low":
                filtered_df = filtered_df.sort_values(
                    by=["price_numeric", "product_name"],
                    ascending=[False, True],
                    na_position="last",
                )

            export_df = make_export_dataframe(filtered_df, export_mode)

            site = get_site_name(url)
            csv_path, excel_path = save_outputs(export_df, site, export_mode)

            if save_scrape_to_history:
                try:
                    run_id = save_history(
                        url=url,
                        total_count=len(df),
                        filtered_count=len(export_df),
                        csv_path=csv_path,
                        excel_path=excel_path,
                        expand_variants=expand_variants,
                        export_mode=export_mode,
                        user_name=user_name,
                    )
                    st.caption(f"Saved scrape to local history. Run ID: {run_id}")
                except Exception as error:
                    st.warning(f"Scrape completed, but local history save failed: {error}")

            if expand_variants:
                st.success(f"Found {len(df)} unique variant rows. Exporting {len(export_df)} filtered rows.")
            else:
                st.success(f"Found {len(df)} unique product rows. Exporting {len(export_df)} filtered rows.")

            st.subheader("Preview")
            st.dataframe(export_df, use_container_width=True)

            csv_bytes = export_df.to_csv(index=False).encode("utf-8")
            excel_bytes = dataframe_to_excel_bytes(export_df)

            file_prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", site)
            mode_slug = "clean" if export_mode == "Clean export" else "raw"

            st.download_button(
                label="Download CSV",
                data=csv_bytes,
                file_name=f"{file_prefix}_{mode_slug}_products.csv",
                mime="text/csv",
            )

            st.download_button(
                label="Download Excel",
                data=excel_bytes,
                file_name=f"{file_prefix}_{mode_slug}_products.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            st.caption(f"Saved CSV locally to: {csv_path}")
            st.caption(f"Saved Excel locally to: {excel_path}")


st.divider()

st.subheader("Scrape History")

history_df = load_history()

if history_df.empty:
    st.write("No scrape history yet.")
else:
    if "scraped_at" in history_df.columns:
        history_df = history_df.sort_values("scraped_at", ascending=False)

    st.dataframe(history_df, use_container_width=True)

    history_csv = history_df.to_csv(index=False).encode("utf-8")

    st.download_button(
        label="Download scrape history CSV",
        data=history_csv,
        file_name="scrape_history.csv",
        mime="text/csv",
    )
