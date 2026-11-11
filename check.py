"""
Platenjacht: zoekt eens per week per album de goedkoopste LP (incl. verzending)
en stuurt op maandagochtend een overzicht met de beste deals en laagste prijzen.

Draait via GitHub Actions. Leest albums.json en config.json, schrijft data/prices.json.

Omgevingsvariabelen (GitHub Secrets):
  TAVILY_API_KEY      verplicht, voor zoeken op het web
  GEMINI_API_KEY      verplicht (gratis), Gemini haalt de prijzen uit de gevonden pagina's
  ANTHROPIC_API_KEY   optioneel, alleen als je liever Claude gebruikt dan Gemini
  DISCOGS_TOKEN       optioneel, voor Discogs marketplace prijzen
  RESEND_API_KEY      optioneel, voor e-mailmeldingen
  NOTIFY_EMAIL        optioneel, adres waar meldingen heen gaan
  SCOPE               "all" (standaard) of "new" (alleen albums die nog nooit gecheckt zijn)
  SEND_DIGEST         "true" om na afloop de weekmail te sturen (op maandag via de planning)
  MODE                "full" (grondig, standaard) of "light" (snelle tussentijdse check)
  ALBUM               id van een album om alleen dat album te checken
  SITE_URL            optioneel, link naar je website in de mail
"""

import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
from urllib.parse import urlparse

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALBUMS_FILE = ROOT / "albums.json"
PRICES_FILE = ROOT / "data" / "prices.json"
CONFIG_FILE = ROOT / "config.json"

TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")
SCOPE = os.environ.get("SCOPE", "all").strip().lower() or "all"
SITE_URL = os.environ.get("SITE_URL", "")
SEND_DIGEST = os.environ.get("SEND_DIGEST", "false").strip().lower() == "true"
MODE = (os.environ.get("MODE") or "full").strip().lower()
ALBUM_ID = (os.environ.get("ALBUM") or "").strip()


def depth():
    return "basic" if MODE == "light" else CONFIG.get("zoekdiepte", "advanced")


def queries():
    q = CONFIG.get("web_queries", ["{artist} {title} vinyl LP"])
    return q[:1] if MODE == "light" else q


def shops():
    return [] if MODE == "light" else CONFIG.get("vaste_winkels", [])
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")

UA = "Platenjacht/1.0 +https://github.com"
HISTORY_LIMIT = 180


# ---------- hulpjes ----------

def log(*a):
    print(*a, flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def euro(x):
    return "€ " + f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def is_url(s):
    return bool(re.match(r"^https?://", (s or "").strip(), re.I))


def host(url):
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def parse_json(text):
    """Haalt het eerste JSON-object of -array uit een tekst."""
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if not starts:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[min(starts):])
        return value
    except Exception:
        return None


def num(v):
    try:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            v = v.replace("€", "").replace(",", ".").strip()
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


# ---------- wisselkoersen ----------

_RATES = None


def to_eur(amount, currency):
    global _RATES
    if amount is None:
        return None
    cur = (currency or "EUR").upper().strip()
    if cur in ("EUR", "€"):
        return round(amount, 2)
    if _RATES is None:
        try:
            r = requests.get("https://api.frankfurter.app/latest?from=EUR", timeout=20)
            _RATES = r.json().get("rates", {})
        except Exception as e:
            log("  wisselkoersen niet beschikbaar:", e)
            _RATES = {}
    symbol_map = {"$": "USD", "£": "GBP", "¥": "JPY"}
    cur = symbol_map.get(cur, cur)
    rate = _RATES.get(cur)
    if not rate:
        return None
    return round(amount / rate, 2)


# ---------- externe diensten ----------

_LAST_AI_CALL = 0.0


def ask_ai(prompt, max_tokens=2500):
    """Gemini (gratis) als die sleutel er is, anders Claude."""
    global _LAST_AI_CALL
    wait = 5 - (time.time() - _LAST_AI_CALL)  # rustig aan voor de gratis limieten
    if wait > 0:
        time.sleep(wait)
    _LAST_AI_CALL = time.time()
    if GEMINI_KEY:
        return ask_gemini(prompt, max_tokens)
    return ask_claude(prompt, max_tokens)


def ask_gemini(prompt, max_tokens=2500):
    models = [CONFIG.get("gemini_model", "gemini-2.5-flash-lite"), "gemini-flash-lite-latest", "gemini-flash-latest"]
    last_err = None
    for model in dict.fromkeys(models):
        for attempt in range(3):
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "temperature": 0,
                        "maxOutputTokens": max_tokens,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=180,
            )
            if r.status_code == 429:
                log("  Gemini-limiet bereikt, even wachten...")
                time.sleep(30 * (attempt + 1))
                continue
            if r.status_code == 404:
                last_err = f"model {model} niet gevonden"
                break  # probeer het volgende model
            if r.status_code >= 400:
                raise RuntimeError(f"Gemini {r.status_code}: {r.text[:300]}")
            data = r.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        else:
            last_err = "Gemini-limiet bleef bereikt"
    raise RuntimeError(f"Gemini werkt niet: {last_err}")


def ask_claude(prompt, max_tokens=2500):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


CREDITS_USED = 0


def tavily_search(query, domains=None):
    global CREDITS_USED
    body = {
        "query": query,
        "search_depth": depth(),
        "max_results": 5 if domains else 8,
        "include_raw_content": True,
    }
    if domains:
        body["include_domains"] = domains
    r = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_KEY}"},
        json=body,
        timeout=90,
    )
    CREDITS_USED += 2 if depth() == "advanced" else 1
    if r.status_code >= 400:
        raise RuntimeError(f"Tavily {r.status_code}: {r.text[:300]}")
    return r.json().get("results", [])


def tavily_extract(url):
    global CREDITS_USED
    CREDITS_USED += 1
    r = requests.post(
        "https://api.tavily.com/extract",
        headers={"Authorization": f"Bearer {TAVILY_KEY}"},
        json={"urls": [url]},
        timeout=90,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Tavily extract {r.status_code}: {r.text[:300]}")
    results = r.json().get("results", [])
    return (results[0].get("raw_content") or "") if results else ""


def discogs_get(path, params=None):
    r = requests.get(
        "https://api.discogs.com" + path,
        params=params or {},
        headers={"User-Agent": UA, "Authorization": f"Discogs token={DISCOGS_TOKEN}"},
        timeout=40,
    )
    time.sleep(1.1)  # Discogs staat ongeveer 60 verzoeken per minuut toe
    if r.status_code == 429:
        time.sleep(30)
        return discogs_get(path, params)
    r.raise_for_status()
    return r.json()


def send_mail(subject, html_body):
    if not (RESEND_KEY and NOTIFY_EMAIL):
        log("Geen RESEND_API_KEY of NOTIFY_EMAIL ingesteld, mail overgeslagen.")
        return False
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
        json={
            "from": CONFIG.get("email_from", "Platenjacht <onboarding@resend.dev>"),
            "to": [e.strip() for e in NOTIFY_EMAIL.split(",") if e.strip()],
            "subject": subject,
            "html": html_body,
        },
        timeout=30,
    )
    if r.status_code >= 400:
        log("Mail versturen mislukt:", r.status_code, r.text[:300])
        return False
    return True


# ---------- tekst rond prijzen uitknippen ----------

PRICE_HINT = re.compile(r"(€|eur\b|\$|£|price|prijs|verzend|shipping|op voorraad|in stock|uitverkocht|sold out|vinyl|\blp\b)", re.I)


def price_context(raw, limit=2500):
    """Houdt alleen regels die over prijs/voorraad/vinyl gaan, zodat de prompt klein blijft."""
    if not raw:
        return ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    keep = []
    for i, line in enumerate(lines):
        if PRICE_HINT.search(line):
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(lines) and lines[j] not in keep:
                    keep.append(lines[j][:300])
    text = "\n".join(keep) if keep else raw[:1500]
    return text[:limit]


# ---------- album herkennen ----------

def resolve_album(entry, state):
    """Bepaalt artiest en titel. Bij een link wordt de pagina ook gelezen (en eventueel als aanbieding gebruikt)."""
    raw_input = (entry.get("input") or "").strip()
    link_offer = None

    if is_url(raw_input):
        page = ""
        try:
            page = tavily_extract(raw_input)
        except Exception as e:
            log("  pagina lezen mislukt:", e)
        prompt = f"""A user pasted this link to say which vinyl album they want to track:
{raw_input}

Page text (may be empty or partial):
\"\"\"{price_context(page, 4000) or page[:3000]}\"\"\"

Return ONLY a JSON object:
{{"artist": str, "title": str,
  "is_product_page": bool,  // true if this page sells this exact record
  "shop": str or null, "price": number or null, "currency": str or null,
  "shipping": number or null, "condition": "new" or "used" or null,
  "in_stock": true/false/null, "ship_region": "NL" or "EU" or "other" or "unknown"}}
Use the URL slug if the page text is empty. Only fill price if it literally appears in the page text."""
        info = parse_json(ask_ai(prompt, 600)) or {}
        artist = (info.get("artist") or state.get("artist") or "").strip()
        title = (info.get("title") or state.get("title") or "").strip()
        if info.get("is_product_page") and num(info.get("price")):
            link_offer = {
                "shop": info.get("shop") or host(raw_input),
                "url": raw_input,
                "price": num(info.get("price")),
                "currency": info.get("currency") or "EUR",
                "shipping": num(info.get("shipping")),
                "ship_region": info.get("ship_region") or "unknown",
                "condition": info.get("condition") or "new",
                "in_stock": info.get("in_stock"),
                "is_sale": False,
                "original_price": None,
                "edition": None,
                "source": "jouw link",
            }
        return artist, title, link_offer

    if state.get("artist") and state.get("input") == raw_input:
        return state["artist"], state.get("title", ""), None

    parts = re.split(r"\s+[-–]\s+", raw_input, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip(), None

    info = parse_json(ask_ai(
        f'Split this music album description into artist and album title. Text: "{raw_input}". '
        'Return ONLY JSON: {"artist": str, "title": str}. If no artist is given, use the best known artist for that album.',
        200,
    )) or {}
    return (info.get("artist") or "").strip(), (info.get("title") or raw_input).strip(), None


# ---------- aanbiedingen zoeken ----------

# ---------- kleurvarianten ----------

COLORS = [
    (("black", "zwart"), "#161214"), (("red", "rood", "rode", "oxblood", "maroon", "burgundy", "bordeaux"), "#B3262E"),
    (("blue", "blauw", "blauwe", "navy", "cobalt"), "#2E5FA8"), (("sky", "baby blue", "light blue"), "#7FB2E0"),
    (("teal", "turquoise", "aqua", "cyan", "seafoam", "sea glass", "coke bottle"), "#2A9D8F"),
    (("green", "groen", "groene", "emerald", "olive", "forest", "mint"), "#3F7A3A"),
    (("yellow", "geel", "gele", "lemon", "banana", "mustard"), "#E6C229"), (("gold", "goud", "gouden"), "#C9A23A"),
    (("orange", "oranje", "tangerine", "amber"), "#E07B24"), (("pink", "roze", "rose", "magenta", "fuchsia"), "#E07AA0"),
    (("purple", "paars", "paarse", "violet", "lilac", "lavender", "grape"), "#6B3FA0"),
    (("white", "wit", "witte", "milky", "cream", "bone", "ivory"), "#EFE6D6"),
    (("silver", "zilver", "grey", "gray", "grijs", "smoke", "smokey", "smoky"), "#8E8E92"),
    (("brown", "bruin", "tan", "coffee", "cola", "beer", "bronze"), "#7A4E2D"),
    (("clear", "transparent", "translucent", "transparant", "doorzichtig", "doorschijnend", "crystal"), "#D9D2C4"),
]
PATTERNS = [
    (("marble", "marbled", "swirl", "gemarmerd", "marmer", "galaxy", "smoke"), "marble"),
    (("splatter", "splattered", "splash", "spatter", "spetter", "gespikkeld"), "splatter"),
    (("split", "half", "side-by-side", "sides"), "split"),
    (("picture disc", "picture"), "picture"),
    (("clear", "transparent", "translucent", "transparant", "crystal", "doorzichtig", "doorschijnend"), "clear"),
]
SKIP_TOKENS = re.compile(
    r"^(\d*x?lp|album|compilation|ltd|limited( edition)?|re|rm|reissue|remastered|gatefold|stereo|mono|promo|"
    r"unofficial release|club edition|deluxe( edition)?|special edition|numbered|test pressing|vinyl|12\"|7\"|10\"|"
    r"33 ⅓ rpm|45 rpm|180 ?(gram|g)|140 ?g|cd|cassette|mixed|single|ep|misprint|repress|anniversary edition|"
    r"box set|all media|hand numbered|signed)$", re.I)


def variant_info(text):
    """Leest uit een formaatomschrijving welke kleur vinyl het is.
    Geeft {'label', 'key', 'colors', 'pattern'} terug; zonder kleur is het standaard zwart."""
    raw = (text or "").lower()
    found = []
    for words, hexcode in COLORS:
        for w in words:
            if re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", raw):
                if hexcode not in found:
                    found.append(hexcode)
                break
    pattern = "solid"
    for words, name in PATTERNS:
        if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", raw) for w in words):
            pattern = name
            break
    if pattern == "picture":
        return {"label": "Picture disc", "key": "picture", "colors": ["#C9A23A", "#B3262E"], "pattern": "picture"}
    if not found or found == ["#161214"]:
        return {"label": "Zwart", "key": "black", "colors": ["#161214"], "pattern": "solid"}
    # leesbaar label: de kleur-woorden uit de originele tekst
    tokens = [t.strip() for t in re.split(r"[,;]", text or "") if t.strip()]
    color_tokens = [t for t in tokens if not SKIP_TOKENS.match(t.strip())
                    and any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t.lower())
                            for words, _ in COLORS + PATTERNS for w in words)]
    label = " ".join(color_tokens)[:40] or "Gekleurd vinyl"
    if pattern == "solid" or pattern == "clear":
        found = [c for c in found if c != "#161214"] or found
    key = pattern + ":" + "+".join(sorted(found))
    return {"label": label[:1].upper() + label[1:], "key": key, "colors": found[:3], "pattern": pattern}


# ---------- Discogs: hoes, persingen en prijzen ----------

def discogs_lookup(artist, title):
    """Zoekt de 'master' van een album (alle persingen samen) en de hoes."""
    q = f"{artist} {title}"
    try:
        res = discogs_get("/database/search", {"q": q, "type": "master", "per_page": 5})
        for item in res.get("results", []):
            if item.get("id"):
                return {"master_id": item["id"], "cover": item.get("cover_image") or item.get("thumb")}
        res = discogs_get("/database/search", {"q": q, "type": "release", "format": "Vinyl", "per_page": 5})
        for item in res.get("results", []):
            if item.get("master_id"):
                return {"master_id": item["master_id"], "cover": item.get("cover_image")}
            if item.get("id"):
                return {"release_id": item["id"], "cover": item.get("cover_image")}
    except Exception as e:
        log("  Discogs zoeken mislukt:", e)
    return {}


def discogs_offers(artist, title, info_out=None):
    if not DISCOGS_TOKEN:
        return []
    found = discogs_lookup(artist, title)
    if info_out is not None and found.get("cover"):
        info_out["cover"] = found["cover"]
    versions = []
    if found.get("master_id"):
        try:
            res = discogs_get(f"/masters/{found['master_id']}/versions",
                              {"format": "Vinyl", "per_page": 100, "sort": "released", "sort_order": "desc"})
            versions = res.get("versions", [])
        except Exception as e:
            log("  Discogs persingen ophalen mislukt:", e)
    elif found.get("release_id"):
        versions = [{"id": found["release_id"], "format": "", "thumb": found.get("cover")}]

    # per kleurvariant een paar persingen checken, de nieuwste eerst
    limit = int(CONFIG.get("discogs_releases_to_check", 12))
    per_variant = 2 if MODE != "light" else 1
    picked, counts = [], {}
    for v in versions:
        fmt = v.get("format") or ""
        if "lp" not in fmt.lower() and "vinyl" not in fmt.lower() and fmt:
            continue
        info = variant_info(fmt)
        if counts.get(info["key"], 0) >= (3 if info["key"] == "black" else per_variant):
            continue
        counts[info["key"]] = counts.get(info["key"], 0) + 1
        picked.append((v, info))
        if len(picked) >= limit:
            break

    offers = []
    for v, info in picked:
        rid = v.get("id")
        try:
            stats = discogs_get(f"/marketplace/stats/{rid}", {"curr_abbr": "EUR"})
        except Exception as e:
            log("  Discogs prijs mislukt voor", rid, e)
            continue
        lp = stats.get("lowest_price") or {}
        price = num(lp.get("value"))
        if not price or not stats.get("num_for_sale") or stats.get("blocked_from_sale"):
            continue
        bits = [b for b in [str(v.get("released") or "")[:4], v.get("country")] if b]
        offers.append({
            "shop": "Discogs",
            "url": f"https://www.discogs.com/sell/release/{rid}",
            "price": price,
            "currency": lp.get("currency") or "EUR",
            "shipping": None,
            "ship_region": "discogs",
            "condition": "used",
            "in_stock": True,
            "is_sale": False,
            "original_price": None,
            "edition": " ".join(bits) or None,
            "variant_text": v.get("format") or "",
            "thumb": v.get("thumb") or None,
            "source": f"{stats.get('num_for_sale')} te koop op Discogs",
        })
    return offers


EXTRACT_PROMPT = """You extract shop offers for one specific vinyl record from web search results.

Album: {artist} - {title}

Return ONLY a JSON array. One item per offer:
{{"shop": str, "url": str, "price": number, "currency": "EUR"|"USD"|"GBP"|other ISO code,
  "shipping": number or null, "ship_region": "NL"|"EU"|"other"|"unknown",
  "condition": "new"|"used", "in_stock": true|false|null,
  "is_sale": bool, "original_price": number or null, "edition": short string or null,
  "variant": vinyl color or special edition as written on the page (e.g. "Red translucent", "Black", "Splatter") or null}}

Rules:
- Only vinyl LP versions of exactly this album. Any pressing or color edition is fine. Never CD, cassette, singles, box sets of other albums, merch or other albums.
- The price must literally appear in the text for that product. Never guess.
- "url" must be the URL of the result the offer was found on.
- "shipping": shipping cost to the Netherlands, only if explicitly stated, otherwise null. Free shipping stated = 0.
- "ship_region": where the shop ships from, judged from the shop and domain (.nl = NL, other EU countries = EU).
- "is_sale": true only if the page marks it as discounted (sale, korting, aanbieding, was/now price).
- Marketplace pages listing many sellers: give the lowest listed price.
- If nothing qualifies, return [].

Results:
{results}"""


def web_offers(artist, title):
    if not (TAVILY_KEY and (GEMINI_KEY or ANTHROPIC_KEY)):
        return []
    seen, blocks = set(), []
    searches = [(q.format(artist=artist, title=title), None) for q in queries()]
    searches += [(f"{artist} {title} vinyl LP", [d]) for d in shops()]
    for query, domains in searches:
        try:
            results = tavily_search(query, domains)
        except Exception as e:
            log("  zoeken mislukt:", e)
            continue
        for res in results:
            url = res.get("url", "")
            if not url or url in seen or "discogs.com" in url:
                continue  # Discogs halen we betrouwbaarder via de API
            seen.add(url)
            body = price_context(res.get("raw_content") or "") or (res.get("content") or "")[:800]
            blocks.append(f"### {res.get('title', '')}\nURL: {url}\nSnippet: {(res.get('content') or '')[:400]}\nPage: {body}")
    if not blocks:
        return []
    text = ask_ai(EXTRACT_PROMPT.format(artist=artist, title=title, results="\n\n".join(blocks)[:60000]), 3000)
    data = parse_json(text)
    if not isinstance(data, list):
        log("  Claude gaf geen bruikbare lijst terug")
        return []
    out = []
    for o in data:
        if not isinstance(o, dict):
            continue
        price = num(o.get("price"))
        if not price or not o.get("url"):
            continue
        out.append({
            "shop": o.get("shop") or host(o["url"]),
            "url": o["url"],
            "price": price,
            "currency": o.get("currency") or "EUR",
            "shipping": num(o.get("shipping")),
            "ship_region": o.get("ship_region") or "unknown",
            "condition": "used" if o.get("condition") == "used" else "new",
            "in_stock": o.get("in_stock"),
            "is_sale": bool(o.get("is_sale")),
            "original_price": num(o.get("original_price")),
            "edition": o.get("edition"),
            "variant_text": o.get("variant") or "",
            "source": "web",
        })
    return out


def finalize(offers):
    """Rekent alles om naar euro's, telt verzending op en sorteert op totaalprijs."""
    est = CONFIG.get("shipping_estimates_eur", {})
    clean, seen = [], set()
    for o in offers:
        if o.get("in_stock") is False:
            continue
        price = to_eur(o["price"], o.get("currency"))
        if price is None or not (2 <= price <= 1000):
            continue
        ship = to_eur(o["shipping"], o.get("currency")) if o.get("shipping") is not None else None
        estimated = ship is None
        if estimated:
            ship = float(est.get(o.get("ship_region") or "unknown", est.get("unknown", 8)))
        key = (o["url"], round(price, 2))
        if key in seen:
            continue
        seen.add(key)
        orig = to_eur(o["original_price"], o.get("currency")) if o.get("original_price") else None
        vinfo = variant_info(o.get("variant_text") or o.get("edition") or "")
        clean.append({
            "variant": vinfo,
            "thumb": o.get("thumb"),
            "shop": str(o.get("shop"))[:60],
            "url": o["url"],
            "price": round(price, 2),
            "shipping": round(ship, 2),
            "shipping_estimated": estimated,
            "total": round(price + ship, 2),
            "condition": o.get("condition", "new"),
            "is_sale": bool(o.get("is_sale")),
            "original_price": orig,
            "edition": o.get("edition"),
            "source": o.get("source"),
        })
    clean.sort(key=lambda x: x["total"])
    return clean


# ---------- deal-logica ----------

def deal_reasons(album_entry, prev, cheapest):
    """Bepaalt of we moeten mailen. Retourneert een lijst met redenen (leeg = niet mailen)."""
    if not cheapest:
        return []
    total = cheapest["total"]
    last_notified = prev.get("last_notified_total")
    reasons = []

    target = num(album_entry.get("target_price"))
    if target and total <= target and (last_notified is None or total < last_notified - 0.01):
        reasons.append(f"onder je wensprijs van {euro(target)}")

    lowest = prev.get("lowest_ever")
    drop = float(CONFIG.get("drop_percent", 10))
    if lowest and total <= lowest * (1 - drop / 100):
        reasons.append(f"{round((1 - total / lowest) * 100)}% onder de laagste prijs tot nu toe ({euro(lowest)})")

    if CONFIG.get("notify_on_sale_label", True) and cheapest.get("is_sale"):
        if cheapest["url"] not in prev.get("notified_sale_urls", []):
            was = f", was {euro(cheapest['original_price'])}" if cheapest.get("original_price") else ""
            reasons.append(f"als aanbieding gemarkeerd bij {cheapest['shop']}{was}")
    return reasons


# ---------- tegoed verdelen ----------

def web_cost(entry):
    per = 2 if depth() == "advanced" else 1
    cost = per * (len(queries()) + len(shops()))
    if is_url(entry.get("input", "")):
        cost += 1
    return max(cost, 1)


def plan_web_checks(albums, state, usage):
    """Kiest welke albums vandaag op het web gezocht worden, binnen het maandbudget.
    Nieuwe albums gaan voor, daarna de albums die het langst niet gecheckt zijn."""
    budget = int(CONFIG.get("tavily_monthly_budget", 900))
    today = dt.date.today()
    next_month = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    mondays_left = sum(1 for i in range((next_month - today).days)
                       if (today + dt.timedelta(days=i)).weekday() == 0) or 1
    left = max(budget - usage.get("tavily_credits", 0), 0)
    allowance = left if (SCOPE == "new" or ALBUM_ID) else left / mondays_left
    order = sorted(
        [a for a in albums if a.get("id") and (not ALBUM_ID or a["id"] == ALBUM_ID)],
        key=lambda a: (state.get(a["id"], {}).get("web_checked") is not None,
                       state.get(a["id"], {}).get("web_checked") or ""),
    )
    chosen, spent = set(), 0
    for a in order:
        if SCOPE == "new" and state.get(a["id"], {}).get("checked"):
            continue
        c = web_cost(a)
        if spent + c > allowance and chosen:
            break
        if spent + c > left:
            break
        chosen.add(a["id"])
        spent += c
    log(f"Tavily: {usage.get('tavily_credits', 0)} van {budget} credits gebruikt deze maand, "
        f"deze ronde ruimte voor ongeveer {allowance:.0f}, {len(chosen)} album(s) op het web.")
    return chosen


# ---------- hoofdprogramma ----------

WEB_MAX_AGE_DAYS = 21


def check_album(entry, prev, do_web):
    if do_web or not prev.get("artist"):
        artist, title, link_offer = resolve_album(entry, prev)
    else:
        artist, title, link_offer = prev["artist"], prev.get("title", ""), None
    if not artist and not title:
        raise RuntimeError("Kon niet bepalen welk album dit is. Probeer 'Artiest - Album'.")
    log(f"  = {artist} - {title}" + ("" if do_web else " (alleen Discogs deze keer)"))
    info = {}
    fresh = discogs_offers(artist, title, info)
    if do_web:
        web = []
        if link_offer:
            web.append(link_offer)
        web += web_offers(artist, title)
        for o in web:
            o["seen"] = now_iso()[:10]
        web_final = finalize(web)
        for o in web_final:
            o["seen"] = now_iso()[:10]
        if MODE == "light":
            # snelle check vindt minder winkels: eerdere vondsten bewaren als ze recent zijn
            cutoff = (dt.date.today() - dt.timedelta(days=WEB_MAX_AGE_DAYS)).isoformat()
            urls = {o["url"] for o in web_final}
            web_final += [o for o in prev.get("offers", []) if o.get("shop") != "Discogs"
                          and o["url"] not in urls and (o.get("seen") or "") >= cutoff]
    else:
        # webprijzen van eerder bewaren, zolang ze niet te oud zijn
        cutoff = (dt.date.today() - dt.timedelta(days=WEB_MAX_AGE_DAYS)).isoformat()
        web_final = [o for o in prev.get("offers", []) if o.get("shop") != "Discogs" and (o.get("seen") or "") >= cutoff]
        if link_offer:
            extra = finalize([link_offer])
            for o in extra:
                o["seen"] = now_iso()[:10]
            web_final = [o for o in web_final if o["url"] != link_offer["url"]] + extra
    all_offers = finalize(fresh) + web_final
    all_offers.sort(key=lambda x: x["total"])
    return artist, title, all_offers, info.get("cover")


def group_variants(offers):
    """Per kleurvariant de goedkoopste aanbieding."""
    groups = {}
    for o in offers:
        v = o.get("variant") or {"key": "black", "label": "Zwart", "colors": ["#161214"], "pattern": "solid"}
        g = groups.get(v["key"])
        if not g:
            groups[v["key"]] = g = {**v, "count": 0, "best": None, "thumb": None}
        g["count"] += 1
        if g["best"] is None or o["total"] < g["best"]["total"]:
            g["best"] = {k: o[k] for k in ("shop", "url", "total", "condition")}
        g["thumb"] = g["thumb"] or o.get("thumb")
    out = sorted(groups.values(), key=lambda g: (g["key"] != "black", g["best"]["total"]))
    return out


def main():
    if not TAVILY_KEY or not (GEMINI_KEY or ANTHROPIC_KEY):
        log("TAVILY_API_KEY en GEMINI_API_KEY zijn verplicht. Zie README.")
        sys.exit(1)

    albums = load_json(ALBUMS_FILE, [])
    db = load_json(PRICES_FILE, {"updated": None, "albums": {}})
    state = db.setdefault("albums", {})

    ids = {a["id"] for a in albums if a.get("id")}
    for gone in [k for k in state if k not in ids]:
        del state[gone]

    month = dt.date.today().strftime("%Y-%m")
    usage = db.get("usage") or {}
    if usage.get("month") != month:
        usage = {"month": month, "tavily_credits": 0}
    web_ids = plan_web_checks(albums, state, usage)

    hits = []
    for entry in albums:
        aid = entry.get("id")
        if not aid:
            continue
        prev = state.get(aid, {})
        if SCOPE == "new" and prev.get("checked"):
            continue
        if ALBUM_ID and aid != ALBUM_ID:
            continue
        log(f"> {entry.get('input')}")
        try:
            artist, title, offers, cover = check_album(entry, prev, aid in web_ids)
        except Exception as e:
            log("  fout:", e)
            prev.update({"input": entry.get("input"), "error": str(e)[:300], "checked": now_iso()})
            state[aid] = prev
            continue

        cheapest = offers[0] if offers else None
        reasons = deal_reasons(entry, prev, cheapest)

        rec = dict(prev)
        rec.update({
            "input": entry.get("input"),
            "artist": artist,
            "title": title,
            "offers": offers[:30],
            "variants": group_variants(offers),
            "cover": cover or prev.get("cover"),
            "cheapest": cheapest,
            "checked": now_iso(),
            "error": None,
        })
        if aid in web_ids:
            rec["web_checked"] = now_iso()
        if cheapest:
            hist = rec.get("history", [])
            hist.append({"date": now_iso()[:10], "total": cheapest["total"], "shop": cheapest["shop"]})
            rec["history"] = hist[-HISTORY_LIMIT:]
            low = prev.get("lowest_ever")
            rec["lowest_ever"] = cheapest["total"] if low is None else min(low, cheapest["total"])
        if reasons:
            rec["last_notified_total"] = cheapest["total"]  # wordt alleen nog gebruikt om deals te markeren
            rec["last_deal"] = {"date": now_iso()[:10], "reasons": reasons, "total": cheapest["total"]}
            if cheapest.get("is_sale"):
                rec["notified_sale_urls"] = (prev.get("notified_sale_urls", []) + [cheapest["url"]])[-50:]
            hits.append((rec, reasons))
        state[aid] = rec
        log(f"  {len(offers)} aanbiedingen, goedkoopst: {euro(cheapest['total']) if cheapest else 'geen'}")

    usage["tavily_credits"] = usage.get("tavily_credits", 0) + CREDITS_USED
    usage["budget"] = int(CONFIG.get("tavily_monthly_budget", 900))
    db["usage"] = usage
    db["updated"] = now_iso()
    PRICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRICES_FILE.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")

    if SEND_DIGEST:
        send_digest(albums, state)
    else:
        log("Geen weekmail deze keer (die gaat alleen op maandag).")


# ---------- weekmail ----------

def week_ago_total(rec):
    """Laagste totaalprijs van ongeveer een week geleden, voor de vergelijking."""
    hist = rec.get("history", [])[:-1]
    if not hist:
        return None
    cutoff = (dt.date.today() - dt.timedelta(days=6)).isoformat()
    older = [h for h in hist if h["date"] <= cutoff]
    return (older or hist)[-1]["total"]


def best_of(offers, condition):
    pick = [o for o in offers if o.get("condition") == condition]
    return pick[0] if pick else None


def send_digest(albums, state):
    rows = []
    for entry in albums:
        rec = state.get(entry.get("id"), {})
        c = rec.get("cheapest")
        if not rec.get("artist"):
            continue
        labels, score = [], 0
        prev = week_ago_total(rec)
        target = num(entry.get("target_price"))
        if c:
            if target and c["total"] <= target:
                labels.append(f"onder je wensprijs van {euro(target)}"); score += 3
            if prev and c["total"] < prev - 0.01:
                pct = round((1 - c["total"] / prev) * 100)
                labels.append(f"{pct}% goedkoper dan vorige week ({euro(prev)})"); score += 2 if pct >= 5 else 1
            elif prev and c["total"] > prev + 0.01:
                labels.append(f"duurder dan vorige week ({euro(prev)})")
            earlier = [h["total"] for h in rec.get("history", [])[:-1]]
            if earlier and c["total"] < min(earlier) - 0.01:
                labels.append("laagste prijs tot nu toe"); score += 1
            if c.get("is_sale"):
                was = f", was {euro(c['original_price'])}" if c.get("original_price") else ""
                labels.append(f"aanbieding{was}"); score += 2
        rows.append((score, entry, rec, labels))

    if not rows:
        log("Nog geen albums met resultaten, geen weekmail.")
        return
    rows.sort(key=lambda r: (-r[0], r[2]["cheapest"]["total"] if r[2].get("cheapest") else 9999))
    deals = sum(1 for r in rows if r[0] >= 2)

    WINE, WINE2, WINE3, CREAM, DIM, AMBER, GOLD, LEAF = "#2A0B12", "#3D111A", "#5A1824", "#F2E2C4", "#C8A98B", "#E09A3A", "#EBC665", "#7DAE52"
    serif = "Georgia, Times New Roman, serif"

    def ship_text(o):
        if o["shipping_estimated"]:
            return "verzending geschat"
        return "gratis verzending" if o["shipping"] == 0 else f"incl. {euro(o['shipping'])} verzending"

    def chip(v, price=None):
        col = (v.get("colors") or ["#161214"])[0]
        ring = "#8E7A6A" if col == "#161214" else col
        txt = f"{v.get('label', 'Zwart')}" + (f" {euro(price)}" if price else "")
        return (f"<span style='display:inline-block;margin:0 6px 6px 0;padding:3px 10px 3px 6px;border-radius:12px;"
                f"background:{WINE};border:1px solid {WINE3};font-size:12px;color:{CREAM};white-space:nowrap'>"
                f"<span style='display:inline-block;width:10px;height:10px;border-radius:5px;background:{col};"
                f"border:1px solid {ring};vertical-align:-1px;margin-right:5px'></span>{txt}</span>")

    blocks = []
    for score, entry, rec, labels in rows:
        c = rec.get("cheapest")
        deal = score >= 2
        sticker_bg = GOLD if deal else AMBER
        cover = rec.get("cover")
        cover_html = (f"<img src='{cover}' width='92' height='92' alt='' style='display:block;width:92px;height:92px;border-radius:3px;object-fit:cover'>"
                      if cover else
                      f"<div style='width:92px;height:92px;border-radius:3px;background:{WINE3};color:{AMBER};font:bold 44px/92px {serif};text-align:center'>{(rec['artist'] or '?')[0]}</div>")
        if c:
            price = (f"<table role='presentation' cellpadding='0' cellspacing='0' style='margin:8px 0 4px'><tr>"
                     f"<td style='background:{sticker_bg};color:{WINE};border-radius:22px;padding:6px 14px;font:bold 20px {serif}'>{euro(c['total'])}</td>"
                     f"<td style='padding-left:10px;font-size:13.5px;color:{DIM};font-family:{serif}'>bij <a href='{c['url']}' style='color:{AMBER}'>{c['shop']}</a>, "
                     f"{'tweedehands' if c['condition'] == 'used' else 'nieuw'}<br>{ship_text(c)}</td></tr></table>")
        else:
            price = f"<div style='color:{DIM};font-size:14px;margin:8px 0'>Geen LP met prijs gevonden deze week.</div>"
        tag = ""
        if labels:
            color = GOLD if deal else (LEAF if score == 1 else DIM)
            t = "; ".join(labels)
            tag = f"<div style='color:{color};font-size:13.5px;font-weight:bold;margin:2px 0 6px'>{t[:1].upper() + t[1:]}</div>"
        variants = rec.get("variants") or []
        chips = ""
        if len(variants) > 1:
            chips = "<div style='margin-top:4px'>" + "".join(chip(v, v["best"]["total"]) for v in variants[:5]) + "</div>"
        border = GOLD if deal else WINE3
        blocks.append(
            f"<tr><td style='padding:0 0 14px'>"
            f"<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:{WINE2};border-radius:14px;border-left:4px solid {border}'>"
            f"<tr><td width='92' valign='top' style='padding:14px 0 14px 14px'>{cover_html}</td>"
            f"<td valign='top' style='padding:12px 14px 10px 16px;font-family:{serif};color:{CREAM}'>"
            f"<div style='font-size:17px;font-weight:bold;line-height:1.2'>{rec['artist']}</div>"
            f"<div style='font-size:15px;font-style:italic;color:{DIM}'>{rec.get('title', '')}</div>"
            f"{price}{tag}{chips}</td></tr></table></td></tr>"
        )

    week = dt.date.today().isocalendar()[1]
    base = SITE_URL.rstrip("/") + "/" if SITE_URL else ""
    header = (f"<img src='{base}assets/mail-header.jpg' width='600' alt='Platenjacht' style='display:block;width:100%;max-width:600px;height:auto;border-radius:18px 18px 0 0'>"
              if base else f"<div style='padding:28px;text-align:center;font:bold 36px {serif};color:{CREAM}'>Platenjacht</div>")
    intro = (f"{deals} {'deal' if deals == 1 else 'deals'} deze week, bovenaan in goud." if deals
             else "Geen opvallende deals deze week. Hieronder de laagste prijs per plaat.")
    site = (f"<p style='text-align:center;margin:8px 0 0'><a href='{base}' style='display:inline-block;background:{AMBER};color:{WINE};"
            f"text-decoration:none;font:bold 15px {serif};padding:11px 22px;border-radius:22px'>Open je platenbak</a></p>") if base else ""
    body = (
        f"<div style='background:{WINE};padding:24px 10px'>"
        f"<table role='presentation' align='center' width='600' cellpadding='0' cellspacing='0' style='max-width:600px;width:100%;background:{WINE};border:1px solid {WINE3};border-radius:18px'>"
        f"<tr><td>{header}</td></tr>"
        f"<tr><td style='padding:18px 20px 6px;font-family:{serif};color:{CREAM}'>"
        f"<div style='font-size:20px;font-weight:bold'>Week {week}</div>"
        f"<div style='font-size:15px;color:{DIM};font-style:italic;margin-top:2px'>{intro}</div></td></tr>"
        f"<tr><td style='padding:12px 20px 4px'><table role='presentation' width='100%' cellpadding='0' cellspacing='0'>{''.join(blocks)}</table></td></tr>"
        f"<tr><td style='padding:4px 20px 24px'>{site}"
        f"<p style='font-family:{serif};color:{DIM};font-size:12px;text-align:center;margin:18px 0 0'>Prijzen inclusief (soms geschatte) verzending naar Nederland. "
        f"Check prijs en verzending altijd even in de winkel zelf.</p></td></tr></table></div>"
    )
    top = rows[0]
    if deals and top[2].get("cheapest"):
        subject = f"Platenjacht: {top[2]['artist']} voor {euro(top[2]['cheapest']['total'])}" + (f" en nog {deals - 1} deal{'s' if deals > 2 else ''}" if deals > 1 else "")
    else:
        subject = f"Platenjacht week {week}: laagste prijzen voor je {len(rows)} platen"
    if send_mail(subject, body):
        log("Weekmail verstuurd.")


CONFIG = load_json(CONFIG_FILE, {})

if __name__ == "__main__":
    main()
