#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OutSmart <-> Zoho CRM koppeling (alles-in-een, geen installatie nodig).

GEBRUIK (in Terminal):
    python3 koppeling.py test          -> test alleen de verbindingen
    python3 koppeling.py all --dry-run -> laat zien wat er ZOU gebeuren
    python3 koppeling.py all           -> echte synchronisatie (beide richtingen)

Losse onderdelen:
    python3 koppeling.py relations
    python3 koppeling.py materials
    python3 koppeling.py workorders

TIP: je hoeft niet naar de juiste map te navigeren. Typ in Terminal
"python3 " (met spatie), sleep dit bestand in het venster, typ " test",
en druk Enter.

LET OP: in dit bestand staan geheime sleutels. Houd het bestand prive.
"""

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# SSL-context: gebruik certifi als dat beschikbaar is (lost macOS-certificaatfouten op)
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CONTEXT = None

# ==========================================================================
# GEGEVENS  (hier staan je tokens en sleutels al ingevuld)
# ==========================================================================
def _load_dotenv():
    """Laad KEY=VALUE-regels uit een .env-bestand naast dit script (indien aanwezig)."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    except NameError:
        path = ".env"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# Gegevens komen uit omgevingsvariabelen (lokaal via .env-bestand, in GitHub via Secrets).
# Er staan GEEN geheimen meer in dit bestand, zodat het veilig naar GitHub kan.
CONFIG = {
    "OUTSMART_TOKEN": os.environ.get("OUTSMART_TOKEN", ""),
    "OUTSMART_SOFTWARE_TOKEN": os.environ.get("OUTSMART_SOFTWARE_TOKEN", ""),
    "OUTSMART_BASE_URL": os.environ.get("OUTSMART_BASE_URL", "https://app.out-smart.com/openapi/8"),
    "ZOHO_CLIENT_ID": os.environ.get("ZOHO_CLIENT_ID", ""),
    "ZOHO_CLIENT_SECRET": os.environ.get("ZOHO_CLIENT_SECRET", ""),
    "ZOHO_REFRESH_TOKEN": os.environ.get("ZOHO_REFRESH_TOKEN", ""),
    "ZOHO_DATACENTER": os.environ.get("ZOHO_DATACENTER", "eu"),
}
# ==========================================================================

# Werkbon-statuscodes -> leesbare omschrijving (uit OutSmart, Werk Statussen)
WORKSTATUS_LABELS = {
    "34334": "Backoffice - Terug queue - Basicall",
    "344534": "Backoffice - Geen route beschikbaar",
    "3623681": "KM - Deadline - Lead later opvolgen",
    "435453": "Speciale werkbon - Expertise vereist",
    "513513": "Backoffice - Administratief verwerkt",
    "58119": "KM - Diverse",
    "58124": "KM - Offerte aanvraag",
    "581241": "KM - Werkzaamheden niet uitgevoerd",
    "5812411": "Backoffice - Werkbon geannuleerd door klant",
    "5812441": "KM - Werkzaamheden uitgevoerd",
    "5812477": "KM - Opnieuw inplannen",
    "58128": "KM - Klant heeft vaste partij",
    "58156": "Massa - Factuur direct verzenden naar klant - Management",
    "58157": "Massa - Offerte verzenden naar klant - Management",
    "58185": "KM - Hold (wacht met vervolgactie)",
    "KM - Bedrijf (tijdelijk) gesloten": "KM - Bedrijf (tijdelijk) gesloten",
}

# De status die betekent 'werkzaamheden uitgevoerd' (voor Prospect/Vaste-klant indeling)
WORKSTATUS_DONE = "5812441"


def workstatus_label(code):
    if code is None:
        return ""
    return WORKSTATUS_LABELS.get(str(code), str(code))


DATACENTERS = {
    "eu": {"accounts": "https://accounts.zoho.eu", "api": "https://www.zohoapis.eu"},
    "com": {"accounts": "https://accounts.zoho.com", "api": "https://www.zohoapis.com"},
    "in": {"accounts": "https://accounts.zoho.in", "api": "https://www.zohoapis.in"},
    "au": {"accounts": "https://accounts.zoho.com.au", "api": "https://www.zohoapis.com.au"},
}


def http(method, url, headers=None, body=None, form=None, timeout=60):
    data = None
    headers = dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return 200, _maybe_json(raw)
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise SystemExit("Netwerkfout bij %s: %s" % (url, e))


def _maybe_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


class OutSmart(object):
    def __init__(self, cfg):
        self.base = cfg["OUTSMART_BASE_URL"].rstrip("/")
        self.token = cfg["OUTSMART_TOKEN"]
        self.software = cfg["OUTSMART_SOFTWARE_TOKEN"]

    def _url(self, endpoint, extra=None):
        params = {"token": self.token, "software_token": self.software}
        if extra:
            params.update(extra)
        return "%s/%s?%s" % (self.base, endpoint.strip("/"), urllib.parse.urlencode(params))

    def get(self, endpoint, extra=None, timeout=60):
        _, data = http("GET", self._url(endpoint, extra),
                       headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                       timeout=timeout)
        return self._unwrap(data)

    def post(self, endpoint, payload, extra=None):
        _, data = http("POST", self._url(endpoint, extra),
                       headers={"Authorization": "Bearer " + self.token}, body=payload)
        return self._unwrap(data)

    @staticmethod
    def _unwrap(data):
        if not isinstance(data, dict):
            raise SystemExit("Onverwacht OutSmart-antwoord: %s" % str(data)[:200])
        code = int(data.get("code", -1))
        if code != 200:
            raise SystemExit("OutSmart-fout %s: %s" % (code, data.get("messages")))
        return data.get("response")

    def get_relations(self):
        return self.get("relations") or []

    def upsert_relations(self, rows):
        return self.post("relations", rows)

    def get_materials(self):
        return self.get("materials") or []

    def upsert_materials(self, rows):
        return self.post("materials", rows)

    def get_hourtypes(self):
        return self.get("hourtypes") or []

    def get_workorders(self):
        return self.get("GetWorkorders", {"status": "Compleet", "update_status": "false"}, timeout=300) or []

    def get_all_workorders(self, statuses=None):
        # GetWorkorders vereist een status-parameter; per status ophalen en samenvoegen.
        if statuses is None:
            # Officiele OutSmart-werkbonstatussen (zie OpenAPI-docs)
            statuses = ["Klaargezet", "Opgehaald", "Compleet", "Afgehandeld"]
        seen = {}
        for st in statuses:
            try:
                rows = self.get("GetWorkorders", {"status": st, "update_status": "false"}, timeout=900) or []
            except SystemExit as e:
                log("   status '%s' niet bruikbaar: %s" % (st, str(e)[:120]))
                continue
            new = 0
            for w in rows:
                k = str(w.get("id") or w.get("OrderNr") or w.get("WorksheetCode") or w.get("WorkorderNo") or id(w))
                if k not in seen:
                    seen[k] = w
                    new += 1
            log("   status '%s': %d opgehaald (%d nieuw)" % (st, len(rows), new))
        return list(seen.values())

    def get_quotations(self):
        return self.get("quotations", {}, timeout=300) or []

    def get_employees(self):
        return self.get("employees", {}, timeout=120) or []

    def raw_get(self, endpoint, extra=None, timeout=20):
        """GET zonder foutafhandeling; geeft het volledige antwoord terug."""
        _, data = http("GET", self._url(endpoint, extra),
                       headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
                       timeout=timeout)
        return data

    def raw_post(self, endpoint, payload):
        _, data = http("POST", self._url(endpoint),
                       headers={"Authorization": "Bearer " + self.token}, body=payload, timeout=20)
        return data


class Zoho(object):
    def __init__(self, cfg):
        dc = DATACENTERS.get(cfg["ZOHO_DATACENTER"], DATACENTERS["eu"])
        self.accounts = dc["accounts"]
        self.api = dc["api"]
        self.client_id = cfg["ZOHO_CLIENT_ID"]
        self.client_secret = cfg["ZOHO_CLIENT_SECRET"]
        self.refresh_token = cfg["ZOHO_REFRESH_TOKEN"]
        self._access = None
        self._expiry = 0.0

    def _token(self):
        if self._access and time.time() < self._expiry:
            return self._access
        _, data = http("POST", self.accounts + "/oauth/v2/token", form={
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        })
        if not isinstance(data, dict) or "access_token" not in data:
            raise SystemExit("Zoho token-verversing mislukt: %s" % data)
        self._access = data["access_token"]
        self._expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        return self._access

    def _headers(self):
        return {"Authorization": "Zoho-oauthtoken " + self._token()}

    def get_records(self, module, fields=None):
        out, page = [], 1
        while True:
            params = {"per_page": 200, "page": page}
            if fields:
                params["fields"] = fields
            url = "%s/crm/v8/%s?%s" % (self.api, module, urllib.parse.urlencode(params))
            status, data = http("GET", url, headers=self._headers())
            if status == 204 or not isinstance(data, dict):
                break
            out.extend(data.get("data", []))
            if not data.get("info", {}).get("more_records"):
                break
            page += 1
        return out

    def upsert(self, module, records, dup_fields):
        results = []
        for i in range(0, len(records), 100):
            _, data = http("POST", "%s/crm/v8/%s/upsert" % (self.api, module),
                           headers=self._headers(),
                           body={"data": records[i:i + 100], "duplicate_check_fields": dup_fields})
            if isinstance(data, dict):
                results.extend(data.get("data", []))
            else:
                raise SystemExit("Zoho-fout: %s" % data)
        return results

    def account_id_by_debtor(self, debtor):
        """Zoek het Zoho account-id bij een debiteurnummer via de search-API."""
        crit = "(Debiteurnummer_Outsmart:equals:%s)" % debtor
        url = "%s/crm/v8/Accounts/search?%s" % (self.api, urllib.parse.urlencode({"criteria": crit}))
        status, data = http("GET", url, headers=self._headers())
        if status == 204 or not isinstance(data, dict):
            return None
        recs = data.get("data") or []
        return recs[0]["id"] if recs else None

    def account_lookup_for(self, debtors):
        """Bouw {debiteurnummer: account_id} voor alleen de opgegeven debiteurnummers."""
        m = {}
        for d in sorted(set(x for x in debtors if x)):
            aid = self.account_id_by_debtor(d)
            if aid:
                m[d] = aid
        return m

    def product_map(self):
        """Product_Code -> Zoho product id."""
        m = {}
        for p in self.get_records("Products", "Product_Code"):
            code = p.get("Product_Code")
            if code:
                m[str(code)] = p["id"]
        return m

    def create_raw(self, module, record):
        """Maak 1 record aan; geeft het volledige (ruwe) Zoho-antwoord terug."""
        status, data = http("POST", "%s/crm/v8/%s" % (self.api, module),
                            headers=self._headers(), body={"data": [record]})
        return data

    def notes_for(self, module, rid):
        url = "%s/crm/v8/%s/%s/Notes" % (self.api, module, rid)
        status, data = http("GET", url, headers=self._headers())
        if status == 204 or not isinstance(data, dict):
            return []
        return data.get("data", []) or []

    def create_note(self, module, parent_id, title, content):
        body = {"data": [{
            "Note_Title": (title or "")[:120],
            "Note_Content": (content or "")[:31000],
            "Parent_Id": {"id": parent_id, "module": {"api_name": module}},
        }]}
        status, data = http("POST", "%s/crm/v8/Notes" % self.api, headers=self._headers(), body=body)
        return data

    def create_notes_bulk(self, items):
        """items: lijst van (parent_id, module, title, content). Maakt 100 notities per call."""
        results = []
        for i in range(0, len(items), 100):
            batch = items[i:i + 100]
            data = [{
                "Note_Title": (t or "")[:120],
                "Note_Content": (c or "")[:31000],
                "Parent_Id": {"id": pid, "module": {"api_name": mod}},
            } for (pid, mod, t, c) in batch]
            _, resp = http("POST", "%s/crm/v8/Notes" % self.api, headers=self._headers(), body={"data": data})
            if isinstance(resp, dict):
                results.extend(resp.get("data", []))
        return results

    def update_note(self, note_id, title, content):
        body = {"data": [{"Note_Title": (title or "")[:120], "Note_Content": (content or "")[:31000]}]}
        status, data = http("PUT", "%s/crm/v8/Notes/%s" % (self.api, note_id), headers=self._headers(), body=body)
        return data

    def insert(self, module, records):
        results = []
        for i in range(0, len(records), 100):
            _, data = http("POST", "%s/crm/v8/%s" % (self.api, module),
                           headers=self._headers(), body={"data": records[i:i + 100]})
            if isinstance(data, dict):
                results.extend(data.get("data", []))
            else:
                raise SystemExit("Zoho-fout: %s" % data)
        return results

    def all_ids(self, module):
        # page_token-paginatie (Zoho's page-nummers stoppen bij 2000 records)
        ids = []
        params = {"fields": "id", "per_page": 200}
        while True:
            url = "%s/crm/v8/%s?%s" % (self.api, module, urllib.parse.urlencode(params))
            status, data = http("GET", url, headers=self._headers())
            if status == 204 or not isinstance(data, dict):
                break
            ids.extend(r["id"] for r in data.get("data", []) if r.get("id"))
            info = data.get("info", {}) or {}
            if not info.get("more_records"):
                break
            token = info.get("next_page_token")
            params = {"fields": "id", "per_page": 200}
            if token:
                params["page_token"] = token
            else:
                break
        return ids

    def delete_ids(self, module, ids):
        deleted = 0
        for i in range(0, len(ids), 100):
            batch = ids[i:i + 100]
            url = "%s/crm/v8/%s?ids=%s" % (self.api, module, ",".join(batch))
            _, data = http("DELETE", url, headers=self._headers())
            if isinstance(data, dict):
                deleted += sum(1 for r in data.get("data", []) if r.get("status") == "success")
        return deleted

    def records_index(self, module, field):
        """Bouw {veldwaarde: [ids]} voor ALLE records (met page_token-paginatie)."""
        idx = {}
        params = {"fields": "id,%s" % field, "per_page": 200}
        while True:
            url = "%s/crm/v8/%s?%s" % (self.api, module, urllib.parse.urlencode(params))
            status, data = http("GET", url, headers=self._headers())
            if status == 204 or not isinstance(data, dict):
                break
            for r in data.get("data", []):
                v = r.get(field)
                if v:
                    idx.setdefault(str(v), []).append(r["id"])
            info = data.get("info", {}) or {}
            if not info.get("more_records"):
                break
            token = info.get("next_page_token")
            params = {"fields": "id,%s" % field, "per_page": 200}
            if token:
                params["page_token"] = token
            else:
                break
        return idx

    def update(self, module, records):
        """Update bestaande records; elk record moet een 'id' bevatten."""
        results = []
        for i in range(0, len(records), 100):
            _, data = http("PUT", "%s/crm/v8/%s" % (self.api, module),
                           headers=self._headers(), body={"data": records[i:i + 100]})
            if isinstance(data, dict):
                results.extend(data.get("data", []))
            else:
                raise SystemExit("Zoho-fout: %s" % data)
        return results


def _f(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def _clip(v, n):
    """Maak schoon en kort in tot n tekens (herstelt te lange / rare waarden)."""
    if v is None:
        return None
    s = str(v).replace("\r", " ").replace("\n", " ").strip()
    return s[:n] if s else None


def relation_to_account(r):
    street = " ".join(p for p in [r.get("street", ""), r.get("house_number", "")] if p).strip()
    name = _clip(r.get("name"), 200) or _clip(r.get("debtor_number"), 200) or "Onbekend"
    return {
        "Account_Name": name,
        "Debiteurnummer_Outsmart": _clip(r.get("debtor_number"), 255),
        "Phone": _clip(r.get("phone_number"), 30),
        "Billing_Street": _clip(street, 250),
        "Billing_Code": _clip(r.get("postal_code"), 30),
        "Billing_City": _clip(r.get("city"), 100),
        "Description": _clip(r.get("remark"), 2000),
    }


def account_to_relation(a):
    if not a.get("Debiteurnummer_Outsmart") or not a.get("Account_Name"):
        return None
    return {
        "name": a.get("Account_Name"),
        "debtor_number": str(a.get("Debiteurnummer_Outsmart")),
        "phone_number": a.get("Phone") or "",
        "street": a.get("Billing_Street") or "",
        "postal_code": a.get("Billing_Code") or "",
        "city": a.get("Billing_City") or "",
        "remark": a.get("Description") or "",
    }


def material_to_product(m):
    return {
        "Product_Name": m.get("description") or m.get("code"),
        "Product_Code": m.get("code"),
        "Unit_Price": _f(m.get("price")),
        "Usage_Unit": m.get("unit"),
        "Description": m.get("description"),
    }


def product_to_material(p):
    if not p.get("Product_Code"):
        return None
    price = p.get("Unit_Price")
    return {
        "code": str(p.get("Product_Code")),
        "description": p.get("Product_Name") or str(p.get("Product_Code")),
        "price": "" if price is None else str(price),
        "unit": p.get("Usage_Unit") or "stuks",
    }


def workorder_to_deal(w, lookup):
    wid = w.get("id") or w.get("workorder_id") or w.get("number")
    debtor = str(w.get("debtor_number") or "")
    deal = {
        "Deal_Name": ("Werkbon %s" % wid) if wid else "Werkbon",
        "Stage": "Qualification",
        "Description": w.get("description") or w.get("remark") or "",
    }
    if debtor in lookup:
        deal["Account_Name"] = {"id": lookup[debtor]}
    amt = _f(w.get("total") or w.get("amount"))
    if amt is not None:
        deal["Amount"] = amt
    return deal


def employee_name(emp):
    fn = (emp.get("firstname") or "").strip()
    ln = (emp.get("lastname") or "").strip()
    num = (emp.get("number") or "").strip()
    if ln and ln != num:
        return ("%s %s" % (fn, ln)).strip()
    return fn or num


def build_employee_map(os_c):
    m = {}
    try:
        for e in os_c.get_employees():
            num = str(e.get("number") or "").strip()
            if num:
                m[num] = employee_name(e)
    except SystemExit:
        pass
    return m


def build_logboek(w, emp_map=None):
    """Bouwt een leesbaar logboek uit statuswijzigingen, notities, foto's en documenten."""
    emp_map = emp_map or {}

    def who(u):
        u = str(u or "").strip()
        return emp_map.get(u, u)

    parts = []
    changes = w.get("StatusChanges") or []
    if changes:
        parts.append("== Statuswijzigingen ==")
        for c in changes:
            st = workstatus_label(c.get("status"))
            parts.append("%s  -  %s  (%s)" % (c.get("timestamp", ""), st, who(c.get("user"))))

    def add(label, val):
        val = (val or "").strip()
        if val:
            parts.append("")
            parts.append("== %s ==" % label)
            parts.append(val)

    add("Korte omschrijving", w.get("ShortWorkDescription"))
    add("Commentaar", w.get("Comment"))
    add("Interne omschrijving", w.get("InternalWorkDescription"))
    add("Klantopmerking", w.get("CustomerRemark"))
    photos = w.get("Photos") or []
    if photos:
        parts.append("")
        parts.append("== Foto's ==")
        for p in photos:
            if isinstance(p, dict):
                url = p.get("image") or p.get("url") or ""
                title = (p.get("title") or "").strip()
                parts.append(("%s %s" % (title, url)).strip())
            else:
                parts.append(str(p))
    links = []
    if w.get("PdfUrl"):
        links.append("PDF: " + w["PdfUrl"])
    if w.get("WordUrl"):
        links.append("Word: " + w["WordUrl"])
    if w.get("SignatureUrl"):
        links.append("Handtekening: " + w["SignatureUrl"])
    if links:
        parts.append("")
        parts.append("== Documenten ==")
        parts.extend(links)
    # Volledige dump van alle overige werkbon-velden
    skip = {"Materials", "StatusChanges", "Photos", "Workperiods", "Documents", "Forms",
            "WorkObjects", "Employees", "Signature", "SignatureUrl", "PdfUrl", "WordUrl",
            "WorkDescription", "ShortWorkDescription", "Comment", "InternalWorkDescription",
            "CustomerRemark"}
    dump = []
    for k in sorted(w.keys()):
        if k in skip:
            continue
        v = w.get(k)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
        label = "Werkbon status (code)" if k == "WorkStatus" else k
        dump.append("%s: %s" % (label, str(v)[:500]))
    if dump:
        parts.append("")
        parts.append("== Alle werkbon-velden ==")
        parts.extend(dump)
    # Materialen als leesbare regels
    mats = w.get("Materials") or []
    if mats:
        parts.append("")
        parts.append("== Materialen ==")
        for m in mats:
            parts.append("%s x %s (%s) - EUR %s" % (
                m.get("MaterialNr", ""), m.get("MaterialName", ""),
                m.get("MaterialCode", ""), m.get("MaterialTotalPrice", "")))
    return "\n".join(parts)[:31000]


def workorder_to_salesorder(w, account_lookup, product_map, emp_map=None, quote_map=None, generic_pid=None):
    """OutSmart werkbon -> Zoho Verkooporder (Sales_Orders) met orderregels.
    Geeft None terug als er geen regels en geen algemeen product zijn."""
    emp_map = emp_map or {}
    quote_map = quote_map or {}
    lines = []
    for mat in (w.get("Materials") or []):
        code = str(mat.get("MaterialCode") or "").strip()
        pid = product_map.get(code)
        if not pid:
            continue
        qty = _f(mat.get("MaterialNr")) or 1
        price = _f(mat.get("MaterialPrice")) or 0
        lines.append({
            "Product_Name": {"id": pid},
            "Quantity": qty,
            "List_Price": price,
        })
    debtor = str(w.get("CustomerDebtorNr") or "").strip()
    if not debtor:
        return None  # geen debiteurnummer -> niet invoeren
    if not lines:
        if generic_pid:
            lines = [{"Product_Name": {"id": generic_pid}, "Quantity": 1, "List_Price": 0}]
        else:
            return None
    so = {
        "Subject": _clip(w.get("OrderNr") or w.get("WorkorderNo") or w.get("WorksheetCode"), 250) or "Werkbon",
        "Werkbonnummer_Outsmart": _clip(w.get("OrderNr") or w.get("WorksheetCode"), 255),
        "Debiteurnummer": _clip(debtor, 255),
        "Klant_naam": _clip(w.get("CustomerName"), 255),
        "Status": _clip(workstatus_label(w.get("WorkStatus")), 250),
        "Werkstatus_Outsmart_meest_recente_bezoek": _clip(workstatus_label(w.get("WorkStatus")), 250),
        "Type_werk": _clip(w.get("TypeOfWork"), 255),
        "Description": _clip(w.get("WorkDescription"), 2000),
        "Ordered_Items": lines,
    }
    # Offertenummer, afgeleid uit de offertes (verwijzen naar de werkbon)
    qn = quote_map.get(str(w.get("id") or "")) or quote_map.get(str(w.get("OrderNr") or ""))
    if qn:
        so["Offerte_nummer"] = _clip(qn, 255)
    # Factuuradres (standaard Zoho-velden)
    inv_street = " ".join(p for p in [w.get("CustomerStreetInvoice", ""), w.get("CustomerStreetNoInvoice", "")] if p).strip()
    if inv_street:
        so["Billing_Street"] = _clip(inv_street, 250)
    if w.get("CustomerCityInvoice"):
        so["Billing_City"] = _clip(w["CustomerCityInvoice"], 100)
    if w.get("CustomerZIPInvoice"):
        so["Billing_Code"] = _clip(w["CustomerZIPInvoice"], 30)
    if w.get("CustomerCountryInvoice"):
        so["Billing_Country"] = _clip(w["CustomerCountryInvoice"], 100)
    # Verzendadres (standaard Zoho-velden)
    ship_street = " ".join(p for p in [w.get("CustomerStreet", ""), w.get("CustomerStreetNo", "")] if p).strip()
    if ship_street:
        so["Shipping_Street"] = _clip(ship_street, 250)
    if w.get("CustomerCity"):
        so["Shipping_City"] = _clip(w["CustomerCity"], 100)
    if w.get("CustomerZIP"):
        so["Shipping_Code"] = _clip(w["CustomerZIP"], 30)
    if w.get("CustomerCountry"):
        so["Shipping_Country"] = _clip(w["CustomerCountry"], 100)
    if debtor in account_lookup:
        so["Account_Name"] = {"id": account_lookup[debtor]}
    return so


def debug_dump(os_c):
    """Schrijf een volledige werkbon + werknemerslijst naar bestanden (voor analyse)."""
    wos = os_c.get_workorders()
    # neem de werkbon met de meeste velden ingevuld (meest complete)
    best = max(wos, key=lambda w: sum(1 for v in w.values() if v not in (None, "", [], {}))) if wos else {}
    p1 = os.path.join(SCRIPT_DIR, "debug_workorder.json")
    with open(p1, "w", encoding="utf-8") as f:
        json.dump(best, f, ensure_ascii=False, indent=2)
    emps = []
    try:
        emps = os_c.get_employees()
    except SystemExit as e:
        log("Werknemers: fout - %s" % e)
    p2 = os.path.join(SCRIPT_DIR, "debug_employees.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump(emps[:10], f, ensure_ascii=False, indent=2)
    log("Geschreven:")
    log("  %s  (%d velden)" % (p1, len(best)))
    log("  %s  (%d werknemers, eerste 10 opgeslagen)" % (p2, len(emps)))


def probe_invoices(os_c):
    candidates = ["GetInvoices", "invoices", "GetInvoice", "invoice",
                  "facturen", "factuur", "billing", "bills", "GetBilling"]
    for ep in candidates:
        g = {}
        try:
            g = os_c.raw_get(ep, {}, timeout=30) or {}
        except Exception as e:  # noqa: BLE001
            log("  /%-14s -> %s" % (ep, type(e).__name__))
            continue
        code = g.get("code") if isinstance(g, dict) else "?"
        resp = g.get("response") if isinstance(g, dict) else None
        n = len(resp) if isinstance(resp, list) else "-"
        mark = "  <-- BESTAAT" if code not in (1001, "TIMEOUT", "?") else ""
        log("  /%-14s GET code %s (records %s)%s" % (ep, code, n, mark))
        if code == 200 and isinstance(resp, list) and resp:
            log("     Velden: %s" % ", ".join(sorted(resp[0].keys())))
            log(json.dumps(resp[0], ensure_ascii=False, indent=2)[:1500])
            return


def dump_employees(os_c):
    try:
        emps = os_c.get_employees()
    except SystemExit as e:
        log("Fout bij ophalen werknemers: %s" % e)
        return
    log("OutSmart: %d werknemers" % len(emps))
    if emps:
        log("Velden eerste werknemer: %s" % ", ".join(sorted(emps[0].keys())))
        log(json.dumps(emps[0], ensure_ascii=False, indent=2)[:1200])


def test_product(zo):
    log("1 testproduct aanmaken in Zoho...")
    r = zo.create_raw("Products", {"Product_Name": "TEST product koppeling", "Product_Code": "TEST-KPL-001"})
    log(json.dumps(r, ensure_ascii=False, indent=2)[:1500])


def test_salesorder(os_c, zo):
    log("Producten ophalen uit Zoho...")
    pm = zo.product_map()
    log("   %d producten gevonden." % len(pm))
    emp_map = build_employee_map(os_c)
    log("Werkbonnen ophalen...")
    wos = os_c.get_workorders()
    am = zo.account_lookup_for(str(w.get("CustomerDebtorNr") or "").strip() for w in wos)
    log("   %d klanten gekoppeld via debiteurnummer." % len(am))
    for w in wos:
        so = workorder_to_salesorder(w, am, pm, emp_map)
        if so:
            log("Test-verkooporder voor werkbon %s (%d regels):" % (w.get("OrderNr"), len(so["Ordered_Items"])))
            log(json.dumps(so, ensure_ascii=False, indent=2)[:1200])
            log("")
            log("Zoho-antwoord:")
            log(json.dumps(zo.create_raw("Sales_Orders", so), ensure_ascii=False, indent=2)[:1500])
            return
    log("Geen werkbon met koppelbare regels gevonden (staan de materialen al in Producten?).")


def log(msg):
    print(msg)
    sys.stdout.flush()


def summarize(label, results):
    ok = sum(1 for r in results if r.get("status") == "success")
    log("   %s: %d gelukt, %d mislukt" % (label, ok, len(results) - ok))
    for r in results:
        if r.get("status") != "success":
            log("      ! %s" % (r.get("message") or r))


def report_failures(label, results, raws):
    """Schrijf mislukte records naar een CSV en toon een overzicht per oorzaak."""
    ok, fails = 0, []
    for res, raw in zip(results, raws):
        if res.get("status") == "success":
            ok += 1
        else:
            det = res.get("details") or {}
            fails.append({
                "debiteurnummer": raw.get("debtor_number") or "",
                "naam": (raw.get("name") or "").replace(";", ","),
                "reden": res.get("message") or res.get("code") or "",
                "veld": det.get("api_name") or "",
            })
    log("   Zoho %s: %d gelukt, %d mislukt" % (label, ok, len(fails)))
    if not fails:
        return
    # Overzicht per (reden, veld)
    counts = {}
    for f in fails:
        key = (f["reden"], f["veld"])
        counts[key] = counts.get(key, 0) + 1
    log("   Oorzaken:")
    for (reden, veld), n in sorted(counts.items(), key=lambda x: -x[1]):
        log("      %d x  %s%s" % (n, reden, (" (veld: %s)" % veld) if veld else ""))
    # Schrijf CSV naast het script
    path = os.path.join(SCRIPT_DIR, "mislukte_%s.csv" % label)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("debiteurnummer;naam;reden;veld\n")
        for f in fails:
            fh.write("%s;%s;%s;%s\n" % (f["debiteurnummer"], f["naam"], f["reden"], f["veld"]))
    log("   Details opgeslagen in: %s" % path)


def sync_relations(os_c, zo, direction, dry):
    if direction in ("outsmart-to-zoho", "both"):
        rels = os_c.get_relations()
        log("OutSmart: %d relaties opgehaald" % len(rels))
        pairs = [(relation_to_account(r), r) for r in rels if r.get("debtor_number")]
        accs = [p[0] for p in pairs]
        raws = [p[1] for p in pairs]
        if dry:
            log("   [proef] zou %d accounts naar Zoho schrijven" % len(accs))
        elif accs:
            results = zo.upsert("Accounts", accs, ["Debiteurnummer_Outsmart"])
            # Records die alleen op het telefoonnummer struikelen: opnieuw
            # proberen zonder Phone, met het nummer bewaard in Opmerking.
            retry_idx = [i for i, res in enumerate(results)
                         if res.get("status") != "success"
                         and (res.get("details") or {}).get("api_name") == "Phone"]
            if retry_idx:
                retry_accs = []
                for i in retry_idx:
                    a = dict(accs[i])
                    ph = a.pop("Phone", None)
                    if ph:
                        note = (a.get("Description") or "").strip()
                        a["Description"] = (note + (" | " if note else "") + "Tel (OutSmart): " + ph)[:2000]
                    retry_accs.append(a)
                retry_results = zo.upsert("Accounts", retry_accs, ["Debiteurnummer_Outsmart"])
                for j, i in enumerate(retry_idx):
                    results[i] = retry_results[j]
                log("   %d records opnieuw geladen zonder telefoonveld (nummer in Opmerking gezet)" % len(retry_idx))
            report_failures("relaties", results, raws)
    if direction in ("zoho-to-outsmart", "both"):
        accs = zo.get_records("Accounts",
            "Account_Name,Debiteurnummer_Outsmart,Phone,Billing_Street,Billing_Code,Billing_City,Description")
        log("Zoho: %d accounts opgehaald" % len(accs))
        rels = [x for x in (account_to_relation(a) for a in accs) if x]
        if dry:
            log("   [proef] zou %d relaties naar OutSmart schrijven" % len(rels))
        elif rels:
            log("   OutSmart: %s relaties gesynct" % os_c.upsert_relations(rels))


def sync_materials(os_c, zo, direction, dry):
    if direction in ("outsmart-to-zoho", "both"):
        mats = os_c.get_materials()
        log("OutSmart: %d materialen opgehaald" % len(mats))
        prods = [material_to_product(m) for m in mats if m.get("code")]
        if dry:
            log("   [proef] zou %d producten naar Zoho schrijven" % len(prods))
        elif prods:
            summarize("Zoho Products", zo.upsert("Products", prods, ["Product_Code"]))
    if direction in ("zoho-to-outsmart", "both"):
        prods = zo.get_records("Products", "Product_Name,Product_Code,Unit_Price,Usage_Unit,Description")
        log("Zoho: %d producten opgehaald" % len(prods))
        mats = [x for x in (product_to_material(p) for p in prods) if x]
        if dry:
            log("   [proef] zou %d materialen naar OutSmart schrijven" % len(mats))
        elif mats:
            log("   OutSmart: %s materialen gesynct" % os_c.upsert_materials(mats))


def _wo_date(w):
    """Retourneer werkbon-datum als 'YYYY-MM-DD' (uit WorkDate dd-mm-yyyy, anders CreationDate)."""
    d = str(w.get("WorkDate") or "").strip()
    if len(d) == 10 and d[2] == "-" and d[5] == "-":
        dd, mm, yy = d.split("-")
        return "%s-%s-%s" % (yy, mm, dd)
    c = str(w.get("CreationDate") or w.get("DateCreated") or "").strip()
    if len(c) >= 10 and c[4] == "-":
        return c[:10]
    if len(c) == 10 and c[2] == "-" and c[5] == "-":
        dd, mm, yy = c.split("-")
        return "%s-%s-%s" % (yy, mm, dd)
    return ""


def sync_workorders(os_c, zo, dry, all_statuses=False, until=None):
    log("Producten ophalen uit Zoho...")
    pm = zo.product_map()
    log("   %d producten gevonden." % len(pm))
    log("Werknemers ophalen uit OutSmart...")
    emp_map = build_employee_map(os_c)
    log("   %d werknemers." % len(emp_map))
    if all_statuses:
        wos = os_c.get_all_workorders()
        log("OutSmart: %d werkbonnen opgehaald (alle statussen)" % len(wos))
        if until:
            before = len(wos)
            wos = [w for w in wos if _wo_date(w) and _wo_date(w) <= until]
            log("   %d t/m %s (van %d, %d zonder/na datum overgeslagen)"
                % (len(wos), until, before, before - len(wos)))
    else:
        wos = os_c.get_workorders()
        log("OutSmart: %d voltooide werkbonnen opgehaald" % len(wos))
    log("Klanten indexeren op debiteurnummer...")
    am = {d: ids[0] for d, ids in zo.records_index("Accounts", "Debiteurnummer_Outsmart").items()}
    log("   %d klanten geindexeerd." % len(am))
    log("Offertes ophalen (voor offertenummer-koppeling)...")
    quote_map = {}
    for q in os_c.get_quotations():
        num = q.get("quo_number_formatted")
        if not num:
            continue
        if q.get("quo_worksheet_id"):
            quote_map[str(q["quo_worksheet_id"])] = num
        if q.get("quo_reference"):
            quote_map[str(q["quo_reference"])] = num
    log("   %d offerte-verwijzingen." % len(quote_map))
    # Zorg dat alle materiaalcodes uit de werkbonnen als product bestaan (anders aanmaken)
    code_name = {}
    for w in wos:
        for m in (w.get("Materials") or []):
            c = str(m.get("MaterialCode") or "").strip()
            if c and c not in code_name:
                code_name[c] = m.get("MaterialName") or c
    missing = [c for c in code_name if c not in pm]
    if missing:
        log("   %d ontbrekende producten aanmaken..." % len(missing))
        for c in missing:
            r = zo.create_raw("Products", {"Product_Name": _clip(code_name[c], 120) or c, "Product_Code": c})
            pid = ((r.get("data") or [{}])[0].get("details") or {}).get("id") if isinstance(r, dict) else None
            if pid:
                pm[c] = pid
    # Algemeen product 'Werkzaamheden' (lege waarde) voor werkbonnen zonder materiaal
    GEN = "OUTSMART-WERKZAAMHEDEN"
    generic_pid = pm.get(GEN)
    if not generic_pid:
        r = zo.create_raw("Products", {"Product_Name": "Werkzaamheden", "Product_Code": GEN})
        generic_pid = ((r.get("data") or [{}])[0].get("details") or {}).get("id") if isinstance(r, dict) else None
        if generic_pid:
            pm[GEN] = generic_pid
    pairs, skipped = [], []
    for w in wos:
        so = workorder_to_salesorder(w, am, pm, emp_map, quote_map, generic_pid)
        if so:
            pairs.append((so, w))
        else:
            skipped.append(w.get("OrderNr") or w.get("id"))
    log("   %d werkbonnen met koppelbare regels; %d overgeslagen (geen/onbekende materiaalregels)"
        % (len(pairs), len(skipped)))
    if skipped:
        log("   Overgeslagen: %s" % ", ".join(str(s) for s in skipped))
    if dry:
        log("   [proef] zou %d verkooporders naar Zoho schrijven" % len(pairs))
        return
    if not pairs:
        return
    # Betrouwbare ontdubbeling: bestaande records opzoeken op werkbonnummer en updaten via id;
    # alleen echt nieuwe werkbonnen invoegen. (Zoho's upsert op een niet-uniek veld is onbetrouwbaar.)
    log("Bestaande verkooporders indexeren op werkbonnummer...")
    existing_idx = zo.records_index("Sales_Orders", "Werkbonnummer_Outsmart")
    log("   %d bestaande werkbonnummers in Zoho." % len(existing_idx))
    to_update, to_insert = [], []
    for so, w in pairs:
        key = str(so.get("Werkbonnummer_Outsmart") or "")
        ids = existing_idx.get(key) if key else None
        if ids:
            rec = dict(so)
            rec["id"] = ids[0]
            to_update.append((rec, w, ids[0]))
        else:
            to_insert.append((so, w))
    log("   %d bijwerken, %d nieuw invoegen" % (len(to_update), len(to_insert)))
    results = []  # (result, werkbon)
    if to_update:
        r = zo.update("Sales_Orders", [x[0] for x in to_update])
        for res, (_, w, rid) in zip(r, to_update):
            results.append((res, w))
    new_notes = []  # notities alleen voor NIEUW ingevoegde werkbonnen (snel bij herhaalde runs)
    if to_insert:
        for i in range(0, len(to_insert), 100):
            chunk = to_insert[i:i + 100]
            r = zo.insert("Sales_Orders", [x[0] for x in chunk])
            for res, (_, w) in zip(r, chunk):
                results.append((res, w))
                if res.get("status") == "success":
                    soid = (res.get("details") or {}).get("id")
                    lb = build_logboek(w, emp_map)
                    if soid and lb:
                        title = "Werkbon %s - alle gegevens" % (w.get("OrderNr") or "")
                        new_notes.append((soid, "Sales_Orders", title, lb))
    summarize("Zoho Verkooporders", [x[0] for x in results])
    if new_notes:
        log("Notities plaatsen voor nieuwe werkbonnen (bulk)...")
        zo.create_notes_bulk(new_notes)
        log("   %d werkbon-notities geplaatst." % len(new_notes))


def wipe_accounts(zo):
    ids = zo.all_ids("Accounts")
    log("Zoho: %d klanten gevonden om te verwijderen" % len(ids))
    if not ids:
        log("   Niks te verwijderen.")
        return
    deleted = zo.delete_ids("Accounts", ids)
    log("   %d klanten verwijderd." % deleted)


def wipe_module(zo, module):
    total = 0
    while True:
        ids = zo.all_ids(module)
        if not ids:
            break
        log("Zoho: %d records in %s om te verwijderen" % (len(ids), module))
        total += zo.delete_ids(module, ids)
        if len(ids) < 200:
            break
    log("   %d records verwijderd." % total)


def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


# Genormaliseerde export-headernaam -> Zoho-veld. Afgestemd op de OutSmart werkbon-export.
IMPORT_COLMAP = {
    "ordernummer": "Werkbonnummer_Outsmart", "werkbonnummer": "Werkbonnummer_Outsmart",
    "worksheetcode": "Werkbonnummer_Outsmart", "ordernr": "Werkbonnummer_Outsmart",
    "klantdebiteurnummer": "Debiteurnummer", "debiteurnummer": "Debiteurnummer",
    "klantnaam": "Klant_naam",
    "typewerkzaamheden": "Type_werk", "typewerk": "Type_werk",
    "werkbonstatus": "Status", "werkstatus": "Status", "workstatus": "Status",
    "omschrijving": "Description", "werkomschrijving": "Description",
}

# Specifieke export-kolommen voor adres/extra velden (exacte headernamen).
EXPORT_SHIP = {"street": "Klant straat", "huisnr": "Klant huisnummer",
               "code": "Klant postcode", "city": "Klant stad", "country": "Klant land"}
EXPORT_BILL = {"street": "Factuur klant straat", "huisnr": "Factuur klant huisnummer",
               "code": "Factuur klant postcode", "city": "Factuur klant stad", "country": "Factuur klant land"}


def _read_table(path):
    """Lees CSV/TSV of XLSX -> (headers, list van dict-rijen)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise SystemExit("Voor .xlsx is openpyxl nodig: python3 -m pip install --user openpyxl  "
                             "(of sla het bestand op als CSV).")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        headers = [str(h).strip() if h is not None else "" for h in next(it)]
        out = []
        for r in it:
            out.append({h: ("" if v is None else str(v)) for h, v in zip(headers, r)})
        return headers, out
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        delim = ";" if sample.count(";") >= sample.count(",") else ","
        reader = csv.DictReader(f, delimiter=delim)
        headers = reader.fieldnames or []
        out = [dict(row) for row in reader]
    return headers, out


def import_werkbonnen(zo, path, inspect=False):
    if not path or not os.path.exists(path):
        raise SystemExit("Bestand niet gevonden: %s" % path)
    headers, rows = _read_table(path)
    log("Bestand: %s" % path)
    log("   %d kolommen, %d rijen" % (len(headers), len(rows)))
    mapping = {}
    for h in headers:
        z = IMPORT_COLMAP.get(_norm(h))
        if z:
            mapping[h] = z
    if inspect:
        log("Kolom-koppeling (pas IMPORT_COLMAP aan waar nodig):")
        for h in headers:
            log("   %-42s -> %s" % (h, mapping.get(h, "(naar notitie)")))
        if rows:
            log("Voorbeeld eerste rij:")
            for h in headers:
                log("   %-42s = %s" % (h, str(rows[0].get(h))[:80]))
        return

    def field(row, zf):
        for h, z in mapping.items():
            if z == zf:
                v = row.get(h)
                if v not in (None, ""):
                    return str(v).strip()
        return None

    def addr(row, spec):
        street = " ".join(x for x in [str(row.get(spec["street"]) or "").strip(),
                                      str(row.get(spec["huisnr"]) or "").strip()] if x)
        return (street, str(row.get(spec["code"]) or "").strip(),
                str(row.get(spec["city"]) or "").strip(), str(row.get(spec["country"]) or "").strip())

    # Generiek product voor de orderregel
    pm = zo.product_map()
    GEN = "OUTSMART-WERKZAAMHEDEN"
    generic_pid = pm.get(GEN)
    if not generic_pid:
        r = zo.create_raw("Products", {"Product_Name": "Werkzaamheden", "Product_Code": GEN})
        generic_pid = ((r.get("data") or [{}])[0].get("details") or {}).get("id") if isinstance(r, dict) else None

    # Klant-koppeling: debiteurnummer -> Zoho account-id (in bulk, 1x indexeren)
    log("Klanten indexeren op debiteurnummer...")
    acc_idx = zo.records_index("Accounts", "Debiteurnummer_Outsmart")
    log("   %d klanten geindexeerd." % len(acc_idx))

    pairs, no_debtor, no_wb = [], 0, 0
    for row in rows:
        wb = field(row, "Werkbonnummer_Outsmart")
        debtor = field(row, "Debiteurnummer")
        if not wb:
            no_wb += 1
            continue
        if not debtor:
            no_debtor += 1
            continue
        so = {
            "Subject": _clip(wb, 250),
            "Werkbonnummer_Outsmart": _clip(wb, 255),
            "Debiteurnummer": _clip(debtor, 255),
        }
        for zf in ("Klant_naam", "Type_werk", "Status", "Description"):
            v = field(row, zf)
            if v:
                so[zf] = _clip(v, 2000 if zf == "Description" else 255)
        if so.get("Status"):
            so["Werkstatus_Outsmart_meest_recente_bezoek"] = so["Status"]
        # Klant koppelen
        aid = acc_idx.get(str(debtor))
        if aid:
            so["Account_Name"] = {"id": aid[0]}
        # Adressen
        s_street, s_code, s_city, s_country = addr(row, EXPORT_SHIP)
        if s_street:
            so["Shipping_Street"] = _clip(s_street, 250)
        if s_code:
            so["Shipping_Code"] = _clip(s_code, 50)
        if s_city:
            so["Shipping_City"] = _clip(s_city, 100)
        if s_country:
            so["Shipping_Country"] = _clip(s_country, 100)
        b_street, b_code, b_city, b_country = addr(row, EXPORT_BILL)
        if b_street:
            so["Billing_Street"] = _clip(b_street, 250)
        if b_code:
            so["Billing_Code"] = _clip(b_code, 50)
        if b_city:
            so["Billing_City"] = _clip(b_city, 100)
        if b_country:
            so["Billing_Country"] = _clip(b_country, 100)
        if generic_pid:
            so["Ordered_Items"] = [{"Product_Name": {"id": generic_pid}, "Quantity": 1}]
        note = "\n".join("%s: %s" % (h, row.get(h)) for h in headers if row.get(h) not in (None, ""))
        pairs.append((so, note))
    log("   %d importeerbaar; %d zonder debiteurnummer overgeslagen; %d zonder werkbonnummer overgeslagen"
        % (len(pairs), no_debtor, no_wb))
    if not pairs:
        return
    log("Bestaande verkooporders indexeren op werkbonnummer...")
    idx = zo.records_index("Sales_Orders", "Werkbonnummer_Outsmart")
    log("   %d bestaande werkbonnummers in Zoho." % len(idx))
    to_update, to_insert = [], []
    for so, note in pairs:
        ids = idx.get(str(so["Werkbonnummer_Outsmart"]))
        if ids:
            rec = dict(so)
            rec["id"] = ids[0]
            to_update.append((rec, note, ids[0]))
        else:
            to_insert.append((so, note))
    log("   %d bijwerken, %d nieuw invoegen" % (len(to_update), len(to_insert)))
    ok = 0
    new_notes = []  # (parent_id, module, title, content) - alleen voor nieuw ingevoegde records
    if to_update:
        r = zo.update("Sales_Orders", [x[0] for x in to_update])
        ok += sum(1 for res in r if res.get("status") == "success")
    if to_insert:
        for i in range(0, len(to_insert), 100):
            chunk = to_insert[i:i + 100]
            r = zo.insert("Sales_Orders", [x[0] for x in chunk])
            for res, (_, note) in zip(r, chunk):
                if res.get("status") == "success":
                    ok += 1
                    soid = (res.get("details") or {}).get("id")
                    if soid and note:
                        new_notes.append((soid, "Sales_Orders", "Werkbon (import) - alle exportkolommen", note))
            log("   ... %d/%d verwerkt" % (min(i + 100, len(to_insert)), len(to_insert)))
    log("Zoho Verkooporders (import): %d gelukt, %d mislukt" % (ok, len(pairs) - ok))
    if new_notes:
        log("Notities plaatsen (bulk, 100 per keer)...")
        zo.create_notes_bulk(new_notes)
        log("   %d notities geplaatst." % len(new_notes))


INVOICE_COLMAP = {
    "factuurnummer": "num", "factuurnr": "num", "invoicenumber": "num",
    "factuur": "num", "nummer": "num", "ordernummer": "num",
    "klantdebiteurnummer": "debtor", "debiteurnummer": "debtor", "debiteur": "debtor",
    "klantnaam": "klant", "klant": "klant", "bedrijfsnaam": "klant", "naam": "klant",
    "factuurdatum": "invdate", "datum": "invdate", "invoicedate": "invdate",
    "vervaldatum": "duedate", "vervaldag": "duedate", "duedate": "duedate",
    "totaalincl": "incl", "bedragincl": "incl", "totaal": "incl", "grandtotal": "incl", "bedrag": "incl",
    "totaalexcl": "excl", "bedragexcl": "excl", "subtotaal": "excl", "subtotal": "excl",
    "omschrijving": "descr", "description": "descr",
    "status": "status", "factuurstatus": "status",
}


def import_facturen(zo, path, inspect=False):
    if not path or not os.path.exists(path):
        raise SystemExit("Bestand niet gevonden: %s" % path)
    headers, rows = _read_table(path)
    log("Bestand: %s" % path)
    log("   %d kolommen, %d rijen" % (len(headers), len(rows)))
    mapping = {}
    for h in headers:
        k = INVOICE_COLMAP.get(_norm(h))
        if k and k not in mapping.values():
            mapping[h] = k
    if inspect:
        log("Kolom-koppeling:")
        for h in headers:
            log("   %-42s -> %s" % (h, mapping.get(h, "(naar notitie)")))
        if rows:
            log("Voorbeeld eerste rij:")
            for h in headers:
                log("   %-42s = %s" % (h, str(rows[0].get(h))[:80]))
        return

    def field(row, key):
        for h, k in mapping.items():
            if k == key:
                v = row.get(h)
                if v not in (None, ""):
                    return str(v).strip()
        return None

    pm = zo.product_map()
    GEN = "OUTSMART-WERKZAAMHEDEN"
    generic_pid = pm.get(GEN)
    if not generic_pid:
        r = zo.create_raw("Products", {"Product_Name": "Werkzaamheden", "Product_Code": GEN})
        generic_pid = ((r.get("data") or [{}])[0].get("details") or {}).get("id") if isinstance(r, dict) else None
    log("Klanten indexeren op debiteurnummer...")
    acc_idx = zo.records_index("Accounts", "Debiteurnummer_Outsmart")
    log("   %d klanten geindexeerd." % len(acc_idx))

    pairs, no_num = [], 0
    for row in rows:
        num = field(row, "num")
        if not num:
            no_num += 1
            continue
        debtor = field(row, "debtor")
        inv = {"Subject": _clip(num, 250)}
        d = _date10(field(row, "invdate"))
        if d:
            inv["Invoice_Date"] = d
        dd = _date10(field(row, "duedate"))
        if dd:
            inv["Due_Date"] = dd
        descr = field(row, "descr")
        summary = "Factuurnummer: %s | Klant: %s (%s) | Status: %s | Datum: %s | Bedrag incl: %s" % (
            num, field(row, "klant") or "", debtor or "", field(row, "status") or "",
            field(row, "invdate") or "", field(row, "incl") or "")
        inv["Description"] = _clip((descr + " | " if descr else "") + summary, 2000)
        if debtor and acc_idx.get(debtor):
            inv["Account_Name"] = {"id": acc_idx[debtor][0]}
        amt = _f(field(row, "excl")) if field(row, "excl") else _f(field(row, "incl"))
        line = {"Product_Name": {"id": generic_pid}, "Quantity": 1}
        if amt is not None:
            line["List_Price"] = amt
        if generic_pid:
            inv["Invoiced_Items"] = [line]
        note = "\n".join("%s: %s" % (h, row.get(h)) for h in headers if row.get(h) not in (None, ""))
        pairs.append((inv, note))
    log("   %d facturen; %d zonder factuurnummer overgeslagen" % (len(pairs), no_num))
    if not pairs:
        return
    log("Bestaande facturen indexeren op factuurnummer (Subject)...")
    idx = zo.records_index("Invoices", "Subject")
    log("   %d bestaande facturen in Zoho." % len(idx))
    to_update, to_insert = [], []
    for inv, note in pairs:
        ids = idx.get(str(inv["Subject"]))
        if ids:
            rec = dict(inv)
            rec["id"] = ids[0]
            to_update.append((rec, note, ids[0]))
        else:
            to_insert.append((inv, note))
    log("   %d bijwerken, %d nieuw invoegen" % (len(to_update), len(to_insert)))
    ok = 0
    new_notes = []
    if to_update:
        r = zo.update("Invoices", [x[0] for x in to_update])
        ok += sum(1 for res in r if res.get("status") == "success")
    if to_insert:
        for i in range(0, len(to_insert), 100):
            chunk = to_insert[i:i + 100]
            r = zo.insert("Invoices", [x[0] for x in chunk])
            for res, (_, note) in zip(r, chunk):
                if res.get("status") == "success":
                    ok += 1
                    iid = (res.get("details") or {}).get("id")
                    if iid and note:
                        new_notes.append((iid, "Invoices", "Factuur - alle gegevens", note))
            log("   ... %d/%d verwerkt" % (min(i + 100, len(to_insert)), len(to_insert)))
    log("Zoho Facturen: %d gelukt, %d mislukt" % (ok, len(pairs) - ok))
    if new_notes:
        log("Notities plaatsen (bulk, 100 per keer)...")
        zo.create_notes_bulk(new_notes)
        log("   %d notities geplaatst." % len(new_notes))


def probe_afgehandeld(os_c):
    """Test empirisch of afgehandelde/historische werkbonnen via GetWorkorders op te halen zijn."""
    attempts = [
        ("status=Afgehandeld", {"status": "Afgehandeld", "update_status": "false"}),
        ("status=Afgehandeld (zonder update_status)", {"status": "Afgehandeld"}),
        ("status=afgehandeld (kleine letters)", {"status": "afgehandeld", "update_status": "false"}),
        ("geen status, datum ge 2023 (key=date)", {"key": "date", "operator": "ge", "value": "2023-01-01"}),
        ("geen status, datum ge 2023 (key=creation_date)", {"key": "creation_date", "operator": "ge", "value": "2023-01-01"}),
        ("geen status, datum ge 2023 (key=workdate)", {"key": "workdate", "operator": "ge", "value": "2023-01-01"}),
        ("geen status, datum ge 2023 (key=date_created)", {"key": "date_created", "operator": "ge", "value": "2023-01-01"}),
        ("Afgehandeld + datum ge 2023 (key=date)",
         {"status": "Afgehandeld", "key": "date", "operator": "ge", "value": "2023-01-01", "update_status": "false"}),
        ("status[]=Afgehandeld (array)", {"status[]": "Afgehandeld", "update_status": "false"}),
    ]
    for label, extra in attempts:
        url = os_c._url("GetWorkorders", extra)
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + os_c.token, "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=300, context=_SSL_CONTEXT) as resp:
                code = resp.getcode()
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read().decode("utf-8", "replace")
        except Exception as e:
            log("[%s] EXCEPTIE: %s" % (label, e))
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            log("[%s] HTTP %s | GEEN JSON | lengte=%d | body[:160]=%r" % (label, code, len(raw), raw[:160]))
            continue
        if isinstance(d, dict):
            r = d.get("response")
            n = len(r) if isinstance(r, list) else "n.v.t."
            log("[%s] HTTP %s | envelope-code=%s | records=%s | messages=%s"
                % (label, code, d.get("code"), n, str(d.get("messages"))[:120]))
        else:
            log("[%s] HTTP %s | JSON maar geen object | %s" % (label, code, str(d)[:160]))


def dedupe_salesorders(zo):
    """Verwijder duplicaat-verkooporders: houd per werkbonnummer 1 record over."""
    idx = zo.records_index("Sales_Orders", "Werkbonnummer_Outsmart")
    to_delete = []
    dup_keys = 0
    for wb, ids in idx.items():
        if len(ids) > 1:
            dup_keys += 1
            # houd de nieuwste (hoogste id) = meest recent geimporteerd; verwijder de rest
            keep = max(ids, key=lambda x: int(x) if str(x).isdigit() else 0)
            to_delete.extend(x for x in ids if x != keep)
    log("Verkooporders: %d unieke werkbonnummers, %d met duplicaten, %d records te verwijderen"
        % (len(idx), dup_keys, len(to_delete)))
    if not to_delete:
        log("   Geen duplicaten gevonden.")
        return
    deleted = zo.delete_ids("Sales_Orders", to_delete)
    log("   %d duplicaat-records verwijderd." % deleted)


def probe_workorders(os_c):
    groups = {
        "WERKBONNEN": ["workorders", "workorder", "GetWorkorders", "get_workorders",
                       "werkbonnen", "werkbon", "orders", "order", "opdrachten",
                       "opdracht", "jobs", "worksheets"],
        "FACTUREN": ["invoices", "invoice", "facturen", "factuur", "bills", "billing"],
        "OFFERTES": ["quotes", "quote", "offertes", "offerte", "estimates",
                     "estimate", "quotations", "quotation"],
    }
    def safe(fn, *args):
        try:
            r = fn(*args)
            return r if isinstance(r, dict) else {"code": "?"}
        except SystemExit as e:
            return {"code": "ERR", "_msg": str(e)}
        except Exception as e:  # noqa: BLE001  (incl. socket.timeout)
            return {"code": "TIMEOUT", "_msg": type(e).__name__}

    for title, candidates in groups.items():
        log("=== %s ===" % title)
        found = False
        for ep in candidates:
            g = safe(os_c.raw_get, ep, {})
            gcode = g.get("code")
            gresp = g.get("response")
            n = len(gresp) if isinstance(gresp, list) else "-"
            p = safe(os_c.raw_post, ep, [])
            pcode = p.get("code")
            exists = (gcode not in (1001, "TIMEOUT", "?")) or (pcode not in (1001, "?"))
            mark = "  <-- BESTAAT" if exists else ""
            log("  /%-14s GET code %s (records %s) | POST-leeg code %s%s"
                % (ep, gcode, n, pcode, mark))
            if gcode == 200 and isinstance(gresp, list) and gresp and not found:
                log("     >>> VOORBEELD eerste record:")
                log(json.dumps(gresp[0], ensure_ascii=False, indent=2)[:1800])
                found = True
        log("")


def sample_sources(os_c):
    sources = [
        ("WERKBONNEN", "GetWorkorders", {"status": "Compleet", "update_status": "false"}),
        ("OFFERTES", "quotations", {}),
        ("FACTUREN", "invoices", {}),
    ]
    for title, ep, extra in sources:
        log("=== %s  (/%s) ===" % (title, ep))
        try:
            data = os_c.raw_get(ep, extra, timeout=240)
        except Exception as e:  # noqa: BLE001
            log("   Fout/timeout: %s" % type(e).__name__)
            log("")
            continue
        if not isinstance(data, dict):
            log("   Geen JSON-antwoord: %s" % str(data)[:300])
            log("")
            continue
        code = data.get("code")
        resp = data.get("response")
        if code != 200:
            log("   code %s, messages %s" % (code, data.get("messages")))
            log("")
            continue
        n = len(resp) if isinstance(resp, list) else "-"
        log("   OK - %s records" % n)
        if isinstance(resp, list) and resp:
            log("   Velden van eerste record: %s" % ", ".join(sorted(resp[0].keys())))
            log("   Voorbeeld eerste record:")
            log(json.dumps(resp[0], ensure_ascii=False, indent=2)[:2500])
        log("")


def _date10(s):
    s = str(s or "").strip()
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    return None


def sync_quotations(zo, os_c, dry=False):
    log("Offertes ophalen uit OutSmart...")
    qs = os_c.get_quotations()
    log("   %d offertes opgehaald." % len(qs))
    if not qs:
        return
    pm = zo.product_map()
    GEN = "OUTSMART-WERKZAAMHEDEN"
    generic_pid = pm.get(GEN)
    if not generic_pid:
        r = zo.create_raw("Products", {"Product_Name": "Werkzaamheden", "Product_Code": GEN})
        generic_pid = ((r.get("data") or [{}])[0].get("details") or {}).get("id") if isinstance(r, dict) else None
    log("Klanten indexeren op debiteurnummer...")
    acc_idx = zo.records_index("Accounts", "Debiteurnummer_Outsmart")
    log("   %d klanten geindexeerd." % len(acc_idx))

    pairs = []
    for q in qs:
        num = str(q.get("quo_number_formatted") or q.get("quo_number_numeric") or q.get("quo_id") or "").strip()
        if not num:
            continue
        debtor = str(q.get("quo_quotation_debtor_nr") or "").strip()
        excl = _f(q.get("quo_amount_excl"))
        so = {
            "Subject": _clip(num, 250),
            "Description": _clip("Offertenummer: %s | Status: %s | Werkbon: %s | Klant: %s (%s) | Datum: %s | Vervaldatum: %s | Bedrag excl: %s | Bedrag incl: %s" % (
                num, q.get("quo_status") or "", q.get("quo_reference") or "",
                q.get("quo_quotation_debtor_name") or "", debtor,
                q.get("quo_date") or "", q.get("quo_due_date") or "",
                q.get("quo_amount_excl") or "", q.get("quo_amount") or ""), 2000),
        }
        vt = _date10(q.get("quo_due_date"))
        if vt:
            so["Valid_Till"] = vt
        if debtor and acc_idx.get(debtor):
            so["Account_Name"] = {"id": acc_idx[debtor][0]}
        line = {"Product_Name": {"id": generic_pid}, "Quantity": 1}
        if excl is not None:
            line["List_Price"] = excl
        if generic_pid:
            so["Quoted_Items"] = [line]
        # Notitie: alle offertevelden (grote base64-velden inkorten)
        note_lines = []
        for k in sorted(q.keys()):
            v = q.get(k)
            if v in (None, ""):
                continue
            sv = str(v)
            if len(sv) > 300:
                sv = sv[:300] + " …(ingekort)"
            note_lines.append("%s: %s" % (k, sv))
        pairs.append((so, "\n".join(note_lines)))
    log("   %d offertes te importeren." % len(pairs))
    if dry:
        log("   [proef] geen wijzigingen geschreven.")
        return
    log("Bestaande offertes indexeren op offertenummer (Subject)...")
    idx = zo.records_index("Quotes", "Subject")
    log("   %d bestaande offertes in Zoho." % len(idx))
    to_update, to_insert = [], []
    for so, note in pairs:
        ids = idx.get(str(so["Subject"]))
        if ids:
            rec = dict(so)
            rec["id"] = ids[0]
            to_update.append((rec, note, ids[0]))
        else:
            to_insert.append((so, note))
    log("   %d bijwerken, %d nieuw invoegen" % (len(to_update), len(to_insert)))
    ok = 0
    new_notes = []
    if to_update:
        r = zo.update("Quotes", [x[0] for x in to_update])
        ok += sum(1 for res in r if res.get("status") == "success")
    if to_insert:
        for i in range(0, len(to_insert), 100):
            chunk = to_insert[i:i + 100]
            r = zo.insert("Quotes", [x[0] for x in chunk])
            for res, (_, note) in zip(r, chunk):
                if res.get("status") == "success":
                    ok += 1
                    qid = (res.get("details") or {}).get("id")
                    if qid and note:
                        new_notes.append((qid, "Quotes", "Offerte - alle gegevens", note))
            log("   ... %d/%d verwerkt" % (min(i + 100, len(to_insert)), len(to_insert)))
    log("Zoho Offertes: %d gelukt, %d mislukt" % (ok, len(pairs) - ok))
    if new_notes:
        log("Notities plaatsen (bulk, 100 per keer)...")
        zo.create_notes_bulk(new_notes)
        log("   %d notities geplaatst." % len(new_notes))


def dump_quotations(os_c):
    qs = os_c.get_quotations()
    if not qs:
        log("Geen offertes.")
        return
    log(">>> Voorbeeld offerte (volledig):")
    log(json.dumps(qs[0], ensure_ascii=False, indent=2)[:2000])
    log("")
    log(">>> Velden: %s" % ", ".join(sorted(qs[0].keys())))
    log(">>> TOTAAL offertes: %d" % len(qs))


def dump_workorder(os_c):
    wos = os_c.get_workorders()
    log("OutSmart: %d voltooide werkbonnen" % len(wos))
    if not wos:
        return
    # Overzicht: hoeveel regels per werkbon
    log("Regels per werkbon (materialen / uren):")
    for w in wos:
        mats = w.get("Materials") or []
        wps = w.get("Workperiods") or []
        log("  %s : %d materialen, %d urenregels" % (w.get("OrderNr") or w.get("id"), len(mats), len(wps)))
    # Toon de eerste werkbon met materialen
    for w in wos:
        if w.get("Materials"):
            log("")
            log(">>> Voorbeeld MATERIALS (werkbon %s):" % (w.get("OrderNr") or w.get("id")))
            log(json.dumps(w["Materials"][0], ensure_ascii=False, indent=2)[:1500])
            break
    # Toon de eerste werkbon met urenregels
    for w in wos:
        if w.get("Workperiods"):
            log("")
            log(">>> Voorbeeld WORKPERIODS (werkbon %s):" % (w.get("OrderNr") or w.get("id")))
            log(json.dumps(w["Workperiods"][0], ensure_ascii=False, indent=2)[:1500])
            break
    # Kopvelden voor het logboek
    w = wos[0]
    log("")
    log(">>> KOPVELDEN eerste werkbon:")
    for k in ("TypeOfWork", "WorkStatus", "status", "CreationDate", "MutationDate",
              "EmployeeNr", "SignatureUrl", "PdfUrl", "WordUrl", "Comment", "ShortWorkDescription",
              "InternalWorkDescription", "CustomerRemark", "Reference", "ExternalReference"):
        log("   %-24s = %s" % (k, str(w.get(k))[:80]))
    log("   Photos (aantal)        = %s" % len(w.get("Photos") or []))
    log("   Employees              = %s" % json.dumps(w.get("Employees"), ensure_ascii=False)[:200])
    # StatusChanges (logboek) van de eerste werkbon die het heeft
    for w in wos:
        if w.get("StatusChanges"):
            log("")
            log(">>> Voorbeeld STATUSCHANGES (werkbon %s):" % (w.get("OrderNr") or w.get("id")))
            log(json.dumps(w["StatusChanges"][:3], ensure_ascii=False, indent=2)[:2000])
            break


def run_test(os_c, zo):
    log("OutSmart-verbinding testen...")
    try:
        ht = os_c.get_hourtypes()
        log("   OK - OutSmart bereikbaar (%d uurtypes)." % len(ht))
    except SystemExit as e:
        log("   FOUT - %s" % e)
    log("Zoho-verbinding testen...")
    try:
        accs = zo.get_records("Accounts", "Account_Name")
        log("   OK - Zoho bereikbaar (%d accounts gevonden)." % len(accs))
    except SystemExit as e:
        log("   FOUT - %s" % e)


def main():
    p = argparse.ArgumentParser(description="OutSmart <-> Zoho CRM koppeling")
    p.add_argument("entity", choices=["relations", "materials", "workorders", "workorders-all", "all", "test", "wipe-accounts", "probe-workorders", "sample-sources", "dump-workorder", "test-salesorder", "test-product", "dump-employees", "debug-dump", "probe-invoices", "wipe-salesorders", "dedupe-salesorders", "probe-afgehandeld", "import-werkbonnen", "dump-quotations", "quotations", "import-facturen"])
    p.add_argument("--direction", choices=["outsmart-to-zoho", "zoho-to-outsmart", "both"], default="both")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--until", default="2026-07-22", help="Alleen werkbonnen t/m deze datum (YYYY-MM-DD), voor workorders-all")
    p.add_argument("--file", default=None, help="Pad naar export-bestand (CSV/XLSX), voor import-werkbonnen")
    p.add_argument("--inspect", action="store_true", help="Toon alleen kolommen/koppeling van het export-bestand")
    args = p.parse_args()

    os_c = OutSmart(CONFIG)
    zo = Zoho(CONFIG)

    if args.entity == "test":
        run_test(os_c, zo)
        return
    if args.entity == "wipe-accounts":
        wipe_accounts(zo)
        return
    if args.entity == "wipe-salesorders":
        wipe_module(zo, "Sales_Orders")
        return
    if args.entity == "dedupe-salesorders":
        dedupe_salesorders(zo)
        return
    if args.entity == "probe-afgehandeld":
        probe_afgehandeld(os_c)
        return
    if args.entity == "import-werkbonnen":
        import_werkbonnen(zo, args.file, inspect=args.inspect)
        return
    if args.entity == "probe-workorders":
        probe_workorders(os_c)
        return
    if args.entity == "sample-sources":
        sample_sources(os_c)
        return
    if args.entity == "dump-workorder":
        dump_workorder(os_c)
        return
    if args.entity == "dump-quotations":
        dump_quotations(os_c)
        return
    if args.entity == "quotations":
        sync_quotations(zo, os_c, dry=args.dry_run)
        return
    if args.entity == "import-facturen":
        import_facturen(zo, args.file, inspect=args.inspect)
        return
    if args.entity == "test-salesorder":
        test_salesorder(os_c, zo)
        return
    if args.entity == "test-product":
        test_product(zo)
        return
    if args.entity == "dump-employees":
        dump_employees(os_c)
        return
    if args.entity == "debug-dump":
        debug_dump(os_c)
        return
    if args.entity == "probe-invoices":
        probe_invoices(os_c)
        return
    if args.entity in ("relations", "all"):
        sync_relations(os_c, zo, args.direction, args.dry_run)
    if args.entity in ("materials", "all"):
        sync_materials(os_c, zo, args.direction, args.dry_run)
    if args.entity in ("workorders", "all"):
        sync_workorders(os_c, zo, args.dry_run)
    if args.entity == "workorders-all":
        until = None if str(args.until).lower() in ("all", "none", "alles", "") else args.until
        sync_workorders(os_c, zo, args.dry_run, all_statuses=True, until=until)
    log("Klaar.")


if __name__ == "__main__":
    main()
