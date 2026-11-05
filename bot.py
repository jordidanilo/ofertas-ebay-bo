"""Bot de ofertas de eBay -> Telegram.

Solo NOTIFICA: busca subastas por terminar y ofertas de "Comprar ahora"
muy por debajo del precio promedio, y te las manda a Telegram.
Nunca puja ni compra nada (solo usa la parte de la API que lee publicaciones).

Uso:
    python bot.py            # busca ofertas y avisa
    python bot.py --prueba   # solo manda un mensaje de prueba a Telegram
"""
import base64
import html
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
STATE_PATH = os.path.join(ROOT, "state.json")

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
# Todas las condiciones menos "For parts or not working" (7000)
CONDITION_IDS = "1000|1500|1750|2000|2010|2020|2030|2500|2750|3000|4000|5000|6000"
ECUADOR_TZ = timezone(timedelta(hours=-5))


class LimiteDeLlamadas(Exception):
    pass


# ---------------------------------------------------------------- utilidades

def now_utc():
    return datetime.now(timezone.utc)


def parse_date(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def money(value):
    return f"${value:,.2f}"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("vistos", {})
    state.setdefault("promedios", {})
    return state


def save_state(state):
    # Olvida avisos de hace más de 10 días para que el archivo no crezca
    limite = now_utc() - timedelta(days=10)
    state["vistos"] = {
        k: v for k, v in state["vistos"].items() if parse_date(v) > limite
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)


# ---------------------------------------------------------------- eBay

class Ebay:
    def __init__(self, client_id, client_secret, zip_envio, max_llamadas):
        self.session = requests.Session()
        self.llamadas = 0
        self.max_llamadas = max_llamadas
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        r = self.session.post(
            EBAY_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            timeout=30,
        )
        r.raise_for_status()
        self.session.headers.update({
            "Authorization": f"Bearer {r.json()['access_token']}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            # Para que eBay calcule el envío hasta la zona de tu casillero
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country=US,zip={zip_envio}",
        })

    def search(self, params):
        if self.llamadas >= self.max_llamadas:
            raise LimiteDeLlamadas()
        self.llamadas += 1
        r = self.session.get(EBAY_SEARCH_URL, params=params, timeout=30)
        if r.status_code == 429:
            raise LimiteDeLlamadas()
        r.raise_for_status()
        return r.json().get("itemSummaries", [])


def base_filter(busqueda, buying_option):
    partes = [
        f"buyingOptions:{{{buying_option}}}",
        f"conditionIds:{{{CONDITION_IDS}}}",
        "itemLocationCountry:US",
        "deliveryCountry:US",
    ]
    if busqueda.get("precio_maximo"):
        partes.append(f"price:[..{busqueda['precio_maximo']}],priceCurrency:USD")
    return partes


def search_params(busqueda, filtros, sort, limit):
    params = {"q": busqueda["consulta"], "filter": ",".join(filtros), "limit": limit}
    if sort:
        params["sort"] = sort
    if busqueda.get("categoria"):
        params["category_ids"] = str(busqueda["categoria"])
    return params


def parse_item(raw):
    is_auction = "AUCTION" in raw.get("buyingOptions", [])
    price_obj = raw.get("currentBidPrice") if is_auction else raw.get("price")
    price_obj = price_obj or raw.get("price") or {}

    shipping = None
    costs = [
        float(o["shippingCost"]["value"])
        for o in raw.get("shippingOptions", [])
        if o.get("shippingCost", {}).get("value") is not None
    ]
    if costs:
        shipping = min(costs)

    seller = raw.get("seller", {})
    image = (raw.get("image") or {}).get("imageUrl")
    return {
        "id": raw["itemId"],
        "titulo": raw.get("title", ""),
        "url": raw.get("itemWebUrl", ""),
        "imagen": image,
        "subasta": is_auction,
        "precio": float(price_obj.get("value", 0)),
        "envio": shipping,
        "pujas": raw.get("bidCount", 0),
        "termina": parse_date(raw.get("itemEndDate")),
        "publicado": parse_date(raw.get("itemCreationDate")),
        "condicion": raw.get("condition", ""),
        "vendedor_pct": float(seller.get("feedbackPercentage") or 0),
        "vendedor_score": int(seller.get("feedbackScore") or 0),
    }


def total_miami(item):
    return item["precio"] + (item["envio"] or 0)


def contains_word(titulo, palabra):
    # Palabra completa: "locked" no debe coincidir con "unlocked"
    patron = r"(?<![a-z])" + re.escape(palabra.lower()) + r"(?![a-z])"
    return re.search(patron, titulo) is not None


def is_junk(item, busqueda, cfg):
    titulo = item["titulo"].lower()
    palabras = cfg.get("excluir_siempre", []) + busqueda.get("excluir", [])
    if any(contains_word(titulo, str(p)) for p in palabras):
        return True
    if any(p.lower() not in titulo for p in busqueda.get("requiere", [])):
        return True
    if item["precio"] <= 0:
        return True
    vend = cfg["vendedor"]
    if item["vendedor_pct"] < vend["calificacion_minima"]:
        return True
    if item["vendedor_score"] < vend["ventas_minimas"]:
        return True
    return False


def get_promedio(ebay, busqueda, cfg, state):
    """Mediana del precio (con envío) de publicaciones "Comprar ahora" parecidas."""
    nombre = busqueda["nombre"]
    cache = state["promedios"].get(nombre)
    horas = cfg["horas_cache_promedio"]
    if cache and parse_date(cache["fecha"]) > now_utc() - timedelta(hours=horas):
        return cache["mediana"]

    raw = ebay.search(search_params(busqueda, base_filter(busqueda, "FIXED_PRICE"), "", 100))
    precios = sorted(
        total_miami(it)
        for it in map(parse_item, raw)
        if not is_junk(it, busqueda, cfg)
    )
    if len(precios) < cfg["muestras_minimas_promedio"]:
        mediana = None
    else:
        # Quita el 10% más barato y el 10% más caro (precios raros)
        corte = len(precios) // 10
        mediana = round(statistics.median(precios[corte:len(precios) - corte]), 2)

    state["promedios"][nombre] = {"mediana": mediana, "muestras": len(precios), "fecha": now_utc().isoformat()}
    return mediana


def find_auctions(ebay, busqueda, cfg, mediana):
    ahora = now_utc()
    hasta = ahora + timedelta(minutes=cfg["subastas"]["minutos_antes_de_terminar"])
    filtros = base_filter(busqueda, "AUCTION")
    filtros.append(f"itemEndDate:[..{hasta.strftime('%Y-%m-%dT%H:%M:%S.000Z')}]")
    raw = ebay.search(search_params(busqueda, filtros, "endingSoonest", 50))

    minimo = timedelta(minutes=cfg["subastas"]["minutos_minimos_restantes"])
    limite = mediana * (1 - cfg["subastas"]["descuento_minimo"])
    for item in map(parse_item, raw):
        if is_junk(item, busqueda, cfg) or not item["termina"]:
            continue
        if item["termina"] - ahora < minimo:
            continue
        if total_miami(item) <= limite:
            yield item


def find_buy_now(ebay, busqueda, cfg, mediana):
    ahora = now_utc()
    desde = ahora - timedelta(hours=cfg["comprar_ahora"]["horas_publicacion_nueva"])
    raw = ebay.search(search_params(busqueda, base_filter(busqueda, "FIXED_PRICE"), "newlyListed", 50))

    limite = mediana * (1 - cfg["comprar_ahora"]["descuento_minimo"])
    for item in map(parse_item, raw):
        if is_junk(item, busqueda, cfg):
            continue
        if item["publicado"] and item["publicado"] < desde:
            continue
        if total_miami(item) <= limite:
            yield item


# ---------------------------------------------------------------- mensajes

def tiempo_restante(termina):
    minutos = int((termina - now_utc()).total_seconds() // 60)
    if minutos >= 60:
        return f"{minutos // 60} h {minutos % 60} min"
    return f"{minutos} min"


def build_message(item, busqueda, cfg, mediana):
    t = html.escape(item["titulo"][:200])
    lineas = []
    if item["subasta"]:
        hora = item["termina"].astimezone(ECUADOR_TZ).strftime("%H:%M")
        lineas.append(f"⏰ <b>SUBASTA · termina en {tiempo_restante(item['termina'])}</b> ({hora} hora Ecuador)")
    else:
        lineas.append("🔥 <b>OFERTA · Comprar ahora</b>")
    lineas.append(f"<b>{t}</b>")
    lineas.append("")

    if item["subasta"]:
        pujas = item["pujas"]
        lineas.append(f"💵 Puja actual: {money(item['precio'])} ({pujas} {'puja' if pujas == 1 else 'pujas'})")
    else:
        lineas.append(f"💵 Precio: {money(item['precio'])}")

    if item["envio"] is None:
        lineas.append("🚚 Envío a Miami: ver en eBay")
    elif item["envio"] == 0:
        lineas.append("🚚 Envío a Miami: gratis")
    else:
        lineas.append(f"🚚 Envío a Miami: {money(item['envio'])}")

    total = total_miami(item)
    if busqueda.get("pequeno", True):
        casillero = cfg["casillero_producto_pequeno"]
        lineas.append(f"✈️ Casillero: {money(casillero)}")
        lineas.append(f"📦 <b>Total puesto en Ecuador: ~{money(total + casillero)}</b>")
    else:
        lineas.append("✈️ Casillero: cotizar (producto grande)")
        lineas.append(f"📦 <b>Total en Miami: ~{money(total)}</b>")

    descuento = round((1 - total / mediana) * 100)
    lineas.append(f"📊 Promedio en eBay: {money(mediana)} (<b>-{descuento}%</b>)")
    lineas.append(f"⭐ Vendedor: {item['vendedor_pct']:.1f}% ({item['vendedor_score']:,} ventas)")
    if item["condicion"]:
        lineas.append(f"🏷️ {html.escape(item['condicion'])}")
    lineas.append("")
    lineas.append(f"🔗 <a href=\"{html.escape(item['url'], quote=True)}\">Ver en eBay</a>")
    return "\n".join(lineas)


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def send(self, texto, imagen=None):
        if imagen:
            r = requests.post(f"{self.base}/sendPhoto", data={
                "chat_id": self.chat_id, "photo": imagen, "caption": texto, "parse_mode": "HTML",
            }, timeout=30)
            if r.ok:
                return
        r = requests.post(f"{self.base}/sendMessage", data={
            "chat_id": self.chat_id, "text": texto, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }, timeout=30)
        r.raise_for_status()


# ---------------------------------------------------------------- principal

def main():
    telegram = Telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"])
    if "--prueba" in sys.argv:
        telegram.send("✅ <b>Bot de ofertas funcionando.</b>\nTe avisaré aquí cuando encuentre algo bueno en eBay.")
        print("Mensaje de prueba enviado.")
        return

    cfg = load_config()
    state = load_state()
    ebay = Ebay(
        os.environ["EBAY_CLIENT_ID"], os.environ["EBAY_CLIENT_SECRET"],
        cfg["zip_casillero"], cfg["max_llamadas_por_ejecucion"],
    )

    enviados = 0
    try:
        for busqueda in cfg["busquedas"]:
            if enviados >= cfg["max_avisos_por_ejecucion"]:
                break
            try:
                mediana = get_promedio(ebay, busqueda, cfg, state)
                if not mediana:
                    print(f"[{busqueda['nombre']}] pocas publicaciones para calcular promedio, se omite")
                    continue
                encontrados = list(find_auctions(ebay, busqueda, cfg, mediana))
                encontrados += list(find_buy_now(ebay, busqueda, cfg, mediana))
            except requests.HTTPError as e:
                print(f"[{busqueda['nombre']}] error de eBay: {e}")
                continue

            for item in encontrados:
                if item["id"] in state["vistos"]:
                    continue
                if enviados >= cfg["max_avisos_por_ejecucion"]:
                    break
                telegram.send(build_message(item, busqueda, cfg, mediana), item["imagen"])
                state["vistos"][item["id"]] = now_utc().isoformat()
                enviados += 1
                print(f"[{busqueda['nombre']}] aviso: {item['titulo']}")
                time.sleep(1.2)  # Telegram limita mensajes por segundo
    except LimiteDeLlamadas:
        print("Se alcanzó el límite de llamadas a eBay en esta ejecución; sigue en la próxima.")
    finally:
        save_state(state)

    print(f"Listo: {enviados} avisos, {ebay.llamadas} llamadas a eBay.")


if __name__ == "__main__":
    main()
