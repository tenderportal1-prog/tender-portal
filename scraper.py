"""
Tender scraper - Phase 2.
Reads public tender tables from Pakistani procurement portals and saves them
into your Supabase `tenders` table. Safe to run repeatedly (no duplicates).
"""
import os, re, sys, hashlib
from datetime import timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dparser

PKT = timezone(timedelta(hours=5))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TenderPortalBot/1.0)"}
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))
_DEBUGGED = set()

# ---- Sources. Add or switch off portals here. ----
SOURCES = [
    {"name": "PPRA Federal", "province": "Federal", "paged": True, "enabled": True,
     "url": "https://epms.ppra.gov.pk/public/tenders/active-tenders"},
    # Experimental: these layouts are unverified, so each is isolated.
    {"name": "SPPRA Sindh", "province": "Sindh", "paged": False, "enabled": True,
     "url": "https://ppms.pprasindh.gov.pk/PPMS/public/portal/notice-inviting-tender"},
    {"name": "KPPRA", "province": "Khyber Pakhtunkhwa", "paged": False, "enabled": True,
     "url": "https://www.kppra.gov.pk/kppra/activetenders"},
    {"name": "Punjab PPRA", "province": "Punjab", "paged": False, "enabled": True,
     "url": "https://eproc.punjab.gov.pk/ActiveTenders.aspx"},
]


def parse_date(text):
    if not text:
        return None
    try:
        d = dparser.parse(text.strip(), dayfirst=True)
        if d.tzinfo is None:
            d = d.replace(tzinfo=PKT)
        return d.isoformat()
    except Exception:
        return None


def map_headers(cells):
    """Match column names to our fields by keyword."""
    m = {}
    for i, c in enumerate(cells):
        h = c.lower()
        if "tender no" in h or "ref" in h:
            m.setdefault("external_id", i)
        elif "closing" in h or "submission" in h or "last date" in h or "due" in h:
            m.setdefault("closing", i)
        elif "advertis" in h or "publish" in h or "posted" in h:
            m.setdefault("advertised", i)
        elif "organi" in h or "department" in h or "agency" in h or "authority" in h:
            m.setdefault("department", i)
        elif "status" in h:
            m.setdefault("status", i)
        elif "district" in h or "city" in h:
            m.setdefault("city", i)
        elif "detail" in h or "title" in h or "description" in h or "name" in h:
            m.setdefault("title", i)
    return m


def guess_city(org_text):
    m = re.search(r"([A-Za-z\.\-' ]+?)\s*-\s*Pakistan", org_text or "")
    if m:
        words = m.group(1).strip().split()
        return words[-1] if words else None
    return None


def first_line(cell):
    for line in cell.get_text("\n").split("\n"):
        line = line.strip()
        if line and "view invoice" not in line.lower():
            return line[:500]
    return None


def parse_page(html, src):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if not trs:
            continue
        head = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        cols = map_headers(head)
        if "title" not in cols or "closing" not in cols:
            continue
        if src["name"] not in _DEBUGGED:
            _DEBUGGED.add(src["name"])
            print(f"   DEBUG [{src['name']}] column headers: {head}")
            print(f"   DEBUG [{src['name']}] matched columns: {cols}")
            if len(trs) > 1:
                sample = [c.get_text(' ', strip=True)[:60] for c in trs[1].find_all('td')]
                print(f"   DEBUG [{src['name']}] first row: {sample}")
        for tr in trs[1:]:
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            get = lambda k: tds[cols[k]] if k in cols and cols[k] < len(tds) else None
            title = first_line(get("title")) if get("title") else None
            if not title:
                continue
            dept = get("department").get_text(" ", strip=True) if get("department") else None
            ext = get("external_id").get_text(" ", strip=True) if get("external_id") else None
            ext = re.sub(r"(?i)view invoice", "", ext or "").strip() or None
            closing = parse_date(get("closing").get_text(" ", strip=True)) if get("closing") else None
            if not ext:
                ext = hashlib.md5(f"{title}|{dept}|{closing}".encode()).hexdigest()[:16]
            link = None
            for a in tr.find_all("a", href=True):
                h = a["href"]
                if h.startswith(("javascript", "#")):
                    continue
                link = urljoin(src["url"], h)
                if "tender-details" in h or "detail" in h.lower():
                    break
            city = get("city").get_text(" ", strip=True) if get("city") else guess_city(dept)
            rows.append({
                "source": src["name"], "external_id": ext, "title": title,
                "department": dept, "city": city, "province": src["province"],
                "closing_date": closing,
                "advertised_date": parse_date(get("advertised").get_text(" ", strip=True)) if get("advertised") else None,
                "status": get("status").get_text(" ", strip=True) if get("status") else None,
                "detail_url": link, "document_url": link,
            })
    return rows


def scrape_source(src):
    all_rows, seen = [], set()
    pages = range(1, MAX_PAGES + 1) if src["paged"] else [1]
    for p in pages:
        url = src["url"] + (f"?page={p}" if src["paged"] else "")
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        rows = [x for x in parse_page(r.text, src) if x["external_id"] not in seen]
        if not rows:
            break
        for x in rows:
            seen.add(x["external_id"])
        all_rows += rows
    return all_rows


def save(rows):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/tenders?on_conflict=source,external_id"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    h = {"apikey": key, "Content-Type": "application/json",
         "Prefer": "resolution=merge-duplicates,return=minimal"}
    if key.startswith("eyJ"):   # old-style JWT keys also go in Authorization; new sb_secret keys do not
        h["Authorization"] = f"Bearer {key}"
    for i in range(0, len(rows), 100):
        r = requests.post(url, headers=h, json=rows[i:i + 100], timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"Supabase said {r.status_code}: {r.text[:300]}")


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    total, failed = 0, 0
    for src in SOURCES:
        if not src["enabled"]:
            continue
        try:
            rows = scrape_source(src)
            print(f"[{src['name']}] found {len(rows)} tenders")
            if rows and dry:
                print("   sample:", rows[0])
            elif rows:
                save(rows)
            if not rows:
                print(f"   WARNING: 0 tenders; page layout may have changed or needs JavaScript")
            total += len(rows)
        except Exception as e:
            failed += 1
            print(f"[{src['name']}] FAILED: {e}")
    print(f"Done. {total} tenders processed, {failed} source(s) failed.")
    if failed == len([s for s in SOURCES if s["enabled"]]):
        sys.exit(1)


if __name__ == "__main__":
    main()
