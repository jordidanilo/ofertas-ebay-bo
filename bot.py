"""Bot de ofertas de eBay -> Telegram.

Solo NOTIFICA: busca subastas por terminar y ofertas de "Comprar ahora"
muy por debajo del precio promedio, y te las manda a Telegram.
Nunca puja ni compra nada (solo usa la parte de la API que lee publicaciones).

También responde comandos que le escribes en Telegram
(/agregar, /quitar, /lista, /pausa, /activar). Los lee cada vez que corre.

Uso:
    python bot.py            # lee comandos, busca ofertas y avisa
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
CATEGORIA_CELULARES = 9355
ECUADOR_TZ = timezone(timedelta(hours=-5))

# eBay da 5,000 llamadas gratis al día; cada búsqueda usa ~145
MAX_BUSQUEDAS = 30


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


def env(name):
    # .strip() por si al pegar el secret se coló un Enter o un espacio
    return os.environ.get(name, "").strip()


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
    state.setdefault("agregadas", [])
    state.setdefault("quitadas", [])
    state.setdefault("pausado", False)
    state.setdefault("telegram_offset", 0)
    return state


def save_state(state):
    # Olvida avisos de hace más de 10 días para que el archivo no crezca
    limite = now_utc() - timedelta(days=10)
    state["vistos"] = {
        k: v for k, v in state["vistos"].items() if parse_date(v) > limite
    }
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)


def en_silencio(cfg):
    silencio = cfg.get("horario_silencio") or {}
    if not silencio.get("activo"):
        return False
    hora = now_utc().astimezone(ECUADOR_TZ).hour
    desde, hasta = silencio["desde"], silencio["hasta"]
    if desde < hasta:
        return desde <= hora < hasta
    return hora >= desde or hora < hasta  # cruza la medianoche, ej. 23 a 7


def is_phone(busqueda):
    return busqueda.get("categoria") == CATEGORIA_CELULARES


# ---------------------------------------------------------------- búsquedas

def active_searches(cfg, state):
    quitadas = {n.lower() for n in state["quitadas"]}
    base = [b for b in cfg["busquedas"] if b["nombre"].lower() not in quitadas]
    return base + state["agregadas"]


GRANDES = ("laptop", "macbook", "thinkpad", "notebook", "ps5", "ps4", "playstation",
           "xbox", "switch", "console", "consola", "monitor")
CELULARES = ("iphone", "pixel", "galaxy s", "galaxy z", "galaxy a", "galaxy note",
             "phone", "celular", "smartphone", "motorola", "xiaomi", "oneplus")


def nueva_busqueda(texto):
    consulta = " ".join(texto.split())
    low = consulta.lower()
    busqueda = {
        "nombre": consulta,
        "consulta": consulta,
        "pequeno": not any(p in low for p in GRANDES),
        # Las palabras con números (5800x3d, 16gb, 3600...) deben estar en el título
        "requiere": [p for p in low.split() if any(c.isdigit() for c in p)],
    }
    if "watch" not in low and any(p in low for p in CELULARES):
        busqueda["categoria"] = CATEGORIA_CELULARES
    return busqueda


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
    if is_phone(busqueda):
        # Celulares: solo los marcados como liberados en eBay
        params["aspect_filter"] = f"categoryId:{CATEGORIA_CELULARES},Network:{{Unlocked}}"
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
    if any(str(p).lower() not in titulo for p in busqueda.get("requiere", [])):
        return True
    if is_phone(busqueda) and "unlocked" not in titulo:
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

    def call(self, method, **data):
        r = requests.post(f"{self.base}/{method}", data=data, timeout=30)
        r.raise_for_status()
        return r.json()["result"]

    def send(self, texto, imagen=None):
        if imagen:
            r = requests.post(f"{self.base}/sendPhoto", data={
                "chat_id": self.chat_id, "photo": imagen, "caption": texto, "parse_mode": "HTML",
            }, timeout=30)
            if r.ok:
                return
        self.call("sendMessage", chat_id=self.chat_id, text=texto, parse_mode="HTML",
                  disable_web_page_preview="true")


# ---------------------------------------------------------------- comandos

COMANDOS = [
    ("lista", "Ver qué productos estoy buscando"),
    ("agregar", "Buscar un producto nuevo"),
    ("quitar", "Dejar de buscar un producto"),
    ("pausa", "Pausar los avisos"),
    ("activar", "Volver a activar los avisos"),
    ("ayuda", "Ver cómo usar el bot"),
]

AYUDA = (
    "🤖 <b>Comandos del bot</b>\n\n"
    "/lista: ver qué estoy buscando\n"
    "/agregar <i>producto</i>: buscar algo nuevo\n"
    "    ej. <code>/agregar Ryzen 7 5800X</code>\n"
    "/quitar <i>número o nombre</i>: dejar de buscarlo\n"
    "    ej. <code>/quitar 3</code>\n"
    "/pausa: no mandar avisos\n"
    "/activar: volver a mandar avisos\n\n"
    "⏱️ Reviso tus mensajes cada vez que busco ofertas (cada ~20 min), "
    "así que mi respuesta puede tardar un poco."
)


def find_search(busquedas, arg):
    if arg.isdigit() and 1 <= int(arg) <= len(busquedas):
        return busquedas[int(arg) - 1], None
    low = arg.lower()
    exactas = [b for b in busquedas if b["nombre"].lower() == low]
    if exactas:
        return exactas[0], None
    parciales = [b for b in busquedas if low in b["nombre"].lower()]
    if len(parciales) == 1:
        return parciales[0], None
    if parciales:
        return None, "Hay varias búsquedas que coinciden. Usa /lista y luego <code>/quitar número</code>."
    return None, "No encontré esa búsqueda. Usa /lista y luego <code>/quitar número</code>."


def handle_command(texto, cfg, state):
    cmd, _, arg = texto.partition(" ")
    cmd = cmd.lower().split("@")[0].lstrip("/")
    arg = arg.strip()
    busquedas = active_searches(cfg, state)

    if cmd == "lista":
        estado = "⏸️ En pausa" if state["pausado"] else "▶️ Activo"
        lineas = [f"{i}. {html.escape(b['nombre'])}" for i, b in enumerate(busquedas, 1)]
        return f"📋 <b>Buscando {len(busquedas)} productos</b> · {estado}\n\n" + "\n".join(lineas)

    if cmd == "agregar":
        if not arg:
            return "Escribe qué buscar, por ejemplo:\n<code>/agregar Ryzen 7 5800X</code>"
        if any(b["nombre"].lower() == arg.lower() for b in busquedas):
            return f"Ya estoy buscando <b>{html.escape(arg)}</b>. 👍"
        quitada = next((n for n in state["quitadas"] if n.lower() == arg.lower()), None)
        if quitada:
            state["quitadas"].remove(quitada)
            return f"✅ Volví a buscar <b>{html.escape(quitada)}</b>."
        if len(busquedas) >= MAX_BUSQUEDAS:
            return (f"Ya hay {len(busquedas)} búsquedas, el máximo para no pasarse del límite gratis de eBay. "
                    "Quita alguna con /quitar y vuelve a intentar.")
        nueva = nueva_busqueda(arg)
        state["agregadas"].append(nueva)
        extra = "\n📱 Es celular: solo te avisaré de equipos <b>unlocked</b>." if is_phone(nueva) else ""
        tipo = "pequeño (+$9 casillero)" if nueva["pequeno"] else "grande (casillero: cotizar)"
        return (f"✅ Agregado: <b>{html.escape(nueva['nombre'])}</b>\n"
                f"📦 Producto {tipo}{extra}\n"
                "Empiezo a buscarlo en la próxima revisión.")

    if cmd == "quitar":
        if not arg:
            return "Dime cuál, por ejemplo <code>/quitar 3</code>. Usa /lista para ver los números."
        objetivo, error = find_search(busquedas, arg)
        if error:
            return error
        if objetivo in state["agregadas"]:
            state["agregadas"].remove(objetivo)
        else:
            state["quitadas"].append(objetivo["nombre"])
        state["promedios"].pop(objetivo["nombre"], None)
        return f"🗑️ Ya no busco <b>{html.escape(objetivo['nombre'])}</b>."

    if cmd in ("pausa", "pausar"):
        state["pausado"] = True
        return "⏸️ Avisos en pausa. Escribe /activar cuando quieras que vuelva a buscar."

    if cmd == "activar":
        state["pausado"] = False
        return "▶️ Avisos activados. Vuelvo a buscar ofertas."

    return AYUDA


def process_commands(telegram, cfg, state):
    try:
        telegram.call("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in COMANDOS], ensure_ascii=False))
        updates = telegram.call("getUpdates", offset=state["telegram_offset"], timeout=0)
    except requests.RequestException as e:
        print(f"No se pudieron leer los comandos de Telegram: {e}")
        return

    respuestas = []
    viejo = now_utc() - timedelta(hours=6)
    for update in updates:
        state["telegram_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        # Solo obedece mensajes de tu chat; ignora a cualquier otra persona
        if str(msg.get("chat", {}).get("id")) != telegram.chat_id:
            continue
        texto = (msg.get("text") or "").strip()
        if not texto or datetime.fromtimestamp(msg.get("date", 0), timezone.utc) < viejo:
            continue
        respuesta = handle_command(texto, cfg, state)
        if respuesta == AYUDA and AYUDA in respuestas:
            continue  # no repetir la ayuda si escribiste varios mensajes sueltos
        respuestas.append(respuesta)

    for respuesta in respuestas:
        telegram.send(respuesta)
        time.sleep(0.5)


# ---------------------------------------------------------------- principal

def main():
    telegram = Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"))
    if "--prueba" in sys.argv:
        telegram.send("✅ <b>Bot de ofertas funcionando.</b>\nTe avisaré aquí cuando encuentre algo bueno en eBay.\n\n"
                      "Escribe /ayuda para ver los comandos.")
        print("Mensaje de prueba enviado.")
        return

    cfg = load_config()
    state = load_state()
    try:
        process_commands(telegram, cfg, state)
        if state["pausado"]:
            print("Bot en pausa (/activar en Telegram para reanudar).")
            return
        if en_silencio(cfg):
            print("Horario de silencio: no se busca ni se avisa.")
            return
        if not env("EBAY_CLIENT_ID") or not env("EBAY_CLIENT_SECRET"):
            print("Faltan las claves de eBay (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET); solo se leyeron comandos.")
            return
        search_and_notify(telegram, cfg, state)
    finally:
        save_state(state)


def search_and_notify(telegram, cfg, state):
    ebay = Ebay(
        env("EBAY_CLIENT_ID"), env("EBAY_CLIENT_SECRET"),
        cfg["zip_casillero"], cfg["max_llamadas_por_ejecucion"],
    )

    enviados = 0
    try:
        for busqueda in active_searches(cfg, state):
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

    print(f"Listo: {enviados} avisos, {ebay.llamadas} llamadas a eBay.")


if __name__ == "__main__":
    main()
