"""OrderSaathi backend: Bolna tool endpoints, webhooks, outbound trigger, dashboard API."""
import difflib, json, os, re, sqlite3
from datetime import date, timedelta
from pathlib import Path

import dateparser, httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

BASE = Path(__file__).parent
DB_PATH = os.getenv("DB_PATH", str(BASE / "ordersaathi.db"))
TOKEN = os.getenv("API_TOKEN", "dev-token")
BOLNA_KEY = os.getenv("BOLNA_API_KEY", "")
OUT_AGENT = os.getenv("BOLNA_OUTBOUND_AGENT_ID", "")
DEMO_CALL_TO = os.getenv("DEMO_CALL_TO", "")  # route every outbound call to this number during demos
DEMO_RETAILER_PHONE = os.getenv("DEMO_RETAILER_PHONE", "9000000001")  # your phone = retailer #1
DELIVERY_NOTE = os.getenv("DELIVERY_NOTE", "Delivery ki details humari team confirm karegi.")

app = FastAPI(title="OrderSaathi")

SCHEMA = """
CREATE TABLE IF NOT EXISTS retailers(id INTEGER PRIMARY KEY, name TEXT, phone TEXT UNIQUE, credit_limit REAL);
CREATE TABLE IF NOT EXISTS products(id TEXT PRIMARY KEY, name TEXT, aliases TEXT, unit TEXT, price REAL, stock INTEGER, substitute_id TEXT);
CREATE TABLE IF NOT EXISTS carts(call_sid TEXT, product_id TEXT, qty INTEGER, PRIMARY KEY(call_sid, product_id));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, order_no TEXT, call_sid TEXT UNIQUE, retailer_id INTEGER,
  total REAL, status TEXT, reason TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS order_items(order_id INTEGER, name TEXT, qty INTEGER, price REAL);
CREATE TABLE IF NOT EXISTS invoices(invoice_no TEXT PRIMARY KEY, retailer_id INTEGER, amount REAL, due_date TEXT,
  status TEXT DEFAULT 'pending', outcome TEXT, promised_text TEXT, promised_date TEXT);
CREATE TABLE IF NOT EXISTS calls(key TEXT PRIMARY KEY, execution_id TEXT, direction TEXT, retailer_id INTEGER, status TEXT,
  transcript TEXT, recording_url TEXT, escalated INTEGER DEFAULT 0, reason TEXT, updated_at TEXT DEFAULT (datetime('now')));
"""


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with db() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            return
        c.executemany("INSERT INTO products VALUES(?,?,?,?,?,?,?)", [
            ("P01", "Parle-G 5 rupaye carton", "parle g|parle ji|parle g biscuit|5 rupaye wala biscuit", "carton", 380, 60, None),
            ("P02", "Maggi noodles 70g carton", "maggi|maggi noodles|maggi 70", "carton", 1020, 0, "P03"),
            ("P03", "Yippee noodles 70g carton", "yippee|yippee noodles|sunfeast noodles", "carton", 960, 40, None),
            ("P04", "Surf Excel 1kg carton", "surf excel|surf excel 1 kg|surf excel 1kg|surf 1 kilo", "carton", 1560, 25, None),
            ("P05", "Surf Excel 500g carton", "surf excel|surf excel 500 gram|surf excel 500g|surf 500", "carton", 840, 25, None),
            ("P06", "Tata Salt 1kg carton", "tata salt|tata namak|namak|salt", "carton", 700, 30, None),
            ("P07", "Fortune sunflower oil 1L carton", "fortune oil|fortune tel|sunflower oil|refined tel", "carton", 1480, 20, None),
            ("P08", "Aashirvaad atta 5kg bag", "atta|aashirvaad atta|ashirwad atta|gehu ka atta", "bag", 265, 80, None),
            ("P09", "Red Label tea 250g carton", "red label|chai patti|brooke bond red label|tea", "carton", 1800, 20, None),
            ("P10", "Lux soap 100g carton", "lux|lux sabun|lux soap", "carton", 900, 30, None),
            ("P11", "Colgate 100g carton", "colgate|colgate paste|colgate toothpaste", "carton", 1750, 15, None),
        ])
        c.executemany("INSERT INTO retailers(name, phone, credit_limit) VALUES(?,?,?)", [
            ("Sharma General Store", last10(DEMO_RETAILER_PHONE), 50000),
            ("Gupta Kirana", "9000000002", 20000),
            ("Jain Provision", "9000000003", 15000),
            ("Verma Stores", "9000000004", 8000),  # outstanding 7800 vs limit 8000: any order trips review
        ])
        d = lambda n: (date.today() - timedelta(days=n)).isoformat()
        c.executemany("INSERT INTO invoices(invoice_no, retailer_id, amount, due_date) VALUES(?,?,?,?)", [
            ("INV-2041", 1, 8200, d(12)), ("INV-2056", 2, 14500, d(5)),
            ("INV-2063", 3, 6300, d(2)), ("INV-2070", 4, 7800, d(20)),
        ])


def last10(s):
    return re.sub(r"\D", "", s or "")[-10:]


def auth(authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "bad token")


async def body(req: Request) -> dict:
    try:
        return await req.json()
    except Exception:
        return dict(await req.form())


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def norm(s):
    return re.sub(r"[^a-z0-9\u0900-\u097f ]", " ", (s or "").lower()).split()


def score(q, p):
    qs, qt, best = " ".join(q), set(q), 0.0
    for n in [p["name"]] + p["aliases"].split("|"):
        nt = norm(n)
        if nt:
            r = difflib.SequenceMatcher(None, qs, " ".join(nt)).ratio()
            best = max(best, .5 * r + .5 * len(qt & set(nt)) / len(set(nt)))
    return best


def card(p):
    return {"product_id": p["id"], "name": p["name"], "unit": p["unit"], "price": p["price"], "in_stock": p["stock"] > 0}


DEV_FALLBACK = os.getenv("DEV_FALLBACK", "") == "1"  # demo mode: unknown callers are treated as the demo retailer


def retailer_by_phone(c, phone):
    r = c.execute("SELECT * FROM retailers WHERE phone=?", (last10(phone),)).fetchone()
    if r is None and DEV_FALLBACK:
        r = c.execute("SELECT * FROM retailers ORDER BY id LIMIT 1").fetchone()  # retailer #1 = demo retailer
    return r


def cart_view(c, sid):
    rows = c.execute("SELECT p.name, k.qty, p.price FROM carts k JOIN products p ON p.id=k.product_id WHERE k.call_sid=?", (sid,)).fetchall()
    items = [{"name": r["name"], "quantity": r["qty"], "line_total": r["qty"] * r["price"]} for r in rows]
    return items, sum(i["line_total"] for i in items)


def resolve_date(text):
    t = (text or "").lower().strip()
    if t in ("", "none"):
        return None
    for w, n in {"aaj": 0, "kal": 1, "parso": 2, "parson": 2}.items():  # "kal" assumed future in a payment context
        if re.search(rf"\b{w}\b", t):
            return date.today() + timedelta(days=n)
    t = re.sub(r"\b(tak|tk|se pehle|ke andar|ko)\b", " ", t)
    d = dateparser.parse(t, languages=["en", "hi"], settings={"PREFER_DATES_FROM": "future"})
    return d.date() if d else None


# ---------- Bolna tool endpoints ----------

@app.post("/tools/search_product", dependencies=[Depends(auth)])
async def search_product(req: Request):
    q = norm((await body(req)).get("query"))
    with db() as c:
        ranked = sorted(((score(q, p), p) for p in c.execute("SELECT * FROM products")), key=lambda x: -x[0])[:2]
        if not q or ranked[0][0] < .45:
            return {"status": "not_found", "matches": [], "substitute": None}
        (s1, p1), (s2, p2) = ranked
        if s2 >= .45 and s1 - s2 < .06:
            return {"status": "ambiguous", "matches": [card(p1), card(p2)], "substitute": None}
        if p1["stock"] <= 0:
            sub = c.execute("SELECT * FROM products WHERE id=? AND stock>0", (p1["substitute_id"],)).fetchone()
            return {"status": "out_of_stock", "matches": [card(p1)], "substitute": card(sub) if sub else None}
        return {"status": "ok", "matches": [card(p1)], "substitute": None}


@app.post("/tools/update_cart", dependencies=[Depends(auth)])
async def update_cart(req: Request):
    d = await body(req)
    sid, pid, qty = d.get("call_sid"), d.get("product_id"), int(num(d.get("quantity")))
    with db() as c:
        r = retailer_by_phone(c, d.get("from_number"))
        if not r:
            return {"status": "unknown_retailer", "retailer_known": False}
        p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return {"status": "unknown_product", "retailer_known": True}
        if qty > p["stock"]:
            return {"status": "insufficient_stock", "name": p["name"], "available": p["stock"], "retailer_known": True}
        c.execute("INSERT OR IGNORE INTO calls(key, direction, retailer_id) VALUES(?, 'inbound', ?)", (sid, r["id"]))
        if qty <= 0:
            c.execute("DELETE FROM carts WHERE call_sid=? AND product_id=?", (sid, pid))
        else:
            c.execute("INSERT INTO carts VALUES(?,?,?) ON CONFLICT(call_sid, product_id) DO UPDATE SET qty=excluded.qty", (sid, pid, qty))
        items, total = cart_view(c, sid)
    return {"status": "ok", "line_items": items, "total": total, "retailer_known": True}


@app.post("/tools/confirm_order", dependencies=[Depends(auth)])
async def confirm_order(req: Request):
    d = await body(req)
    sid, said_total = d.get("call_sid"), num(d.get("read_back_total"))
    with db() as c:
        old = c.execute("SELECT * FROM orders WHERE call_sid=?", (sid,)).fetchone()
        if old:  # idempotent: a retry returns the same order
            return {"status": "confirmed", "order_id": old["order_no"], "total": old["total"], "delivery_note": DELIVERY_NOTE}
        r = retailer_by_phone(c, d.get("from_number"))
        if not r:
            return {"status": "unknown_retailer"}
        items, total = cart_view(c, sid)
        if not items:
            return {"status": "empty_cart"}
        if abs(total - said_total) > 0.5:
            return {"status": "total_mismatch", "total": total, "line_items": items}
        owed = c.execute("SELECT COALESCE(SUM(amount),0) FROM invoices WHERE retailer_id=? AND status='pending'", (r["id"],)).fetchone()[0]
        status, reason = ("needs_review", "Over credit limit") if owed + total > r["credit_limit"] else ("confirmed", None)
        cur = c.execute("INSERT INTO orders(call_sid, retailer_id, total, status, reason) VALUES(?,?,?,?,?)", (sid, r["id"], total, status, reason))
        oid, order_no = cur.lastrowid, f"OS-{1000 + cur.lastrowid}"
        c.execute("UPDATE orders SET order_no=? WHERE id=?", (order_no, oid))
        for k in c.execute("SELECT k.product_id, k.qty, p.name, p.price FROM carts k JOIN products p ON p.id=k.product_id WHERE k.call_sid=?", (sid,)).fetchall():
            c.execute("INSERT INTO order_items VALUES(?,?,?,?)", (oid, k["name"], k["qty"], k["price"]))
            c.execute("UPDATE products SET stock=stock-? WHERE id=?", (k["qty"], k["product_id"]))
        c.execute("DELETE FROM carts WHERE call_sid=?", (sid,))
    return {"status": "confirmed", "order_id": order_no, "total": total, "delivery_note": DELIVERY_NOTE}


@app.post("/tools/log_payment_promise", dependencies=[Depends(auth)])
async def log_payment_promise(req: Request):
    d = await body(req)
    sid, inv, outcome, text = d.get("call_sid"), d.get("invoice_no"), d.get("outcome"), d.get("promised_date_text")
    when = resolve_date(text)
    with db() as c:
        row = c.execute("SELECT retailer_id FROM invoices WHERE invoice_no=?", (inv,)).fetchone()
        if not row:
            return {"status": "unknown_invoice"}
        c.execute("INSERT OR IGNORE INTO calls(key, direction, retailer_id) VALUES(?, 'outbound', ?)", (sid, row["retailer_id"]))
        c.execute("UPDATE invoices SET outcome=?, promised_text=?, promised_date=?, status=CASE WHEN ?='already_paid' THEN 'paid_claimed' ELSE status END WHERE invoice_no=?",
                  (outcome, text, when.isoformat() if when else None, outcome, inv))
    out = {"status": "logged", "resolved_date": when.isoformat() if when else None,
           "resolved_date_spoken": f"{when.day} {when:%B}" if when else None}
    if not when and outcome == "will_pay":
        out["message"] = "Date unclear. Ask the retailer for a specific date."
    return out


# ---------- Webhooks ----------

@app.post("/webhooks/bolna")
async def bolna_webhook(req: Request, key: str = ""):
    if key != TOKEN:
        raise HTTPException(401, "bad key")
    d = await body(req)
    tel = d.get("telephony_data") or {}
    # Inspect your first real payload and adjust these field names if needed.
    ex_id = d.get("id") or d.get("execution_id")
    sid = tel.get("provider_call_id") or d.get("call_sid") or ex_id
    tr = d.get("transcript")
    tr = tr if isinstance(tr, str) or tr is None else json.dumps(tr, ensure_ascii=False)
    with db() as c:
        c.execute("""INSERT INTO calls(key, execution_id, direction, status, transcript, recording_url) VALUES(?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET execution_id=excluded.execution_id, status=excluded.status,
            transcript=COALESCE(excluded.transcript, transcript), recording_url=COALESCE(excluded.recording_url, recording_url),
            direction=COALESCE(calls.direction, excluded.direction), updated_at=datetime('now')""",
                  (sid, ex_id, d.get("call_type"), d.get("status"), tr, tel.get("recording_url") or d.get("recording_url")))
    return {"ok": True}


@app.post("/webhooks/escalation")
async def escalation(req: Request, key: str = ""):
    if key != TOKEN:
        raise HTTPException(401, "bad key")
    d = await body(req)
    sid, reason = d.get("call_sid"), d.get("reason") or "Transferred to staff"
    with db() as c:
        c.execute("INSERT OR IGNORE INTO calls(key) VALUES(?)", (sid,))
        c.execute("UPDATE calls SET escalated=1, reason=?, updated_at=datetime('now') WHERE key=?", (reason, sid))
        c.execute("UPDATE orders SET status='needs_review', reason=? WHERE call_sid=?", (reason, sid))
    return {"ok": True}


# ---------- Outbound collections ----------

@app.post("/admin/collect", dependencies=[Depends(auth)])
async def collect(invoice_no: str | None = None):
    if not (BOLNA_KEY and OUT_AGENT):
        raise HTTPException(400, "Set BOLNA_API_KEY and BOLNA_OUTBOUND_AGENT_ID")
    sql = ("SELECT i.*, r.name, r.phone FROM invoices i JOIN retailers r ON r.id=i.retailer_id "
           "WHERE i.status='pending' AND i.due_date<=?" + (" AND i.invoice_no=?" if invoice_no else ""))
    with db() as c:
        rows = c.execute(sql, (date.today().isoformat(), *([invoice_no] if invoice_no else []))).fetchall()
    results = []
    async with httpx.AsyncClient(timeout=20) as h:
        for r in rows:
            resp = await h.post("https://api.bolna.ai/call", headers={"Authorization": f"Bearer {BOLNA_KEY}"}, json={
                "agent_id": OUT_AGENT,
                "recipient_phone_number": DEMO_CALL_TO or f"+91{r['phone']}",
                "user_data": {"retailer_name": r["name"], "invoice_no": r["invoice_no"],
                              "amount": str(int(r["amount"])), "due_date": r["due_date"]},
            })
            results.append({"invoice_no": r["invoice_no"], "http": resp.status_code, "body": resp.text[:300]})
    return results


# ---------- Dashboard ----------

@app.get("/api/state")
def state():
    with db() as c:
        orders = [dict(o) for o in c.execute("SELECT o.*, r.name AS retailer FROM orders o JOIN retailers r ON r.id=o.retailer_id ORDER BY o.id DESC LIMIT 20")]
        for o in orders:
            o["items"] = [dict(i) for i in c.execute("SELECT name, qty, price FROM order_items WHERE order_id=?", (o["id"],))]
        invoices = [dict(i) for i in c.execute("SELECT i.*, r.name AS retailer FROM invoices i JOIN retailers r ON r.id=i.retailer_id ORDER BY i.due_date")]
        calls = [dict(x) for x in c.execute("SELECT c.*, r.name AS retailer FROM calls c LEFT JOIN retailers r ON r.id=c.retailer_id ORDER BY c.updated_at DESC LIMIT 20")]
        for x in calls:
            x["transcript"] = (x["transcript"] or "")[:400]
    return {"orders": orders, "invoices": invoices, "calls": calls}


@app.get("/")
def dashboard():
    return FileResponse(BASE / "static" / "dashboard.html")


init()