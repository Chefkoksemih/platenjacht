"""
Platenjacht: zoekt eens per week per album de goedkoopste LP (incl. verzending)
en stuurt op maandagochtend een overzicht met de beste deals en laagste prijzen.

Draait via GitHub Actions. Leest albums.json en config.json, schrijft prices.json.
Alle bestanden staan naast elkaar in de hoofdmap van de repository.

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

ROOT = pathlib.Path(__file__).resolve().parent
ALBUMS_FILE = ROOT / "albums.json"
PRICES_FILE = ROOT / "prices.json"
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


EU_TLDS = {"nl", "be", "de", "fr", "it", "es", "pt", "at", "ie", "lu", "dk", "se", "fi", "pl", "cz", "sk",
           "hu", "si", "hr", "ro", "bg", "gr", "ee", "lv", "lt", "mt", "cy", "eu"}
EU_COUNTRIES = {t.upper() for t in EU_TLDS if t != "eu"} | {"GR", "EL"}
EU_CURRENCIES = {"EUR", "SEK", "DKK", "PLN", "CZK", "HUF", "RON", "BGN"}
# grote winkels buiten de EU (of in het VK) die we bij het zoeken al overslaan
NON_EU_DOMAINS = [
    "target.com", "walmart.com", "amazon.com", "amazon.co.uk", "amazon.ca", "amazon.com.au", "barnesandnoble.com",
    "bestbuy.com", "urbanoutfitters.com", "roughtrade.com", "hmv.com", "ebay.com", "ebay.co.uk", "musicdirect.com",
    "deepdiscount.com", "newburycomics.com", "vinylmeplease.com", "sound-of-vinyl.com", "recordstore.co.uk",
    "imusic.uk", "imusic.co.uk", "vovyl.com", "craigslist.org", "etsy.com", "zavvi.com", "juno.co.uk",
    "banquetrecords.com", "roughtraderecords.com", "insound.com", "acousticsounds.com", "elusivedisc.com",
    "allmusic.com", "last.fm", "spotify.com", "youtube.com", "genius.com", "wikipedia.org", "discogs.com",
    "musicbrainz.org", "rateyourmusic.com", "apple.com", "tidal.com", "deezer.com",
]


def is_eu_domain(url):
    h = host(url)
    tld = h.rsplit(".", 1)[-1] if "." in h else ""
    return tld in EU_TLDS


def is_blocked_domain(url):
    h = host(url)
    return any(h == d or h.endswith("." + d) for d in NON_EU_DOMAINS)


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


AI_FAILED = False   # wordt True als een uitleesbeurt echt niet lukte


def ask_gemini(prompt, max_tokens=2500):
    models = [CONFIG.get("gemini_model", "gemini-2.5-flash-lite"), "gemini-flash-lite-latest",
              "gemini-2.5-flash", "gemini-flash-latest"]
    last_err = None
    for model in dict.fromkeys(models):
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                    json={
                        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens,
                                             "responseMimeType": "application/json"},
                    },
                    timeout=180,
                )
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(10)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = [15, 30, 60][attempt]
                last_err = f"{model} gaf {r.status_code}"
                log(f"  Gemini druk of limiet ({r.status_code}, {model}), {wait} seconden wachten...")
                time.sleep(wait)
                continue
            if r.status_code == 404:
                last_err = f"model {model} niet gevonden"
                break
            if r.status_code >= 400:
                last_err = f"{model} gaf {r.status_code}: {r.text[:200]}"
                break
            data = r.json()
            parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        log(f"  {model} lukt nu niet, ander model proberen")
    raise RuntimeError(f"Gemini werkt nu niet: {last_err}")


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


def tavily_search(query, domains=None, exclude=None, depth="basic", country=None, max_results=8):
    global CREDITS_USED
    body = {
        "query": query,
        "search_depth": depth,
        "max_results": max_results,
        "include_raw_content": True,
    }
    if domains:
        body["include_domains"] = domains
    if exclude:
        body["exclude_domains"] = exclude[:150]
    if country:
        body["country"] = country
    r = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {TAVILY_KEY}"},
        json=body,
        timeout=90,
    )
    CREDITS_USED += 2 if depth == "advanced" else 1
    if r.status_code == 400 and country:
        # mocht het land niet geaccepteerd worden: nog een keer zonder
        body.pop("country", None)
        r = requests.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {TAVILY_KEY}"},
                          json=body, timeout=90)
        CREDITS_USED += 2 if depth == "advanced" else 1
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

def clean_artist(name):
    """Discogs zet bij dubbele namen een nummer erachter, bijvoorbeeld 'Willem (4)'."""
    return re.sub(r"\s*\(\d+\)$", "", (name or "").strip()).rstrip("*").strip()


def discogs_link(url):
    m = re.search(r"discogs\.com/(?:[a-z]{2}/)?(?:[^/]+/)?(master|release)/(\d+)", url or "", re.I)
    return (m.group(1).lower(), int(m.group(2))) if m else (None, None)


def split_text(raw):
    parts = re.split(r"\s+[-–—]\s+", raw, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    info = parse_json(ask_ai(
        f'Split this music album description into artist and album title. Text: "{raw}". '
        'Return ONLY JSON: {"artist": str, "title": str}. If no artist is given, use the best known artist for that album.',
        200,
    )) or {}
    return (info.get("artist") or "").strip(), (info.get("title") or raw).strip()


def link_page_info(url):
    """Leest een (winkel)link uit: welk album, en als het een productpagina is, de prijs."""
    page = ""
    try:
        page = tavily_extract(url)
    except Exception as e:
        log("  pagina lezen mislukt:", e)
    prompt = f"""A user pasted this link to say which vinyl album they want to track:
{url}

Page text (may be empty or partial):
\"\"\"{price_context(page, 4000) or page[:3000]}\"\"\"

Return ONLY a JSON object:
{{"artist": str, "title": str,
  "is_product_page": bool,
  "shop": str or null, "price": number or null, "currency": str or null,
  "shipping": number or null, "condition": "new" or "used" or null,
  "in_stock": true/false/null, "ships_from": ISO country code or null, "variant": str or null}}
Use the URL slug if the page text is empty. Only fill price if it literally appears in the page text."""
    info = parse_json(ask_ai(prompt, 600)) or {}
    offer = None
    if info.get("is_product_page") and num(info.get("price")):
        offer = {
            "shop": info.get("shop") or host(url), "url": url, "price": num(info.get("price")),
            "currency": info.get("currency") or "EUR", "shipping": num(info.get("shipping")),
            "ships_from": info.get("ships_from"), "condition": info.get("condition") or "new",
            "in_stock": info.get("in_stock"), "is_sale": False, "original_price": None, "edition": None,
            "variant_text": info.get("variant") or "", "source": "jouw link",
        }
    return (info.get("artist") or "").strip(), (info.get("title") or "").strip(), offer


def identify_album(entry, prev):
    """Bepaalt welk album het is en haalt bij Discogs hoes, label, barcode en persingen op.
    Geeft een dict terug met artist, title, aliases, label, barcodes, cover, pressings en eventueel link_offer."""
    raw = (entry.get("input") or "").strip()
    kind, did = discogs_link(raw)
    info = {"link_offer": None}

    if kind:
        d = discogs_by_id(kind, did)
        if d:
            info.update(d)
        else:
            # Discogs onbereikbaar: naam uit de link halen
            slug = re.sub(r"^\d+-", "", raw.rstrip("/").rsplit("/", 1)[-1]).replace("-", " ")
            info["artist"], info["title"] = prev.get("artist") or slug, prev.get("title") or ""
    elif is_url(raw):
        artist, title, offer = link_page_info(raw)
        info["link_offer"] = offer
        info["artist"], info["title"] = artist or prev.get("artist", ""), title or prev.get("title", "")
        d = discogs_search(info["artist"], info["title"]) if info["artist"] else None
        if d:
            info.update({k: v for k, v in d.items() if k not in ("artist", "title")})
    else:
        if prev.get("artist") and prev.get("input") == raw:
            artist, title = prev["artist"], prev.get("title", "")
        else:
            artist, title = split_text(raw)
        info["artist"], info["title"] = artist, title
        d = discogs_search(artist, title)
        if d:
            # Discogs-namen zijn netter gespeld; de getypte naam houden we als alias
            info.update(d)
            info["aliases"] = list(dict.fromkeys(d.get("aliases", []) + [artist]))
    info.setdefault("aliases", [])
    info["aliases"] = [a for a in dict.fromkeys(info["aliases"]) if a and a.lower() != (info.get("artist") or "").lower()]
    return info


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


# ---------- Discogs: hoes, label, barcode en persingen (geen prijzen) ----------

def discogs_pressings(master_id):
    """Alle vinylpersingen van een album, samengevat per kleurvariant."""
    try:
        res = discogs_get(f"/masters/{master_id}/versions",
                          {"format": "Vinyl", "per_page": 100, "sort": "released", "sort_order": "desc"})
    except Exception as e:
        log("  Discogs persingen ophalen mislukt:", e)
        return [], None
    seen, out, newest = {}, [], []
    for v in res.get("versions", []):
        fmt = v.get("format") or ""
        if fmt and "lp" not in fmt.lower() and "vinyl" not in fmt.lower():
            continue
        if v.get("id") and len(newest) < 3:
            newest.append(v["id"])
        info = variant_info(fmt)
        if info["key"] in seen:
            continue
        seen[info["key"]] = True
        out.append({**info, "thumb": v.get("thumb") or None,
                    "year": str(v.get("released") or "")[:4] or None, "country": v.get("country")})
    return out[:20], newest


def discogs_release_extras(release_id):
    """Label en barcode van een persing."""
    try:
        r = discogs_get(f"/releases/{release_id}")
    except Exception as e:
        log("  Discogs persing ophalen mislukt:", e)
        return {}
    barcodes = []
    for ident in r.get("identifiers", []):
        if (ident.get("type") or "").lower() == "barcode":
            digits = re.sub(r"\D", "", ident.get("value") or "")
            if len(digits) in (12, 13) and digits not in barcodes:
                barcodes.append(digits)
    labels = [clean_artist(l.get("name")) for l in r.get("labels", []) if l.get("name")]
    return {"barcodes": barcodes[:2], "label": labels[0] if labels else None, "master_id": r.get("master_id")}


def discogs_artist_aliases(artist_id):
    try:
        a = discogs_get(f"/artists/{artist_id}")
    except Exception:
        return []
    names = (a.get("namevariations") or [])[:3] + [a.get("realname")]
    return [clean_artist(n) for n in names if n]


def discogs_from_master(master_id):
    try:
        m = discogs_get(f"/masters/{master_id}")
    except Exception as e:
        log("  Discogs album ophalen mislukt:", e)
        return None
    artists = m.get("artists") or []
    images = m.get("images") or []
    cover = next((i.get("uri") for i in images if i.get("type") == "primary"), None) or (images[0].get("uri") if images else None)
    pressings, newest = discogs_pressings(master_id)
    extras, barcodes = {}, []
    for rid in (newest or [m.get("main_release")])[:3]:
        if not rid:
            continue
        e = discogs_release_extras(rid)
        extras = extras or e
        barcodes += [b for b in e.get("barcodes", []) if b not in barcodes]
    aliases = discogs_artist_aliases(artists[0]["id"]) if artists and artists[0].get("id") else []
    return {
        "artist": clean_artist(artists[0]["name"]) if artists else "",
        "title": (m.get("title") or "").strip(),
        "cover": cover, "pressings": pressings, "aliases": aliases,
        "label": extras.get("label"), "barcodes": barcodes[:3],
        "discogs_url": f"https://www.discogs.com/master/{master_id}",
    }


def discogs_nl_link(url):
    """Link naar de Discogs-marktplaats, alleen verkopers uit Nederland."""
    kind, did = discogs_link(url or "")
    if not kind:
        return None
    key = "master_id" if kind == "master" else "release_id"
    return f"https://www.discogs.com/sell/list?{key}={did}&ships_from=Netherlands&format=Vinyl"


def discogs_by_id(kind, did):
    if not DISCOGS_TOKEN:
        return None
    if kind == "master":
        return discogs_from_master(did)
    extras = discogs_release_extras(did)
    if extras.get("master_id"):
        d = discogs_from_master(extras["master_id"])
        if d:
            d["barcodes"] = list(dict.fromkeys(extras.get("barcodes", []) + d.get("barcodes", [])))[:3]
            d["label"] = extras.get("label") or d.get("label")
        return d
    try:
        r = discogs_get(f"/releases/{did}")
    except Exception:
        return None
    artists = r.get("artists") or []
    images = r.get("images") or []
    return {
        "artist": clean_artist(artists[0]["name"]) if artists else "", "title": r.get("title") or "",
        "cover": images[0].get("uri") if images else None, "pressings": [variant_info(" ".join(
            d for f in r.get("formats", []) for d in (f.get("descriptions") or []) + [f.get("text") or ""]))],
        "aliases": [], "label": extras.get("label"), "barcodes": extras.get("barcodes", []),
        "discogs_url": f"https://www.discogs.com/release/{did}",
    }


def discogs_search(artist, title):
    """Zoekt het album op Discogs: eerst precies op artiest en titel, dan breder."""
    if not DISCOGS_TOKEN or not (artist or title):
        return None
    attempts = [
        {"artist": artist, "release_title": title, "type": "master"},
        {"q": f"{artist} {title}", "type": "master"},
        {"artist": artist, "release_title": title, "type": "release", "format": "Vinyl"},
    ]
    for params in attempts:
        try:
            res = discogs_get("/database/search", {**params, "per_page": 5})
        except Exception as e:
            log("  Discogs zoeken mislukt:", e)
            return None
        for item in res.get("results", []):
            if item.get("type") == "master" and item.get("id"):
                return discogs_from_master(item["id"])
            if item.get("master_id"):
                return discogs_from_master(item["master_id"])
            if item.get("id"):
                return discogs_by_id("release", item["id"])
    return None


EXTRACT_PROMPT = """You extract shop offers for one specific vinyl record from web search results.

Album: {artist} - {title}
Artist may also be written as: {aliases}
Record label: {label}
Barcode (EAN): {barcodes}

Return ONLY a JSON array. One item per offer:
{{"shop": str, "url": str, "price": number, "currency": "EUR"|other ISO code,
  "shipping": number or null, "ships_from": ISO 3166 country code of the shop or null,
  "condition": "new"|"used", "in_stock": true|false|null,
  "is_sale": bool, "original_price": number or null, "edition": short string or null,
  "variant": vinyl color or special edition as written on the page (e.g. "Rood vinyl", "Black", "Splatter") or null}}

Rules:
- Only vinyl LP versions of exactly this album. Any pressing or color edition is fine. Never CD, cassette, singles, merch or other albums.
- A matching barcode always means it is the right record.
- The price must literally appear in the text for that product. Never guess.
- "url" must be the URL of the result the offer was found on.
- "shipping": shipping cost to the Netherlands, only if explicitly stated, otherwise null. Free shipping stated = 0.
- "ships_from": the country the shop is based in, judged from the shop, address, language and domain. null if unclear.
- "is_sale": true only if the page marks it as discounted (sale, korting, aanbieding, was/now price).
- If nothing qualifies, return [].

Results:
{results}"""


def search_plan(info, entry):
    """Licht = alleen je vaste winkels (in groepjes van vijf); grondig = ook breed zoeken, andere naam en barcode."""
    artist, title = info.get("artist", ""), info.get("title", "")
    country = CONFIG.get("zoekland", "netherlands")
    shops = CONFIG.get("vaste_winkels", [])
    size = int(CONFIG.get("winkels_per_zoekopdracht", 5))
    plan = []
    for i in range(0, len(shops), size):
        plan.append({"q": f"{artist} {title} LP vinyl", "domains": shops[i:i + size], "depth": "basic", "max": 15})
    if MODE == "light":
        return plan
    plan.append({"q": f"{artist} {title} LP vinyl kopen", "exclude": NON_EU_DOMAINS,
                 "depth": "advanced", "country": country, "max": 12})
    alias = next((a for a in info.get("aliases") or [] if a.lower() != artist.lower()), None)
    if alias:
        plan.append({"q": f"{alias} {title} LP vinyl", "exclude": NON_EU_DOMAINS, "depth": "basic", "country": country})
    for bc in (info.get("barcodes") or [])[:1]:
        plan.append({"q": bc, "exclude": NON_EU_DOMAINS, "depth": "basic", "country": country})
    return plan


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 1 or w.isdigit()]


def mentions_album(info, text):
    """Staat de albumtitel (of de barcode) op de pagina? Zo niet, dan gaat hij niet naar Gemini."""
    t = (text or "").lower()
    if any(bc in t.replace(" ", "") for bc in info.get("barcodes") or []):
        return True
    tw = words(info.get("title"))
    return bool(tw) and all(w in words(t) for w in tw)


EURO_PRICE = re.compile(r"(€\s?\d|\d[\d.]*,\d{2}\s?(€|eur)|eur\s?\d)", re.I)
SEARCH_LOG = []   # per plaat: wat er gevonden is en waarom het wel of niet meetelt


def note(url, shop, reason):
    if not any(x["url"] == url for x in SEARCH_LOG):
        SEARCH_LOG.append({"url": url, "shop": shop or host(url), "reason": reason})


def web_offers(info, entry):
    if not (TAVILY_KEY and (GEMINI_KEY or ANTHROPIC_KEY)):
        return []
    artist, title = info.get("artist", ""), info.get("title", "")
    seen, pages = set(), []
    for step in search_plan(info, entry):
        try:
            results = tavily_search(step["q"], step.get("domains"), step.get("exclude"),
                                    step.get("depth", "basic"), step.get("country"), step.get("max", 8))
        except Exception as e:
            log("  zoeken mislukt:", e)
            continue
        for res in results:
            url = res.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            if is_blocked_domain(url):
                continue
            snippet = (res.get("content") or "")[:900]
            body = price_context(res.get("raw_content") or "")
            if not mentions_album(info, (res.get("title") or "") + " " + url.replace("-", " ") + " " + snippet + " " + body):
                continue   # pagina over iets anders (bijvoorbeeld een ander album van dezelfde artiest)
            if not EURO_PRICE.search(snippet + " " + body):
                note(url, None, "geen europrijs op de pagina")
                continue
            pages.append(f"### {res.get('title', '')}\nURL: {url}\nSnippet: {snippet}\nPage: {body}")
    log(f"  {len(seen)} pagina's gevonden, {len(pages)} met een europrijs")
    if not pages:
        return []

    out, found_urls = [], set()
    global AI_FAILED
    batch = int(CONFIG.get("paginas_per_keer", 5))
    for i in range(0, len(pages), batch):
        chunk = pages[i:i + batch]
        try:
            text = ask_ai(EXTRACT_PROMPT.format(
                artist=artist, title=title, aliases=", ".join(info.get("aliases") or []) or "-",
                label=info.get("label") or "-", barcodes=", ".join(info.get("barcodes") or []) or "-",
                results="\n\n".join(chunk)), 1500)
        except Exception as e:
            log("  uitlezen mislukt:", str(e)[:200])
            AI_FAILED = True
            for page in chunk:
                url = page.split("URL: ", 1)[1].split("\n", 1)[0]
                note(url, None, "kon niet worden uitgelezen (Gemini had het te druk)")
            continue
        data = parse_json(text)
        if not isinstance(data, list):
            continue
        for o in data:
            if not isinstance(o, dict):
                continue
            price = num(o.get("price"))
            if not price or not o.get("url"):
                continue
            if not (5 <= price <= 400):
                note(o["url"], o.get("shop"), f"onwaarschijnlijke prijs ({price})")
                continue
            orig = num(o.get("original_price"))
            if orig and not (price < orig <= price * 2):
                o["original_price"], o["is_sale"] = None, False
            found_urls.add(o["url"])
            out.append({
                "shop": o.get("shop") or host(o["url"]), "url": o["url"], "price": price,
                "currency": (o.get("currency") or "EUR").upper(), "shipping": num(o.get("shipping")),
                "ships_from": (o.get("ships_from") or "").upper() or None,
                "condition": "used" if o.get("condition") == "used" else "new",
                "in_stock": o.get("in_stock"), "is_sale": bool(o.get("is_sale")),
                "original_price": num(o.get("original_price")), "edition": o.get("edition"),
                "variant_text": o.get("variant") or "", "source": "web",
            })
        for page in chunk:
            url = page.split("URL: ", 1)[1].split("\n", 1)[0]
            if url not in found_urls:
                note(url, None, "geen LP van dit album herkend")
    return out


def eu_region(o):
    """'NL', 'EU' of None (= niet uit de EU, of niet vast te stellen)."""
    url = o.get("url", "")
    if is_blocked_domain(url) or (o.get("currency") or "EUR") not in EU_CURRENCIES:
        return None
    h = host(url)
    tld = h.rsplit(".", 1)[-1] if "." in h else ""
    country = (o.get("ships_from") or "").upper()
    if tld == "nl" or country == "NL":
        return "NL"
    if country and country not in EU_COUNTRIES:
        return None
    if tld in EU_TLDS or country in EU_COUNTRIES:
        return "EU"
    if any(h == d or h.endswith("." + d) for d in CONFIG.get("vaste_winkels", [])):
        return "EU" if tld in EU_TLDS and tld != "nl" else "NL"
    return None


def finalize(offers):
    """Rekent alles om naar euro's, telt verzending op en sorteert op totaalprijs."""
    est = CONFIG.get("shipping_estimates_eur", {})
    clean, seen = [], set()
    for o in offers:
        if o.get("in_stock") is False:
            note(o.get("url", ""), o.get("shop"), "uitverkocht")
            continue
        region = o.get("region") or eu_region(o)
        if not region:
            log(f"  overgeslagen (niet uit de EU): {o.get('shop')} {host(o.get('url', ''))}")
            note(o.get("url", ""), o.get("shop"), "niet uit de EU of herkomst onbekend")
            continue
        o["ship_region"] = region
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
            "region": region,
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
    # schatting vooraf: zonder albuminfo gaan we uit van de volledige set zoekopdrachten
    fake = {"artist": "a", "title": "t", "label": "l", "aliases": ["b"], "barcodes": ["0", "1"]}
    cost = sum(2 if st.get("depth") == "advanced" else 1 for st in search_plan(fake, entry))
    if is_url(entry.get("input", "")) and not discogs_link(entry.get("input", ""))[0]:
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
    global AI_FAILED
    AI_FAILED = False
    SEARCH_LOG.clear()
    info = identify_album(entry, prev)
    if not info.get("artist") and not info.get("title"):
        raise RuntimeError("Kon niet bepalen welk album dit is. Probeer 'Artiest - Album' of een Discogs-link.")
    log(f"  = {info.get('artist')} - {info.get('title')}"
        + (f" (label {info['label']})" if info.get("label") else "")
        + (f" (barcode {info['barcodes'][0]})" if info.get("barcodes") else "")
        + ("" if do_web else " (deze keer niet op het web)"))
    cutoff = (dt.date.today() - dt.timedelta(days=WEB_MAX_AGE_DAYS)).isoformat()
    previous = [o for o in prev.get("offers", []) if o.get("shop") != "Discogs" and o.get("region")
                and (o.get("seen") or "") >= cutoff]
    if do_web:
        web = ([info["link_offer"]] if info.get("link_offer") else []) + web_offers(info, entry)
        offers = finalize(web)
        for o in offers:
            o["seen"] = now_iso()[:10]
        if MODE == "light" or AI_FAILED:
            # snelle check, of Gemini kon niet alles lezen: eerdere vondsten bewaren
            urls = {o["url"] for o in offers}
            offers += [o for o in previous if o["url"] not in urls]
    else:
        offers = previous
        if info.get("link_offer"):
            extra = finalize([info["link_offer"]])
            for o in extra:
                o["seen"] = now_iso()[:10]
            offers = [o for o in offers if o["url"] != info["link_offer"]["url"]] + extra
    offers.sort(key=lambda x: x["total"])
    kept = {o["url"] for o in offers}
    info["search_log"] = [x for x in SEARCH_LOG if x["url"] not in kept][:30] if do_web else prev.get("search_log", [])
    return info, offers


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
            info, offers = check_album(entry, prev, aid in web_ids)
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
            "artist": info.get("artist"),
            "title": info.get("title"),
            "offers": offers[:30],
            "variants": group_variants(offers),
            "pressings": info.get("pressings") or prev.get("pressings") or [],
            "cover": info.get("cover") or prev.get("cover"),
            "label": info.get("label") or prev.get("label"),
            "barcodes": info.get("barcodes") or prev.get("barcodes") or [],
            "discogs_url": info.get("discogs_url") or prev.get("discogs_url"),
            "discogs_nl_url": discogs_nl_link(info.get("discogs_url") or prev.get("discogs_url")),
            "search_log": info.get("search_log", []),
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
    header = (f"<img src='{base}mail-header.jpg' width='600' alt='Platenjacht' style='display:block;width:100%;max-width:600px;height:auto;border-radius:18px 18px 0 0'>"
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


# ---------- veilig samenvoegen bij opslaan ----------

def merge_into_current(ours_path):
    """Voegt het resultaat van deze ronde samen met de nieuwste prices.json op GitHub.
    Per plaat wint het meest recent gecheckte resultaat; verwijderde platen vallen weg."""
    empty = {"updated": None, "albums": {}}
    ours = load_json(pathlib.Path(ours_path), empty)
    theirs = load_json(PRICES_FILE, empty)
    ids = {a["id"] for a in load_json(ALBUMS_FILE, []) if a.get("id")}
    merged = dict(theirs.get("albums") or {})
    for aid, rec in (ours.get("albums") or {}).items():
        other = merged.get(aid)
        if other is None or (rec.get("checked") or "") >= (other.get("checked") or ""):
            merged[aid] = rec
    merged = {k: v for k, v in merged.items() if k in ids}
    u1, u2 = ours.get("usage") or {}, theirs.get("usage") or {}
    if u1.get("month") == u2.get("month"):
        usage = {**u2, **u1, "tavily_credits": max(u1.get("tavily_credits", 0), u2.get("tavily_credits", 0))}
    else:
        usage = u1 if (u1.get("month") or "") > (u2.get("month") or "") else u2
    out = {"updated": max(ours.get("updated") or "", theirs.get("updated") or "") or None,
           "usage": usage, "albums": merged}
    PRICES_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Samengevoegd: {len(merged)} platen.")


CONFIG = load_json(CONFIG_FILE, {})

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--merge":
        merge_into_current(sys.argv[2])
    else:
        main()
