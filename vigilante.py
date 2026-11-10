#!/usr/bin/env python3
"""
Vigilante de errores de precio en Amazon.es -> tu Telegram privado
Se ejecuta cada ~10 minutos en GitHub Actions (gratis).
"""

import gzip
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================ CONFIGURACIÓN
# Categorías a vigilar: "Nombre visible": "identificador de Amazon.es"
CATEGORIAS = {
    "Electrónica": "electronics",
    "Hogar y cocina": "kitchen",
    "Juguetes": "toys",
    "Belleza": "beauty",
}

MIN_DROP_PCT = 70      # % de rebaja mínimo para avisar
MIN_REF_EUR = 20       # ignora productos que normalmente cuestan menos (€)
TOY_MAX_AGE = 12       # juguetes: edad máxima; None = sin filtro de edad
# ==========================================================================

BASE = "https://www.amazon.es"
MIN_OBS = 3            # lecturas previas necesarias antes de avisar
HISTORY_LEN = 30       # lecturas guardadas por producto

# Por cada categoría: más vendidos (2 páginas) y "Los que más suben"
PAGES = {
    nombre: [f"/gp/bestsellers/{slug}", f"/gp/bestsellers/{slug}?pg=2",
             f"/gp/movers-and-shakers/{slug}"]
    for nombre, slug in CATEGORIAS.items()
}
TOY_SLUGS = {"toys"}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "estado/historial.json")

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def fetch(path):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, headers={
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "es-ES,es;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def is_blocked(html):
    low = html.lower()
    return ("captcha" in low and "validatecaptcha" in low) or \
        "api-services-support@amazon.com" in low


def send_telegram(text):
    """Envía un mensaje. Devuelve True si Telegram lo ha aceptado."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("(Telegram no configurado) " + text.replace("\n", " | "))
        return False
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
    }).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, timeout=15).read()
        return True
    except urllib.error.URLError as e:
        log(f"Error enviando a Telegram: {e}")
        return False


PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})(?:\s|&nbsp;)*€")
# El producto se reconoce por data-asin="..." o por enlaces /dp/ASIN
ASIN_RE = re.compile(r'data-asin="([A-Z0-9]{10})"|/dp/([A-Z0-9]{10})')
TITLE_RES = [
    re.compile(r'<img[^>]+alt="([^"]{8,})"'),
    re.compile(r'class="[^"]*line-clamp[^"]*"[^>]*>\s*([^<]{8,})<'),
]


def parse_price(txt):
    return float(txt.replace(".", "").replace(",", "."))


def unescape(s):
    return (s.replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&nbsp;", " ").strip())


def parse_items(html):
    """Extrae (asin, título, precio) de una página de listados."""
    items = {}
    marks = []
    for m in ASIN_RE.finditer(html):
        asin = m.group(1) or m.group(2)
        if not marks or marks[-1][1] != asin:
            marks.append((m.start(), asin))
    for i, (pos, asin) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else min(len(html), pos + 8000)
        block = html[pos:end]
        price_m = PRICE_RE.search(block)
        if not price_m or asin in items:
            continue
        title = ""
        for tre in TITLE_RES:
            tm = tre.search(block)
            if tm:
                title = unescape(tm.group(1))
                break
        items[asin] = (title or asin, parse_price(price_m.group(1)))
    return items


AGE_PLUS_RE = re.compile(r"(\d{1,2})\s*(?:\+|años?\s*\+|\+\s*años?|años? o más|años? y más)", re.I)
AGE_RANGE_RE = re.compile(r"(?:de|desde)\s*(\d{1,2})\s*(?:a|-|hasta)\s*\d{1,2}\s*años", re.I)
ADULT_RE = re.compile(r"adult[oa]s?|coleccionist", re.I)


def toy_ok(title):
    """True si el juguete entra en la edad elegida (o no indica edad)."""
    if TOY_MAX_AGE is None:
        return True
    if ADULT_RE.search(title):
        return False
    ages = [int(a) for a in AGE_PLUS_RE.findall(title)]
    ages += [int(a) for a in AGE_RANGE_RE.findall(title)]
    return not ages or min(ages) <= TOY_MAX_AGE


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"products": {}, "alerted": {}, "blocked_runs": 0}


def save_state(state):
    now = time.time()
    state["products"] = {k: v for k, v in state["products"].items()
                         if now - v["last_seen"] < 14 * 86400}
    state["alerted"] = {k: v for k, v in state["alerted"].items()
                        if now - v < 3 * 86400}
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def check_product(state, asin, title, price, category):
    """Registra el precio y devuelve un aviso si es un posible error."""
    now = time.time()
    p = state["products"].setdefault(asin, {"title": title, "prices": [],
                                            "cat": category})
    p["title"], p["last_seen"] = title, now
    history = [x for _, x in p["prices"]]
    alert = None

    if len(history) >= MIN_OBS:
        ref = statistics.median(history)
        drop = (1 - price / ref) * 100 if ref else 0
        key = f"{asin}:{round(price, 2)}"
        if (drop >= MIN_DROP_PCT and ref >= MIN_REF_EUR
                and key not in state["alerted"]):
            if CATEGORIAS.get(category) not in TOY_SLUGS or toy_ok(title):
                alert = {"asin": asin, "title": title, "price": price,
                         "ref": ref, "drop": round(drop), "cat": category}
                state["alerted"][key] = now

    if not alert:
        p["prices"] = (p["prices"] + [[int(now), price]])[-HISTORY_LEN:]
    return alert


def format_alert(a):
    title = (a["title"][:180].replace("&", "&amp;")
             .replace("<", "&lt;").replace(">", "&gt;"))
    return (f"🚨 <b>Posible error de precio (-{a['drop']}%)</b>\n"
            f"{title}\n\n"
            f"💶 Ahora: <b>{a['price']:.2f} €</b>\n"
            f"📊 Habitual: {a['ref']:.2f} €\n"
            f"🗂️ {a['cat']}\n\n"
            f"👉 {BASE}/dp/{a['asin']}")


def main():
    state = load_state()
    pages_ok = pages_blocked = 0
    alerts = []
    seen = 0

    # Orden aleatorio en cada pasada: si Amazon bloquea a mitad, la próxima
    # vez se empieza por otras páginas y, entre pasadas, se cubre todo.
    jobs = [(c, p) for c, paths in PAGES.items() for p in paths]
    random.shuffle(jobs)
    for category, path in jobs:
        try:
            html = fetch(path)
        except Exception as e:
            log(f"Fallo al leer {path}: {e}")
            pages_blocked += 1
            continue
        if is_blocked(html):
            log(f"Amazon pide captcha en {path}")
            pages_blocked += 1
            continue
        items = parse_items(html)
        pages_ok += 1
        seen += len(items)
        log(f"{path}: {len(items)} productos")
        for asin, (title, price) in items.items():
            a = check_product(state, asin, title, price, category)
            if a:
                alerts.append(a)
        time.sleep(random.uniform(4, 8))

    for a in alerts:
        send_telegram(format_alert(a))
        log(f"AVISO {a['asin']} -{a['drop']}% {a['price']:.2f} €")

    if pages_ok == 0:
        state["blocked_runs"] = state.get("blocked_runs", 0) + 1
        if state["blocked_runs"] == 6:
            send_telegram("⚠️ El vigilante lleva ~1 hora sin poder leer "
                          "Amazon (bloqueo temporal). Seguirá intentándolo.")
    else:
        if state.get("blocked_runs", 0) >= 6:
            send_telegram("✅ El vigilante vuelve a leer Amazon con normalidad.")
        state["blocked_runs"] = 0

    if not state.get("bienvenida_ok") and pages_ok:
        ok = send_telegram(
            f"✅ Vigilante activo. Esta pasada: {seen} productos leídos, "
            f"{len(state['products'])} en el historial. Te avisaré de bajadas "
            f"de ≥{MIN_DROP_PCT:.0f}% respecto al precio habitual.")
        state["bienvenida_ok"] = ok

    save_state(state)
    log(f"Fin: {pages_ok} páginas leídas, {pages_blocked} fallidas, "
        f"{seen} productos, {len(alerts)} avisos, "
        f"{len(state['products'])} en historial.")


if __name__ == "__main__":
    if "--test-telegram" in sys.argv:
        send_telegram("🔔 Prueba: el bot del vigilante funciona.")
    else:
        main()
