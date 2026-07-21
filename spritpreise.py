#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Spritpreis-Tracker Diesel -- Magdeburg-Buckau
=============================================
Holt die aktuellen Dieselpreise der Tankstellen im Umkreis (Tankerkoenig-API,
gespeist aus der Markttransparenzstelle fuer Kraftstoffe), schreibt jede
Preisaenderung in eine CSV-Historie und baut ein mobiles HTML-Dashboard.

Ablauf pro Lauf:
  1. Tankstellen im Umkreis bestimmen  -> data/stations.json
     (nur beim ersten Lauf bzw. wenn Radius/Standort geaendert wurden oder die
     Liste aelter als CONFIG["station_refresh_days"] ist -- list.php ist teuer)
  2. Preise abrufen                    -> prices.php, in 10er-Bloecken
  3. Nur echte Aenderungen anhaengen   -> data/prices/YYYY-MM.csv
     (Spritpreise sind eine Treppenfunktion; unveraenderte Werte brauchen keine
     Zeile. Haelt das Repo klein, die Historie bleibt trotzdem exakt.)
  4. Aktuellen Stand merken            -> data/latest.json
  5. Dashboard bauen                   -> public/index.html

Zeitzonen: gespeichert wird immer UTC, angezeigt wird Europe/Berlin. Sonst
zerreisst die Sommerzeitumstellung spaeter die Tageskurve.

API-Key: kommt aus der Umgebungsvariablen TANKERKOENIG_API_KEY (im Workflow aus
dem GitHub-Secret). Niemals in den Code oder ins Repo schreiben.

Nutzungsbedingungen: nicht-kommerziell, hoechstens ein Abruf alle 5 Minuten.
Der Workflow laeuft alle 20 Minuten -- Faktor 4 Sicherheitsabstand.

Aufruf:
    python spritpreise.py                     # schreibt ./public/index.html
    python spritpreise.py --out ./dist        # eigenes Zielverzeichnis
    python spritpreise.py --refresh-stations  # Umkreissuche erzwingen
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# ---------------------------------------------------------------------------
# KONFIGURATION  --  hier gefahrlos nachjustieren
# ---------------------------------------------------------------------------
CONFIG = {
    # Fester Standort: Bleckenburgstrasse, Magdeburg-Buckau (39104).
    "location": {
        "name": "Magdeburg-Buckau",
        "street": "Bleckenburgstraße",
        "lat": 52.1114,
        "lon": 11.6370,
    },
    "timezone": "Europe/Berlin",

    # Umkreissuche. Die API deckelt den Radius bei 25 km.
    "radius_km": 10.0,
    # Nur die N naechstgelegenen Tankstellen verfolgen (haelt Abruf + Seite schlank).
    # In der Stadt ist dies die bindende Grenze, nicht radius_km: bei 12 lagen
    # alle Treffer binnen 3,5 km und die Markenketten (3x Aral, gleicher Preis)
    # dominierten das Bild. 20 reicht bis ~5-6 km und holt die freien
    # Tankstellen mit rein. Kostet einen zweiten prices.php-Block.
    "max_stations": 20,
    # Umkreissuche nur alle N Tage wiederholen (neue/geschlossene Tankstellen).
    "station_refresh_days": 30,

    # Kraftstoff. Dieses Tool ist auf Diesel ausgelegt.
    "fuel": "diesel",

    # --- Anzeige -----------------------------------------------------------
    # Farbschwelle: Aufpreis gegenueber der guenstigsten Tankstelle, in Cent.
    "diff_good_ct": 2.0,   # bis hier: gruen
    "diff_warn_ct": 5.0,   # bis hier: gelb, darueber: rot

    # --- Fuer Schritt 2 bereits vorgesehen ---------------------------------
    "tank_liter": 50.0,      # Tankgroesse fuer die Euro-Ersparnis
    # Hinweis, wenn Diesel darunter faellt (EUR/l). Muss zum Marktniveau passen:
    # am 21.07.2026 lag der guenstigste Preis in Buckau bei 2,308 -- eine
    # Schwelle deutlich darunter loest nie aus. Faustregel: knapp unter das,
    # was du an einem guten Tag siehst.
    "alarm_schwelle": 2.25,
}

APP_NAME = "Diesel-Tracker"

API_BASE = "https://creativecommons.tankerkoenig.de/json"
API_KEY_ENV = "TANKERKOENIG_API_KEY"
PRICES_CHUNK = 10        # prices.php erlaubt max. 10 IDs pro Request
CHUNK_PAUSE_S = 1.0      # kurze Pause zwischen den Bloecken, hoeflich bleiben
HTTP_TIMEOUT_S = 20.0

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIONS_FILE = DATA_DIR / "stations.json"
LATEST_FILE = DATA_DIR / "latest.json"
PRICES_DIR = DATA_DIR / "prices"
CSV_HEADER = ["timestamp_utc", "station_id", "status", "diesel"]


# ---------------------------------------------------------------------------
# HILFSFUNKTIONEN
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def api_key() -> str:
    key = (os.environ.get(API_KEY_ENV) or "").strip()
    if not key:
        raise RuntimeError(
            f"Umgebungsvariable {API_KEY_ENV} ist leer. Key unter "
            "https://creativecommons.tankerkoenig.de beantragen und als "
            "GitHub-Secret hinterlegen."
        )
    return key


def api_get(client: httpx.Client, endpoint: str, params: dict) -> dict:
    """Ruft die API auf und prueft das ok-Flag. Der Key steht nur in params,
    damit er nicht versehentlich in einer geloggten URL landet."""
    params = dict(params, apikey=api_key())
    r = client.get(f"{API_BASE}/{endpoint}", params=params, timeout=HTTP_TIMEOUT_S)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok", False):
        msg = data.get("message") or data.get("status") or "unbekannter Fehler"
        raise RuntimeError(f"Tankerkoenig ({endpoint}): {msg}")
    return data


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warnung: {path.name} nicht lesbar ({e}) -- wird neu aufgebaut.",
              file=sys.stderr)
        return None


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. TANKSTELLEN IM UMKREIS  (list.php -- selten)
# ---------------------------------------------------------------------------
def current_query(cfg: dict) -> dict:
    loc = cfg["location"]
    return {
        "lat": loc["lat"],
        "lon": loc["lon"],
        "radius_km": cfg["radius_km"],
        "max_stations": cfg["max_stations"],
        "fuel": cfg["fuel"],
    }


def stations_need_refresh(stored: dict | None, cfg: dict) -> bool:
    if not stored or not stored.get("stations"):
        return True
    if stored.get("query") != current_query(cfg):
        print("Standort/Radius geaendert -- Umkreissuche wird erneuert.")
        return True
    fetched = parse_iso_utc(stored.get("fetched_utc", ""))
    if fetched is None:
        return True
    age_days = (utc_now() - fetched).total_seconds() / 86400
    if age_days > cfg["station_refresh_days"]:
        print(f"Tankstellenliste ist {age_days:.0f} Tage alt -- wird erneuert.")
        return True
    return False


def fetch_stations(client: httpx.Client, cfg: dict) -> dict:
    """Umkreissuche. Liefert Stammdaten inkl. Entfernung, sortiert nach Naehe."""
    loc = cfg["location"]
    data = api_get(client, "list.php", {
        "lat": loc["lat"],
        "lng": loc["lon"],
        "rad": min(cfg["radius_km"], 25.0),   # API-Limit
        "sort": "dist",
        "type": cfg["fuel"],
    })
    stations = []
    for s in data.get("stations", [])[: cfg["max_stations"]]:
        stations.append({
            "id": s["id"],
            "name": (s.get("name") or "").strip(),
            "brand": (s.get("brand") or "").strip(),
            "street": (s.get("street") or "").strip(),
            "house_number": (s.get("houseNumber") or "").strip(),
            "post_code": s.get("postCode"),
            "place": (s.get("place") or "").strip(),
            "lat": s.get("lat"),
            "lng": s.get("lng"),
            "dist_km": s.get("dist"),
        })
    if not stations:
        raise RuntimeError(
            f"Keine Tankstelle mit Diesel im Umkreis von {cfg['radius_km']} km "
            "gefunden -- radius_km in CONFIG erhoehen."
        )
    print(f"Umkreissuche: {len(stations)} Tankstellen im Radius "
          f"{cfg['radius_km']} km.")
    return {
        "query": current_query(cfg),
        "fetched_utc": iso_utc(utc_now()),
        "stations": stations,
    }


def get_stations(client: httpx.Client, cfg: dict, force: bool) -> dict:
    stored = load_json(STATIONS_FILE)
    if force or stations_need_refresh(stored, cfg):
        stored = fetch_stations(client, cfg)
        save_json(STATIONS_FILE, stored)
    return stored


# ---------------------------------------------------------------------------
# 2. PREISE ABRUFEN  (prices.php -- jeder Lauf)
# ---------------------------------------------------------------------------
def fetch_prices(client: httpx.Client, station_ids: list[str]) -> dict:
    """Fragt die Preise in 10er-Bloecken ab (API-Limit) und fuehrt sie zusammen."""
    result: dict[str, dict] = {}
    chunks = [station_ids[i:i + PRICES_CHUNK]
              for i in range(0, len(station_ids), PRICES_CHUNK)]
    for n, chunk in enumerate(chunks):
        if n:
            time.sleep(CHUNK_PAUSE_S)
        data = api_get(client, "prices.php", {"ids": ",".join(chunk)})
        result.update(data.get("prices", {}))
    return result


def normalise(raw: dict) -> dict:
    """Macht aus der API-Antwort einen sauberen Zustand je Tankstelle.
    status: "open" (mit Preis) | "closed" | "unknown" (offen, aber kein Preis)."""
    status = raw.get("status")
    if status == "open":
        price = raw.get("diesel")
        if isinstance(price, (int, float)) and price > 0:
            return {"status": "open", "diesel": round(float(price), 3)}
        return {"status": "unknown", "diesel": None}
    if status == "closed":
        return {"status": "closed", "diesel": None}
    return {"status": "unknown", "diesel": None}


# ---------------------------------------------------------------------------
# 3. HISTORIE FORTSCHREIBEN  (nur echte Aenderungen)
# ---------------------------------------------------------------------------
def append_changes(prices: dict, previous: dict, now: datetime) -> tuple[dict, int]:
    """Vergleicht mit dem letzten Stand und haengt nur Aenderungen an die
    Monats-CSV an. Gibt den neuen Stand und die Zahl der Aenderungen zurueck."""
    ts = iso_utc(now)
    prev_stations = (previous or {}).get("stations", {})
    new_state: dict[str, dict] = {}
    rows: list[list] = []

    for sid, raw in prices.items():
        cur = normalise(raw)
        old = prev_stations.get(sid) or {}
        changed = (old.get("status") != cur["status"]
                   or old.get("diesel") != cur["diesel"])
        if changed:
            rows.append([ts, sid, cur["status"],
                         f"{cur['diesel']:.3f}" if cur["diesel"] is not None else ""])
        new_state[sid] = {
            "status": cur["status"],
            "diesel": cur["diesel"],
            # Seit wann gilt dieser Preis? Bei Aenderung jetzt, sonst uebernehmen.
            "since_utc": ts if changed else (old.get("since_utc") or ts),
            "checked_utc": ts,
        }

    if rows:
        PRICES_DIR.mkdir(parents=True, exist_ok=True)
        csv_path = PRICES_DIR / f"{now.astimezone(timezone.utc):%Y-%m}.csv"
        is_new = not csv_path.exists()
        with csv_path.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(CSV_HEADER)
            w.writerows(rows)
        print(f"{len(rows)} Aenderung(en) -> {csv_path.name}")
    else:
        print("Keine Preisaenderung seit dem letzten Lauf.")

    return {"checked_utc": ts, "stations": new_state}, len(rows)


def count_history_rows() -> int:
    """Zaehlt die gesammelten Datenpunkte (ueber alle Monate, ohne Kopfzeilen)."""
    total = 0
    if not PRICES_DIR.exists():
        return 0
    for p in PRICES_DIR.glob("*.csv"):
        try:
            with p.open(encoding="utf-8") as f:
                total += max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------
# 4. DASHBOARD BAUEN
# ---------------------------------------------------------------------------
CSS = """
*{box-sizing:border-box;margin:0;padding:0;}
:root{
  --paper:#15120F; --card:#1E1A16; --ink:#F2EDE5; --muted:#A79E90;
  --faint:#6E665B; --line:#2C2823; --chip:#26221D;
  /* Status-Palette (Label + Position sichern die Bedeutung, nie Farbe allein) */
  --good:#0ca30c; --good-ink:#7ed99a;
  --warn:#fab219; --warn-ink:#f7c96b;
  --bad:#d03b3b;  --bad-ink:#eb8f8f;
  --serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,"Times New Roman",serif;
  --sans:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}
html{-webkit-text-size-adjust:100%;}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
  line-height:1.5;padding:20px 16px 48px;max-width:720px;margin:0 auto;
  -webkit-font-smoothing:antialiased;}

/* Kopf */
.masthead{border-bottom:2px solid var(--ink);padding-bottom:18px;margin-bottom:22px;}
.kicker{font-size:12px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--warn-ink);font-weight:600;margin-bottom:8px;}
.title{font-family:var(--serif);font-weight:600;font-size:clamp(34px,9vw,52px);
  line-height:1.02;letter-spacing:-.01em;}
.sub{color:var(--muted);font-size:14px;margin-top:8px;}

/* Stoerungshinweis */
.alert{display:flex;gap:11px;align-items:flex-start;background:var(--chip);
  border:1px solid var(--line);border-left:4px solid var(--bad);
  border-radius:12px;padding:12px 14px;margin-bottom:20px;}
.alert__ic{font-size:20px;flex:none;line-height:1.2;}
.alert__t{font-weight:600;font-size:15px;}
.alert__d{color:var(--muted);font-size:13px;margin-top:2px;}

/* Sieger-Banner */
.hero{background:var(--chip);border:1px solid var(--line);
  border-left:5px solid var(--good);border-radius:16px;
  padding:16px 18px;margin-bottom:22px;}
.hero__t{font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint);}
.hero__row{display:flex;align-items:baseline;justify-content:space-between;
  gap:14px;flex-wrap:wrap;margin-top:6px;}
.hero__name{font-family:var(--serif);font-size:clamp(22px,5.5vw,28px);
  font-weight:600;line-height:1.15;}
.hero__meta{color:var(--muted);font-size:13.5px;margin-top:3px;}
.hero__note{color:var(--good-ink);font-size:13.5px;margin-top:10px;
  padding-top:10px;border-top:1px solid var(--line);}

/* Preisdarstellung */
.price{font-family:var(--serif);font-weight:600;font-variant-numeric:tabular-nums;
  letter-spacing:-.01em;white-space:nowrap;}
.price sup{font-size:.58em;vertical-align:super;margin-left:.5px;}
.price .eur{font-size:.5em;color:var(--muted);margin-left:3px;font-family:var(--sans);
  font-weight:400;letter-spacing:0;}
.hero .price{font-size:clamp(38px,10vw,50px);}
.card .price{font-size:26px;}

/* Legende */
.legend{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px;}
.lg{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;
  color:var(--muted);background:var(--chip);border:1px solid var(--line);
  border-radius:999px;padding:5px 11px 5px 9px;}
.dot{width:10px;height:10px;border-radius:50%;flex:none;}

/* Abschnitte */
.sec-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
  border-bottom:1px solid var(--line);padding-bottom:8px;margin-bottom:14px;}
.sec-label{font-family:var(--serif);font-size:clamp(21px,5vw,26px);font-weight:600;}
.sec-note{color:var(--muted);font-size:13.5px;}

/* Tankstellen-Karten */
.cards{display:flex;flex-direction:column;gap:10px;margin-bottom:30px;}
.card{background:var(--card);border:1px solid var(--line);
  border-left:5px solid var(--c,var(--line));border-radius:14px;padding:13px 15px;
  background:color-mix(in srgb, var(--c,transparent) 9%, var(--card));
  display:flex;align-items:center;justify-content:space-between;gap:14px;}
.card.good{--c:var(--good);} .card.warn{--c:var(--warn);} .card.bad{--c:var(--bad);}
.card.closed{--c:var(--line);opacity:.62;}
.card__l{min-width:0;}
.card__name{font-weight:600;font-size:16px;line-height:1.25;
  overflow-wrap:anywhere;}
.card__meta{color:var(--muted);font-size:13px;margin-top:2px;}
.card__r{text-align:right;flex:none;}
.card__diff{font-size:13px;color:var(--muted);margin-top:1px;
  font-variant-numeric:tabular-nums;}
.card__diff.good{color:var(--good-ink);}
.card__diff.warn{color:var(--warn-ink);}
.card__diff.bad{color:var(--bad-ink);}
.card__zu{font-size:13.5px;color:var(--faint);font-style:italic;}

/* Fusszeile */
.foot{border-top:1px solid var(--line);padding-top:16px;margin-top:8px;
  color:var(--faint);font-size:12.5px;line-height:1.65;}
.foot a{color:var(--muted);}
.foot strong{color:var(--muted);font-weight:600;}
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="112" fill="#221C17"/>
<path d="M256 96c58 74 92 122 92 158a92 92 0 1 1-184 0c0-36 34-84 92-158z"
      fill="#fab219"/>
<path d="M212 244l30 30 26-26 32 32" fill="none" stroke="#14110D"
      stroke-width="22" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M300 280v-30h-30" fill="none" stroke="#14110D" stroke-width="22"
      stroke-linecap="round" stroke-linejoin="round"/>
</svg>
"""

MANIFEST = {
    "name": "Diesel-Tracker Magdeburg",
    "short_name": APP_NAME,
    "start_url": "./",
    "display": "standalone",
    "background_color": "#15120F",
    "theme_color": "#15120F",
    "icons": [
        {"src": "icon.svg", "type": "image/svg+xml", "sizes": "any", "purpose": "any"},
        {"src": "icon-192.png", "type": "image/png", "sizes": "192x192",
         "purpose": "any maskable"},
        {"src": "icon-512.png", "type": "image/png", "sizes": "512x512",
         "purpose": "any maskable"},
    ],
}

ICON_HEAD = (
    '<link rel="icon" type="image/svg+xml" href="icon.svg">\n'
    '<link rel="apple-touch-icon" href="icon-180.png">\n'
    '<link rel="manifest" href="manifest.webmanifest">\n'
    '<meta name="theme-color" content="#15120F">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n'
    '<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
    f'<meta name="apple-mobile-web-app-title" content="{APP_NAME}">'
)


def _drop_png(size: int):
    """Zeichnet das App-Icon (Spritzer mit fallendem Preis) als PNG.
    Motiv bleibt in der zentralen ~70%-Zone -> auch als 'maskable' sicher."""
    from PIL import Image, ImageDraw
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    amber, dark = (250, 178, 25, 255), (20, 17, 13, 255)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22),
                        fill=(34, 28, 23, 255))
    cx = s / 2
    # Tropfen = Kreis unten + Dreieck als Spitze nach oben
    r = s * 0.20
    cyc = s * 0.60
    d.ellipse([cx - r, cyc - r, cx + r, cyc + r], fill=amber)
    d.polygon([(cx, s * 0.19), (cx - r * 0.94, cyc - r * 0.18),
               (cx + r * 0.94, cyc - r * 0.18)], fill=amber)
    # Fallende Zickzack-Linie im Tropfen
    w = max(2, int(s * 0.045))
    pts = [(cx - r * 0.62, cyc - r * 0.30), (cx - r * 0.18, cyc + r * 0.12),
           (cx + r * 0.16, cyc - r * 0.20), (cx + r * 0.62, cyc + r * 0.34)]
    d.line(pts, fill=dark, width=w, joint="curve")
    return img


def write_icons(out_dir: Path) -> None:
    """Icons + Manifest -- wird immer geschrieben, auch bei Abrufproblemen."""
    (out_dir / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    try:
        for size, name in ((180, "icon-180.png"), (192, "icon-192.png"),
                           (512, "icon-512.png")):
            _drop_png(size).save(out_dir / name)
    except ImportError:
        print("Hinweis: Pillow fehlt -- PNG-Icons uebersprungen (SVG greift).",
              file=sys.stderr)
    (out_dir / "manifest.webmanifest").write_text(
        json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_price(p: float) -> str:
    """1.679 -> 1,67<sup>9</sup> -- die uebliche Tankstellen-Schreibweise."""
    txt = f"{p:.3f}".replace(".", ",")
    return f'<span class="price">{txt[:-1]}<sup>{txt[-1]}</sup>'\
           f'<span class="eur">EUR</span></span>'


def fmt_ct(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def fmt_eur(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def fmt_km(v) -> str:
    try:
        return f"{float(v):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return "?"


def station_label(s: dict) -> str:
    """Marke bevorzugen, sonst Name -- die API liefert oft beides redundant."""
    brand, name = s.get("brand", ""), s.get("name", "")
    if brand and brand.lower() not in name.lower():
        return f"{brand} {name}".strip()
    return name or brand or "Tankstelle"


def address_line(s: dict) -> str:
    street = " ".join(x for x in (s.get("street"), s.get("house_number")) if x)
    place = s.get("place") or ""
    return ", ".join(x for x in (street, place) if x)


def build_html(stations: list[dict], state: dict, cfg: dict, now: datetime,
               history_rows: int, error: str | None = None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    loc = cfg["location"]
    by_id = {s["id"]: s for s in stations}

    # Offene Tankstellen mit Preis, sortiert nach Preis; Rest ans Ende.
    rows = []
    for sid, st in (state.get("stations") or {}).items():
        s = by_id.get(sid)
        if s:
            rows.append((s, st))
    open_rows = sorted([r for r in rows if r[1].get("diesel") is not None],
                       key=lambda r: r[1]["diesel"])
    other_rows = sorted([r for r in rows if r[1].get("diesel") is None],
                        key=lambda r: r[0].get("dist_km") or 99)

    checked = parse_iso_utc(state.get("checked_utc", "")) or now
    stand = checked.astimezone(tz).strftime("%d.%m.%Y, %H:%M Uhr")

    # Was die Seite wirklich abdeckt. In der Stadt greift meist max_stations
    # lange vor radius_km -- dann waere "Umkreis 10 km" schlicht gelogen.
    dists = [s["dist_km"] for s in stations if s.get("dist_km") is not None]
    if dists and max(dists) < cfg["radius_km"] * 0.95:
        coverage = (f"Die {len(dists)} n&auml;chsten Tankstellen "
                    f"(bis {fmt_km(max(dists))} km)")
    else:
        coverage = f"Umkreis {cfg['radius_km']:.0f} km"

    # --- Stoerungshinweis ---
    alert_html = ""
    if error:
        alert_html = (
            '<div class="alert"><div class="alert__ic">&#9888;&#65039;</div><div>'
            '<div class="alert__t">Abruf fehlgeschlagen</div>'
            f'<div class="alert__d">Angezeigt wird der letzte bekannte Stand. '
            f'{html.escape(error)}</div></div></div>'
        )

    # --- Sieger-Banner ---
    hero_html = ""
    if open_rows:
        s, st = open_rows[0]
        best = st["diesel"]
        note = ""
        if len(open_rows) > 1:
            worst = open_rows[-1][1]["diesel"]
            spread_ct = (worst - best) * 100
            spar = (worst - best) * cfg["tank_liter"]
            note = (f'<div class="hero__note">{fmt_ct(spread_ct)} ct/l Abstand zur '
                    f'teuersten &middot; {fmt_eur(spar)} EUR Unterschied auf eine '
                    f'{cfg["tank_liter"]:.0f}-l-Tankf&uuml;llung</div>')
        since = parse_iso_utc(st.get("since_utc", ""))
        since_txt = (f' &middot; seit {since.astimezone(tz):%H:%M} Uhr'
                     if since else "")
        hero_html = (
            '<div class="hero"><div class="hero__t">G&uuml;nstigster Diesel</div>'
            '<div class="hero__row"><div>'
            f'<div class="hero__name">{html.escape(station_label(s))}</div>'
            f'<div class="hero__meta">{html.escape(address_line(s))} &middot; '
            f'{fmt_km(s.get("dist_km"))} km{since_txt}</div>'
            f'</div>{fmt_price(best)}</div>{note}</div>'
        )

    # --- Kartenliste ---
    cards = []
    best_price = open_rows[0][1]["diesel"] if open_rows else None
    for s, st in open_rows:
        diff_ct = (st["diesel"] - best_price) * 100
        if diff_ct <= 0.001:
            cls, diff_txt = "good", "g&uuml;nstigster Preis"
        elif diff_ct <= cfg["diff_good_ct"]:
            cls, diff_txt = "good", f"+{fmt_ct(diff_ct)} ct"
        elif diff_ct <= cfg["diff_warn_ct"]:
            cls, diff_txt = "warn", f"+{fmt_ct(diff_ct)} ct"
        else:
            cls, diff_txt = "bad", f"+{fmt_ct(diff_ct)} ct"
        cards.append(
            f'<div class="card {cls}"><div class="card__l">'
            f'<div class="card__name">{html.escape(station_label(s))}</div>'
            f'<div class="card__meta">{html.escape(address_line(s))} &middot; '
            f'{fmt_km(s.get("dist_km"))} km</div></div>'
            f'<div class="card__r">{fmt_price(st["diesel"])}'
            f'<div class="card__diff {cls}">{diff_txt}</div></div></div>'
        )
    for s, st in other_rows:
        zu = "geschlossen" if st.get("status") == "closed" else "kein Preis gemeldet"
        cards.append(
            f'<div class="card closed"><div class="card__l">'
            f'<div class="card__name">{html.escape(station_label(s))}</div>'
            f'<div class="card__meta">{html.escape(address_line(s))} &middot; '
            f'{fmt_km(s.get("dist_km"))} km</div></div>'
            f'<div class="card__r"><span class="card__zu">{zu}</span></div></div>'
        )

    sec_note = (f"{len(open_rows)} offen"
                + (f", {len(other_rows)} ohne Preis" if other_rows else ""))

    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="dark">
<title>{APP_NAME} {loc['name']}</title>
{ICON_HEAD}
<style>{CSS}</style>
</head>
<body>
  <header class="masthead">
    <div class="kicker">&#9981; Diesel-Tracker</div>
    <h1 class="title">{html.escape(loc['name'])}</h1>
    <p class="sub">{coverage} um die
      {html.escape(loc['street'])} &middot; Stand {stand}</p>
  </header>

  {alert_html}
  {hero_html}

  <div class="legend">
    <span class="lg"><span class="dot" style="background:var(--good)"></span>
      bis +{fmt_ct(cfg['diff_good_ct'])} ct</span>
    <span class="lg"><span class="dot" style="background:var(--warn)"></span>
      bis +{fmt_ct(cfg['diff_warn_ct'])} ct</span>
    <span class="lg"><span class="dot" style="background:var(--bad)"></span>
      teurer</span>
  </div>

  <div class="sec-head">
    <div class="sec-label">Alle Tankstellen</div>
    <div class="sec-note">{sec_note}</div>
  </div>
  <div class="cards">
    {"".join(cards)}
  </div>

  <div class="foot">
    <strong>{history_rows}</strong> Preis&auml;nderungen aufgezeichnet &middot;
    Auswertung (Tageskurve, Wochentagsmuster, Preisalarm) folgt in Schritt&nbsp;2.<br>
    Preisdaten von
    <a href="https://creativecommons.tankerkoenig.de">Tankerk&ouml;nig</a>
    (Markttransparenzstelle f&uuml;r Kraftstoffe), Lizenz
    <a href="https://creativecommons.org/licenses/by/4.0/">CC&nbsp;BY&nbsp;4.0</a>.
    Alle Angaben ohne Gew&auml;hr &middot; nicht-kommerzielle Nutzung.
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HAUPTPROGRAMM
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Spritpreis-Tracker Diesel")
    ap.add_argument("--out", default="./public", help="Ausgabeverzeichnis")
    ap.add_argument("--refresh-stations", action="store_true",
                    help="Umkreissuche erzwingen (list.php)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_icons(out_dir)

    cfg = CONFIG
    now = utc_now()
    previous = load_json(LATEST_FILE) or {}
    error: str | None = None
    stations: list[dict] = []
    state = previous

    try:
        with httpx.Client(headers={"User-Agent": "Diesel-Tracker/1.0 (privat)"}) as c:
            store = get_stations(c, cfg, args.refresh_stations)
            stations = store["stations"]
            prices = fetch_prices(c, [s["id"] for s in stations])
            state, n_changes = append_changes(prices, previous, now)
            save_json(LATEST_FILE, state)
    except Exception as e:                      # Netz, API, Key, Kontingent ...
        error = f"{type(e).__name__}: {e}"
        print(f"FEHLER: {error}", file=sys.stderr)
        # Ohne Stammdaten aus dem laufenden Versuch: die gespeicherten nehmen,
        # damit die Seite den letzten bekannten Stand zeigen kann.
        if not stations:
            stations = (load_json(STATIONS_FILE) or {}).get("stations", [])

    html_out = build_html(stations, state, cfg, now, count_history_rows(), error)
    (out_dir / "index.html").write_text(html_out, encoding="utf-8")
    print(f"Dashboard geschrieben: {out_dir / 'index.html'}")

    # Bewusst Exit 0 auch bei Abrufproblemen: die Seite bleibt online und zeigt
    # den letzten Stand samt Hinweis. Der Fehler steht im Actions-Log.
    return 0


if __name__ == "__main__":
    sys.exit(main())
