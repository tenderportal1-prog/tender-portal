"""
Tender scraper - Phase 2.
Reads public tender tables from Pakistani procurement portals and saves them
into your Supabase `tenders` table. Safe to run repeatedly (no duplicates).
"""
import os, re, sys, time, hashlib
from datetime import timezone, timedelta
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup
from dateutil import parser as dparser

PKT = timezone(timedelta(hours=5))
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TenderPortalBot/1.0)"}
MAX_PAGES = int(os.environ.get("MAX_PAGES", "60"))
_DEBUGGED = set()

# ---- Sources. Add or switch off portals here. ----
SOURCES = [
    {"name": "PPRA Federal", "province": "Federal", "paged": True, "enabled": True,
     "url": "https://epms.ppra.gov.pk/public/tenders/active-tenders"},
    # Experimental: these layouts are unverified, so each is isolated.
    {"name": "SPPRA Sindh", "province": "Sindh", "paged": False, "enabled": True, "insecure_ok": True,
     "url": "https://ppms.pprasindh.gov.pk/PPMS/public/portal/notice-inviting-tender"},
    {"name": "KPPRA", "province": "Khyber Pakhtunkhwa", "paged": False, "enabled": True,
     "url": "https://kp.gov.pk/tenders"},
    {"name": "Punjab PPRA", "province": "Punjab", "paged": False, "enabled": True,
     "url": "https://eproc.punjab.gov.pk/ActiveTenders.aspx"},
    # Layout unknown; only used by manual_scrape.py (you open the page yourself)
    {"name": "BPPRA Balochistan", "province": "Balochistan", "paged": False, "enabled": True,
     "manual_only": True, "parser": "bppra",
     "url": "https://bpptwo.vdc.services:5451/Tenders"},
]


def parse_date(text, eod=False):
    if not text:
        return None
    text = text.strip()
    for fuzzy in (False, True):
        try:
            d = dparser.parse(text, dayfirst=True, fuzzy=fuzzy)
            if eod and ":" not in text:   # date without a time: closes at end of that day
                d = d.replace(hour=23, minute=59)
            if d.tzinfo is None:
                d = d.replace(tzinfo=PKT)
            return d.isoformat()
        except Exception:
            continue
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



def parse_bppra(html, src):
    """Balochistan portal: each tender is a block of text starting with an id like TSE-2627101212873."""
    soup = BeautifulSoup(html, "html.parser")
    lines = [l.strip() for l in soup.get_text("\n").split("\n") if l.strip()]
    starts = [i for i, l in enumerate(lines) if re.match(r"^TSE-\d+", l)]
    rows = []
    skip = ("bidding document", "nit document", "boq document", "open", "closed", "e-bids only")
    for n, i in enumerate(starts):
        chunk = lines[i:(starts[n + 1] if n + 1 < len(starts) else len(lines))]
        ext = re.match(r"TSE-\d+", chunk[0]).group(0)
        body = [l for l in chunk[1:] if l.lower() not in skip]
        title = body[0][:500] if body else None
        if not title:
            continue
        category = next((l.strip("()") for l in body[1:3] if l.startswith("(")), None)

        def after(label):
            for k, l in enumerate(chunk):
                if l.lower().startswith(label):
                    rest = l.split(":", 1)[1].strip() if ":" in l else ""
                    if rest:
                        return rest
                    return chunk[k + 1] if k + 1 < len(chunk) else None
            return None

        dept_line = next((k for k, l in enumerate(chunk) if l.lower().startswith("department:")), None)
        agency = chunk[dept_line - 1] if dept_line else None
        dept = after("department:")
        district = after("district:")
        budget = next((l for l in chunk if l.startswith("Rs.")), None)
        department = ", ".join(x for x in (agency, dept) if x) or None
        rows.append({
            "source": src["name"], "external_id": ext, "title": title,
            "department": department, "city": district, "province": src["province"],
            "closing_date": parse_date(after("dead line")),
            "advertised_date": parse_date(after("publish")),
            "opening_date": parse_date(after("opening date")),
            "status": "Open", "item_type": category,
            "description": (f"Estimated budget: {budget}" if budget else None),
            "detail_url": None, "document_url": None,
        })
    return rows


def parse_page(html, src):
    if src.get("parser") == "bppra":
        return parse_bppra(html, src)
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
                print(f"   DEBUG [{src['name']}] row1 html: {str(trs[1])[:3500]}")
            if len(trs) > 2:
                print(f"   DEBUG [{src['name']}] row2 html: {str(trs[2])[:2500]}")
        for tr in trs[1:]:
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            get = lambda k: tds[cols[k]] if k in cols and cols[k] < len(tds) else None
            tcell = get("title")
            lines = [l.strip() for l in tcell.get_text("\n").split("\n") if l.strip()] if tcell else []
            lines = [l for l in lines if "view invoice" not in l.lower()]
            title = lines[0][:500] if lines else None
            if not title:
                continue
            item_type = lines[-2] if len(lines) >= 3 and len(lines[-2]) <= 40 else None
            middle = lines[1:-2] if item_type else lines[1:]
            description = " | ".join(middle)[:1000] or None
            dept = get("department").get_text(" ", strip=True) if get("department") else None
            ext = get("external_id").get_text(" ", strip=True) if get("external_id") else None
            ext = re.sub(r"(?i)view invoice", "", ext or "").strip()
            m_ext = re.match(r"TS\d+[A-Z]?", ext)
            ext = (m_ext.group(0) if m_ext else ext) or None
            closing = parse_date(get("closing").get_text(" ", strip=True), eod=True) if get("closing") else None
            if not ext:
                ext = hashlib.md5(f"{title}|{dept}|{closing}".encode()).hexdigest()[:16]
            link = None
            for a in tr.find_all("a", href=True):
                h = a["href"]
                if h.startswith(("javascript", "#")):
                    continue
                if link is None or "tender-details" in h or "detail" in h.lower() \
                        or "view details" in a.get_text(" ", strip=True).lower():
                    link = urljoin(src["url"], h)
                if "tender-details" in h or "view details" in a.get_text(" ", strip=True).lower():
                    break
            city = get("city").get_text(" ", strip=True) if get("city") else guess_city(dept)
            rows.append({
                "source": src["name"], "external_id": ext, "title": title,
                "department": dept, "city": city, "province": src["province"],
                "closing_date": closing,
                "advertised_date": parse_date(get("advertised").get_text(" ", strip=True)) if get("advertised") else None,
                "status": get("status").get_text(" ", strip=True) if get("status") else None,
                "item_type": item_type, "description": description,
                "detail_url": link, "document_url": link,
            })
    return rows


def scrape_source(src):
    all_rows, seen = [], set()
    pages = range(1, MAX_PAGES + 1) if src["paged"] else [1]
    for p in pages:
        url = src["url"] + (f"?page={p}" if src["paged"] else "")
        # Only for public pages whose own certificate is broken, and only when ALLOW_INSECURE_SSL=1
        verify = not (src.get("insecure_ok") and os.environ.get("ALLOW_INSECURE_SSL") == "1")
        r = requests.get(url, headers=HEADERS, timeout=(10, 40), verify=verify)
        time.sleep(0.3)
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
    only = [x.strip() for x in os.environ.get("ONLY_SOURCES", "").split(",") if x.strip()]
    total, failed, tried = 0, 0, 0
    for src in SOURCES:
        if not src["enabled"] or (only and src["name"] not in only):
            continue
        if src.get("manual_only") and src["name"] not in only:
            continue
        tried += 1
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
    if tried and failed == tried:
        sys.exit(1)


if __name__ == "__main__":
    main()
