# Bot de ofertas eBay → Telegram (NextBit Core)

Revisa eBay **cada 20 minutos**, gratis y sin tu PC encendida, y te avisa por Telegram de:

- ⏰ **Subastas que terminan en la próxima hora** cuyo precio actual está al menos **25% por debajo** del promedio.
- 🔥 **Publicaciones nuevas de "Comprar ahora"** al menos **30% por debajo** del promedio.

**Solo notifica.** Nunca puja ni compra nada.

---

## Paso 1: Claves de eBay (gratis)

1. Entra a **https://developer.ebay.com** y crea una cuenta (puede ser con tu misma cuenta de eBay).
2. Ve a **Hi [tu nombre] → Application Keysets**.
3. En la columna **Production**, haz clic en **Create a keyset**. Ponle cualquier nombre, por ejemplo `ofertas-bot`.
4. eBay te va a pedir configurar **"Marketplace account deletion notifications"**. Elige la opción de **exención** (*"I do not persist eBay data"* / no guardo datos de usuarios de eBay). Este bot solo guarda números de publicaciones, no datos de personas.
5. Cuando quede activo, copia:
   - **App ID (Client ID)** → será `EBAY_CLIENT_ID`
   - **Cert ID (Client Secret)** → será `EBAY_CLIENT_SECRET`

> ⚠️ Usa las claves de **Production**, no las de *Sandbox*.

## Paso 2: Bot de Telegram (gratis)

1. En Telegram busca **@BotFather** y escríbele `/newbot`.
2. Ponle un nombre (ej. `NextBit Ofertas`) y un usuario que termine en `bot` (ej. `nextbit_ofertas_bot`).
3. Te da un **token** parecido a `123456789:ABCdef...` → será `TELEGRAM_BOT_TOKEN`.
4. Abre tu bot nuevo y mándale cualquier mensaje, por ejemplo `hola`.
5. En el navegador abre esta dirección (cambia `TU_TOKEN` por tu token):
   `https://api.telegram.org/botTU_TOKEN/getUpdates`
6. Busca `"chat":{"id":` y copia el número que sigue → será `TELEGRAM_CHAT_ID`.

## Paso 3: Subir el bot a GitHub

1. En GitHub haz clic en **New repository**.
   - Nombre: `ofertas-ebay-bot`
   - Elige **Public** (así GitHub Actions es gratis sin límite de minutos; tus claves igual quedan secretas).
   - Haz clic en **Create repository**.
2. Haz clic en **uploading an existing file** y arrastra estos archivos:
   `bot.py`, `config.yaml`, `requirements.txt`, `state.json`, `.gitignore`, `README.md`
   → **Commit changes**.
3. Crea el archivo del horario automático:
   - **Add file → Create new file**
   - En el nombre escribe exactamente: `.github/workflows/ofertas.yml`
   - Pega el contenido del archivo `ofertas.yml` → **Commit changes**.

> 💡 En Mac, la carpeta `.github` está oculta en Finder. Por eso es más fácil crearla con "Create new file" como en el paso 3.

## Paso 4: Guardar tus claves en secreto

En tu repositorio: **Settings → Secrets and variables → Actions → New repository secret**.
Crea estos 4, uno por uno:

| Name | Valor |
|---|---|
| `EBAY_CLIENT_ID` | App ID de eBay |
| `EBAY_CLIENT_SECRET` | Cert ID de eBay |
| `TELEGRAM_BOT_TOKEN` | Token de @BotFather |
| `TELEGRAM_CHAT_ID` | Tu número de chat |

Nadie puede ver estos valores, aunque el repositorio sea público.

## Paso 5: Probar

1. Ve a la pestaña **Actions**. Si pregunta, haz clic en **I understand my workflows, go ahead and enable them**.
2. Elige **Ofertas eBay** → **Run workflow** → marca ✅ **Solo mandar un mensaje de prueba** → **Run workflow**.
3. Debe llegarte a Telegram: *"✅ Bot de ofertas funcionando."*
4. Vuelve a hacer **Run workflow** **sin** marcar la casilla, para una búsqueda real.

Desde ahí corre solo cada 20 minutos. 🎉

---

## Cómo cambiar lo que busca

Abre `config.yaml` en GitHub y haz clic en el lápiz ✏️. Para agregar una búsqueda, copia un bloque y cámbialo:

```yaml
  - nombre: Ryzen 9 5900X
    consulta: Ryzen 9 5900X
    categoria: 164
    pequeno: true
    requiere: ["5900x"]
    excluir: [motherboard, combo, bundle]
```

| Campo | Para qué sirve |
|---|---|
| `consulta` | Lo que escribirías en el buscador de eBay |
| `categoria` | RAM `170083` · Procesadores `164` · SSD `175669` · Consolas `139971` · Laptops `177` · Smartwatch `178893` · Celulares `9355` |
| `pequeno` | `true` suma $9 de casillero · `false` dice "cotizar" |
| `requiere` | Palabras que **sí** deben estar en el título |
| `excluir` | Palabras que **no** deben estar en el título |

**Otros ajustes útiles** (arriba del archivo):

- `descuento_minimo`: súbelo (ej. `0.35`) si te llegan demasiados avisos; bájalo (ej. `0.20`) si te llegan muy pocos.
- `minutos_antes_de_terminar`: con cuánto tiempo de anticipación avisar de subastas.
- `calificacion_minima` / `ventas_minimas`: qué tan confiable debe ser el vendedor.

**Límite:** eBay da 5,000 consultas gratis al día. Cada búsqueda usa ~145 al día, así que caben unas **30 búsquedas**.

## Si algo falla

- **Actions** → clic en la ejecución con ❌ → **buscar** → lee el mensaje de error.
- `401` o `invalid_client`: revisa `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` (deben ser de Production).
- No llegan mensajes: revisa `TELEGRAM_CHAT_ID` y que le hayas escrito primero a tu bot.
- "pocas publicaciones para calcular promedio": esa búsqueda es muy específica. Hazla más general o quita algo de `requiere`.
