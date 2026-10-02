"""
Amazon Price Tracker.

Uso:
    python tracker.py                 # revisa todos los productos (lo que corre el Programador de tareas)
    python tracker.py add URL --target 25 [--normal 32.99] [--name "Billetera"]
    python tracker.py remove ASIN
    python tracker.py list
    python tracker.py test-notify     # muestra una notificación de prueba

Archivos:
    data/products.json  -> productos a trackear (url, precio normal, precio meta) + settings
    data/history/ASIN.json -> histórico de precios y estado de cada producto (un archivo por producto)
    logs/tracker.log    -> log de cada ejecución
"""

import argparse
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
PRODUCTS_FILE = DATA_DIR / "products.json"
HISTORY_DIR = DATA_DIR / "history"
LEGACY_HISTORY_FILE = DATA_DIR / "history.json"
PROFILE_DIR = DATA_DIR / "browser-profile"
APP_ID = "AmazonPriceTracker"  # AUMID registrado por install_task.ps1

DEFAULT_SETTINGS = {
    # "toast" (centro de notificaciones), "msgbox" (ventana emergente) o "both"
    "notification": "toast",
    # "msedge" o "chrome": navegador instalado que usa Playwright
    "browser_channel": "msedge",
    "headless": True,
}

PRICE_SELECTORS = [
    "#corePrice_feature_div .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
    "#corePrice_desktop .a-offscreen",
    "#apex_desktop .a-price .a-offscreen",
    "#tp_price_block_total_price_ww .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "span.a-price[data-a-color='price'] .a-offscreen",
]

# pythonw.exe (Programador de tareas) no tiene consola: sys.stdout/stderr son None.
LOG_DIR.mkdir(exist_ok=True)
if sys.stdout is None or sys.stderr is None:
    _sink = open(LOG_DIR / "stdout.log", "a", encoding="utf-8")
    sys.stdout = sys.stdout or _sink
    sys.stderr = sys.stderr or _sink

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "tracker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tracker")


# --------------------------------------------------------------------------- datos

def load_json(path, default):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def load_config():
    cfg = load_json(PRODUCTS_FILE, {"settings": {}, "products": []})
    cfg["settings"] = {**DEFAULT_SETTINGS, **cfg.get("settings", {})}
    return cfg


def history_path(pid):
    return HISTORY_DIR / f"{pid}.json"


def load_history(pid):
    return load_json(history_path(pid), {"id": pid, "history": []})


def save_history(pid, entry):
    save_json(history_path(pid), entry)


def migrate_legacy_history():
    """Divide el antiguo data/history.json (todos los productos) en un archivo por producto."""
    if not LEGACY_HISTORY_FILE.exists():
        return
    for pid, entry in load_json(LEGACY_HISTORY_FILE, {}).items():
        if not history_path(pid).exists():
            save_history(pid, {"id": pid, **entry})
    LEGACY_HISTORY_FILE.replace(LEGACY_HISTORY_FILE.with_suffix(".json.bak"))
    log.info("Histórico migrado a %s", HISTORY_DIR)


def extract_asin(url):
    m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", url)
    if not m:
        raise ValueError(f"No se encontró el ASIN en la URL: {url}")
    return m.group(1)


def product_id(product):
    return product.get("id") or extract_asin(product["url"])


def parse_price(text):
    if not text:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# --------------------------------------------------------------------------- progreso hacia la meta

# Tramos de avance desde el precio normal hasta la meta (0 = no ha bajado lo suficiente, 4 = meta)
LEVELS = [
    {"min": 0, "color": "rojo", "emoji": "🔴", "label": "sin bajar"},
    {"min": 25, "color": "naranja", "emoji": "🟠", "label": "25 % del camino"},
    {"min": 50, "color": "amarillo", "emoji": "🟡", "label": "50 % del camino"},
    {"min": 75, "color": "azul", "emoji": "🔵", "label": "75 % del camino"},
    {"min": 100, "color": "verde", "emoji": "🟢", "label": "precio meta"},
]


def target_progress(product, price, entry):
    """% del camino recorrido desde el precio normal hasta la meta: (normal - actual) / (normal - meta)."""
    target = float(product["target_price"])
    normal = product.get("normal_price")
    if normal is None and entry["history"]:
        normal = entry["history"][0]["price"]  # sin precio normal: usar el primer precio registrado
    if price <= target:
        return 100.0
    if normal is None or float(normal) <= target:
        return 0.0
    return max(0.0, (float(normal) - price) / (float(normal) - target) * 100)


def progress_level(pct):
    return max(i for i, lv in enumerate(LEVELS) if pct + 1e-6 >= lv["min"])  # tolerancia de redondeo


# --------------------------------------------------------------------------- scraping

class BlockedError(Exception):
    """Amazon devolvió su página anti-bot."""


def scrape(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("#productTitle, form[action*='validateCaptcha']", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    if page.query_selector("form[action*='validateCaptcha']"):
        raise BlockedError("Amazon mostró la página de verificación anti-bot")

    title_el = page.query_selector("#productTitle")
    title = title_el.inner_text().strip() if title_el else None

    price = None
    for sel in PRICE_SELECTORS:
        for el in page.query_selector_all(sel):
            price = parse_price(el.text_content())
            if price:
                break
        if price:
            break

    if price is None:  # precio partido en entero + fracción
        whole = page.query_selector("#corePrice_feature_div .a-price-whole")
        frac = page.query_selector("#corePrice_feature_div .a-price-fraction")
        if whole:
            price = parse_price(whole.text_content().strip().rstrip(".") + "." + (frac.text_content().strip() if frac else "00"))

    img_el = page.query_selector("#landingImage") or page.query_selector("#imgBlkFront")
    image = img_el.get_attribute("src") if img_el else None

    avail_el = page.query_selector("#availability")
    availability = " ".join(avail_el.inner_text().split()) if avail_el else None
    buyable = (price is not None
               and bool(page.query_selector("#add-to-cart-button, #buy-now-button"))
               and "unavailable" not in (availability or "").lower())

    return {"title": title, "price": price, "image": image, "availability": availability,
            "buyable": buyable, "variants": scrape_variants(page)}


def scrape_variants(page):
    """Variantes de color de la publicación: [{asin, name, price, available}]."""
    variants = []
    for li in page.query_selector_all("#variation_color_name li, #inline-twister-row-color_name li"):
        asin = li.get_attribute("data-asin") or li.get_attribute("data-defaultasin")
        if not asin:
            continue
        img = li.query_selector("img")
        name = li.get_attribute("title") or (img.get_attribute("alt") if img else "") or ""
        name = re.sub(r"^Click to select\s*", "", name).strip()
        text = " ".join(li.inner_text().split())
        cls = (li.get_attribute("class") or "").lower()
        variants.append({"asin": asin, "name": name, "price": parse_price(text),
                         "available": "unavailable" not in cls and "unavailable" not in text.lower()})
    if not variants:  # respaldo: JSON embebido en la página
        m = re.search(r'"colorToAsin"\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})', page.content())
        if m:
            try:
                for name, v in json.loads(m.group(1)).items():
                    variants.append({"asin": v.get("asin"), "name": name, "price": None, "available": None})
            except ValueError:
                pass
    return variants


def search_results(page, query):
    """Resultados de búsqueda de Amazon: [{asin, title, price}]."""
    page.goto("https://www.amazon.com/s?k=" + quote_plus(query), wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector("div[data-component-type='s-search-result'], form[action*='validateCaptcha']",
                               timeout=15000)
    except Exception:
        pass
    if page.query_selector("form[action*='validateCaptcha']"):
        raise BlockedError("Amazon mostró la página de verificación anti-bot")
    results = []
    for r in page.query_selector_all("div[data-component-type='s-search-result']"):
        t = r.query_selector("[data-cy=title-recipe]") or r.query_selector("h2")
        pr = r.query_selector(".a-price .a-offscreen")
        results.append({"asin": r.get_attribute("data-asin"),
                        "title": " ".join((t.inner_text() if t else "").split()),
                        "price": parse_price(pr.text_content()) if pr else None})
    return results


def check_watch(page, product, result):
    """Busca la variante vigilada (p. ej. color "Black"): primero entre las variantes de la
    publicación y, si no está, en la búsqueda de Amazon (por si sale como publicación aparte)."""
    watch = product["watch"]
    pid = product_id(product)
    status = {"wanted": watch.get("variant"), "found": False, "available": False}

    wanted = (watch.get("variant") or "").lower()
    match = next((v for v in result["variants"] if wanted and wanted in v["name"].lower()), None)
    if match:
        url = f"https://www.amazon.com/dp/{match['asin']}?th=1&psc=1"
        r = result if match["asin"] == pid else scrape(page, url)
        status.update(found=True, source="variante", asin=match["asin"], url=url, title=r["title"],
                      price=r["price"], available=r["buyable"])
        return status

    if watch.get("search"):
        keywords = [k.lower() for k in watch.get("keywords") or watch["search"].split()]
        for item in search_results(page, watch["search"]):
            words = set(re.findall(r"[a-z0-9]+", item["title"].lower()))
            if item["asin"] and item["asin"] != pid and all(k in words for k in keywords):
                url = f"https://www.amazon.com/dp/{item['asin']}?th=1&psc=1"
                r = scrape(page, url)
                status.update(found=True, source="búsqueda", asin=item["asin"], url=url, title=r["title"],
                              price=r["price"], available=r["buyable"])
                if r["buyable"]:
                    break
    return status


# --------------------------------------------------------------------------- notificaciones

def notify(settings, title, message, url=None):
    mode = settings.get("notification", "toast")
    shown = False
    if mode in ("toast", "both"):
        try:
            from winotify import Notification, audio

            toast = Notification(app_id=APP_ID, title=title, msg=message, duration="long",
                                 launch=url or "")
            toast.set_audio(audio.Default, loop=False)
            if url:
                toast.add_actions(label="Ver en Amazon", launch=url)
            toast.show()
            shown = True
        except Exception as e:
            log.warning("No se pudo mostrar el toast (%s); usando MessageBox", e)
    if mode in ("msgbox", "both") or not shown:
        import ctypes

        MB_ICONINFORMATION, MB_TOPMOST, MB_SETFOREGROUND = 0x40, 0x40000, 0x10000
        ctypes.windll.user32.MessageBoxW(0, message + (f"\n\n{url}" if url else ""), title,
                                         MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND)


# --------------------------------------------------------------------------- comandos

def cmd_track(_args):
    cfg = load_config()
    settings, products = cfg["settings"], cfg["products"]
    if not products:
        log.info("No hay productos en %s", PRODUCTS_FILE)
        return

    migrate_legacy_history()
    alerts = []
    log.info("Revisando %d producto(s)...", len(products))

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        log.info("Abriendo navegador (%s)", settings["browser_channel"])
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            channel=settings["browser_channel"],
            headless=settings["headless"],
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for i, product in enumerate(products):
            pid = product_id(product)
            entry = load_history(pid)
            now = datetime.now().isoformat(timespec="seconds")
            entry["last_check"] = now

            result, error = None, None
            for attempt in range(2):
                try:
                    result = scrape(page, product["url"])
                    if result["price"] is None:
                        error = "No se encontró el precio (¿sin stock o sin envío?)"
                    break
                except BlockedError as e:
                    error = str(e)
                    if attempt == 0:
                        log.warning("%s: bloqueado, reintentando en 20 s", pid)
                        time.sleep(20)
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    break

            if result:
                entry["title"] = result["title"] or entry.get("title")
                entry["image"] = result["image"] or entry.get("image")
                entry["availability"] = result["availability"]

            if result and product.get("watch"):
                try:
                    status = check_watch(page, product, result)
                    status["checked"] = now
                    entry["watch"] = status
                    entry["last_error"] = None
                    if status["available"]:
                        if status.get("price") is not None:
                            entry["history"].append({"ts": now, "price": status["price"]})
                        if entry.get("last_alert_asin") != status["asin"]:
                            alerts.append(("available", product, entry, status))
                            entry["last_alert_asin"] = status["asin"]
                    else:
                        entry["last_alert_asin"] = None
                    log.info("%s: vigilando '%s' -> %s", pid, status["wanted"],
                             "DISPONIBLE " + status["url"] if status["available"]
                             else "encontrado sin stock" if status["found"] else "no disponible aún")
                except Exception as e:
                    entry["last_error"] = f"{type(e).__name__}: {e}"
                    log.error("%s: %s", pid, entry["last_error"])
            elif result and result["price"] is not None:
                price = result["price"]
                entry["history"].append({"ts": now, "price": price})
                entry["last_error"] = None
                target = float(product["target_price"])
                pct = target_progress(product, price, entry)
                level = progress_level(pct)
                entry["progress"] = round(pct, 1)
                entry["level"] = level
                log.info("%s: $%.2f (meta $%.2f, %d%% del camino, %s) %s", pid, price, target, int(pct),
                         LEVELS[level]["color"], (entry.get("title") or "")[:50])

                if level == len(LEVELS) - 1:
                    # Meta: avisar la primera vez y otra vez si el precio cambia mientras sigue bajo la meta
                    if entry.get("last_alert_price") != price:
                        alerts.append(("price", product, entry, price))
                        entry["last_alert_price"] = price
                else:
                    entry["last_alert_price"] = None
                    # Avisar al alcanzar un tramo nuevo (25/50/75 %). Si el precio sube y baja el tramo,
                    # se volverá a avisar cuando lo alcance de nuevo.
                    if level > entry.get("last_level", 0):
                        alerts.append(("progress", product, entry, (price, pct, level)))
                entry["last_level"] = level
            else:
                entry["last_error"] = error
                log.error("%s: %s", pid, error)

            save_history(pid, entry)
            if i < len(products) - 1:
                time.sleep(random.uniform(4, 9))

        ctx.close()

    log.info("Revisión terminada. Alertas: %d", len(alerts))
    for kind, product, entry, data in alerts:
        name = product.get("name") or entry.get("title") or product_id(product)
        if kind == "progress":
            price, pct, level = data
            lv = LEVELS[level]
            notify(settings, f"{lv['emoji']} Bajó de precio: {lv['label']} hacia la meta",
                   f"{name[:80]}\nAhora: ${price:.2f}  ({int(pct)}% del camino a la meta "
                   f"${float(product['target_price']):.2f})",
                   product["url"])
        elif kind == "available":
            notify(settings, "¡Ya está disponible!",
                   f"{(data.get('title') or name)[:90]}"
                   + (f"\nPrecio: ${data['price']:.2f}" if data.get("price") is not None else ""),
                   data["url"])
        else:
            notify(
                settings,
                "🟢 ¡Precio objetivo alcanzado!",
                f"{name[:80]}\nAhora: ${data:.2f}  (meta ${float(product['target_price']):.2f}"
                + (f", normal ${float(product['normal_price']):.2f}" if product.get("normal_price") else "")
                + ")",
                product["url"],
            )


def cmd_add(args):
    cfg = load_json(PRODUCTS_FILE, {"settings": DEFAULT_SETTINGS, "products": []})
    asin = extract_asin(args.url)
    cfg["products"] = [p for p in cfg["products"] if product_id(p) != asin]
    item = {"id": asin, "name": args.name or "", "url": args.url, "target_price": args.target}
    if args.normal is not None:
        item["normal_price"] = args.normal
    cfg["products"].append(item)
    save_json(PRODUCTS_FILE, cfg)
    print(f"Agregado {asin} (meta ${args.target:.2f})")


def cmd_remove(args):
    cfg = load_json(PRODUCTS_FILE, {"settings": DEFAULT_SETTINGS, "products": []})
    before = len(cfg["products"])
    cfg["products"] = [p for p in cfg["products"] if product_id(p) != args.asin]
    save_json(PRODUCTS_FILE, cfg)
    print("Eliminado" if len(cfg["products"]) < before else "No encontrado")


def cmd_list(_args):
    migrate_legacy_history()
    cfg = load_config()
    for p in cfg["products"]:
        pid = product_id(p)
        entry = load_history(pid)
        h = entry["history"]
        last = f"${h[-1]['price']:.2f}" if h else "—"
        goal = (f"vigila '{p['watch'].get('variant')}'" if p.get("watch")
                else f"meta ${float(p['target_price']):.2f}")
        print(f"{pid}  actual {last:>9}  {goal}  {p.get('name') or (entry.get('title') or '')[:50]}")


def cmd_test_notify(_args):
    cfg = load_config()
    notify(cfg["settings"], "Amazon Price Tracker", "Notificación de prueba: todo funciona.",
           "https://www.amazon.com")


def relaunch_with_hidden_console():
    """Playwright puede colgarse bajo pythonw.exe (sin consola). Cuando el Programador de tareas
    lanza pythonw, nos relanzamos con python.exe en una consola oculta (sin ventana visible)."""
    exe = Path(sys.executable)
    if exe.name.lower() != "pythonw.exe":
        return False
    import subprocess

    python = exe.with_name("python.exe")
    result = subprocess.run([str(python), str(Path(__file__).resolve()), *sys.argv[1:]],
                            cwd=str(BASE_DIR), creationflags=subprocess.CREATE_NO_WINDOW,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.exit(result.returncode)


def main():
    relaunch_with_hidden_console()
    parser = argparse.ArgumentParser(description="Amazon Price Tracker")
    sub = parser.add_subparsers(dest="cmd")

    a = sub.add_parser("add", help="Agregar o actualizar un producto")
    a.add_argument("url")
    a.add_argument("--target", type=float, required=True, help="Precio meta")
    a.add_argument("--normal", type=float, help="Precio normal")
    a.add_argument("--name", help="Nombre corto")

    r = sub.add_parser("remove", help="Eliminar un producto")
    r.add_argument("asin")

    sub.add_parser("list", help="Listar productos")
    sub.add_parser("test-notify", help="Mostrar una notificación de prueba")
    sub.add_parser("track", help="Revisar precios (por defecto)")

    args = parser.parse_args()
    handlers = {"add": cmd_add, "remove": cmd_remove, "list": cmd_list,
                "test-notify": cmd_test_notify, "track": cmd_track, None: cmd_track}
    try:
        handlers[args.cmd](args)
    except Exception:
        log.exception("Error inesperado")
        sys.exit(1)


if __name__ == "__main__":
    main()
