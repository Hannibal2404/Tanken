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

    # --- Auswertung --------------------------------------------------------
    "tank_liter": 50.0,      # Tankgroesse fuer die Euro-Ersparnis
    # Hinweis, wenn Diesel darunter faellt (EUR/l). Muss zum Marktniveau passen:
    # am 21.07.2026 lag der guenstigste Preis in Buckau bei 2,308 -- eine
    # Schwelle deutlich darunter loest nie aus. Faustregel: knapp unter das,
    # was du an einem guten Tag siehst.
    "alarm_schwelle": 2.25,
    # Fenster der Tageskurve. Mehr Tage machen die Kurve auf dem Handy flacher;
    # gezeichnet wird ohnehin nur, soweit Historie da ist.
    "history_days": 4,
    # Aufloesung der rekonstruierten Kurve. Die Rohdaten sind eine Treppe, der
    # Abruf laeuft alle 30 min -- 15 min verliert nichts und glaettet nichts weg.
    "curve_step_min": 15,
    # Das Stundenprofil erst zeigen, wenn jede Stunde aus mindestens so vielen
    # verschiedenen Tagen stammt. Darunter ist es ein Zufallsbild, kein Muster.
    "hourly_min_days": 2,
    # ... und nur Tage zaehlen, die so viele Stunden abgedeckt sind. Ein
    # angebrochener Tag hat ein schiefes Tagesmittel (der erste Messtag begann
    # mittags, also im teuren Teil) und wuerde das ganze Profil kippen.
    "hourly_min_hours": 14,

    # --- Empfehlung & Ersparnis --------------------------------------------
    # Fuer die Hochrechnung der Jahresersparnis. Bewusst konservativ und immer
    # mit der Annahme daneben geschrieben, damit die Zahl ehrlich bleibt.
    "fuellungen_pro_monat": 2,
    # Die Zeit-Empfehlung raet nur zum Warten, wenn spaeter am Tag mindestens so
    # viel guenstiger zu erwarten ist (ct). Darunter lohnt der Aufwand nicht.
    "warten_min_ct": 1.5,
    # Client-seitiger Veraltungshinweis: ist der angezeigte Stand aelter als
    # dies (Minuten), blendet die Seite selbst eine Warnung ein. Faengt den
    # stillen Ausfall ab -- wenn der Workflow gar nicht mehr laeuft, kann der
    # Rebuild sich nicht selbst als veraltet melden, der Browser aber schon.
    "stale_after_min": 90,

    # --- Laengerfristiger Trend (Sparkline der Tagestiefstwerte) -----------
    # Zweites, groeberes Zeitfenster neben der 4-Tage-Detailkurve. Zeigt, ob
    # der Boden gerade steigt oder faellt -- das verdeckt die Detailkurve.
    "trend_days": 30,
    # Erst zeichnen, wenn so viele Tage vorliegen. Drei Punkte sind kein Trend.
    "trend_min_days": 5,

    # --- Seltenheits-Push (ntfy) -------------------------------------------
    # Push NUR bei einem echten Ausreisser nach unten, nicht taeglich. Kanal
    # kommt aus der Umgebung (NTFY_TOPIC, gesetzt als GitHub-Secret); fehlt er,
    # ist der Push komplett aus. Server ueberschreibbar per NTFY_SERVER.
    # Erst scharf, wenn so viele Tage Historie da sind -- sonst ist jedes neue
    # Tief ein "Rekord" und es feuert dauernd.
    "push_min_history_days": 14,
    # Ausloeser: aktueller Bestpreis liegt mindestens so viele ct unter dem
    # ueblichen Tagesboden (Median der Tagestiefstwerte im Fenster).
    "push_below_floor_ct": 2.0,
    # Nicht erneut pingen, solange der Preis nicht wieder um so viel ct
    # gestiegen ist -- verhindert Dauerfeuer, solange es guenstig bleibt.
    "push_reset_ct": 1.0,
    # Zusaetzliche Sperre in Stunden zwischen zwei Pushes.
    "push_cooldown_h": 8.0,
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
ALERTS_FILE = DATA_DIR / "alerts.json"   # Merker fuer den Seltenheits-Push
PRICES_DIR = DATA_DIR / "prices"
CSV_HEADER = ["timestamp_utc", "station_id", "status", "diesel"]

# Seite (fuer die Push-Nachricht). Aus dem Repo ableitbar, aber selten geaendert.
PAGE_URL = "https://hannibal2404.github.io/Tanken/"


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
# 3a. SELTENHEITS-PUSH  (nur bei echtem Preis-Ausreisser nach unten)
# ---------------------------------------------------------------------------
# Absicht: hoechstens ein paar Mal im Monat pingen, nicht taeglich. Ein naiver
# Schwellenalarm feuert taeglich (der Tagesboden liegt fast immer unter jeder
# runden Schwelle). Deshalb der Vergleich mit dem *ueblichen* Tagesboden plus
# eine Wiederscharf-Schaltung: nach einem Push erst wieder armen, wenn der
# Preis sich erholt hat. Der Merker liegt in data/alerts.json und aendert sich
# nur bei einem Push oder beim Wiederscharfschalten -- kein Commit-Rauschen.
def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def push_decision(current: float | None, lows: list[tuple], alert: dict,
                  cfg: dict, now: datetime) -> dict:
    """Reine Entscheidung, ob jetzt gepusht wird -- ohne Seiteneffekt, testbar.

    Gibt {push, reason, floor, state} zurueck. `state` wird immer gespeichert,
    damit Cooldown und Wiederscharf-Schaltung ueber Laeufe hinweg greifen."""
    # Merker uebernehmen (Vorbelegung: scharf, noch nie gepusht).
    armed = alert.get("armed", True)
    rearm_above = alert.get("rearm_above")
    last_push_utc = alert.get("last_push_utc")
    last_push_low = alert.get("last_push_low")

    def keep(reason: str, push: bool = False, floor=None) -> dict:
        return {"push": push, "reason": reason, "floor": floor,
                "state": {"armed": armed, "rearm_above": rearm_above,
                          "last_push_utc": last_push_utc,
                          "last_push_low": last_push_low}}

    if current is None:
        return keep("kein aktueller Preis")
    # Erst scharf, wenn genug Tage Historie da sind -- sonst ist jedes neue Tief
    # ein "Rekord".
    if len(lows) < cfg["push_min_history_days"]:
        return keep(f"zu wenig Historie ({len(lows)} < "
                    f"{cfg['push_min_history_days']} Tage)")

    # Ueblicher Tagesboden = Median der Tagestiefstwerte OHNE den heutigen Tag
    # (sonst zieht der aktuelle Tiefstwert seine eigene Vergleichsmarke runter).
    floor_vals = [v for _, v in lows[:-1]] or [v for _, v in lows]
    floor = _median(floor_vals)
    threshold = floor - cfg["push_below_floor_ct"] / 100.0

    # Nach einem Push erst wieder scharf, wenn der Preis sich erholt hat.
    if not armed and rearm_above is not None and current >= rearm_above - 1e-9:
        armed = True
        rearm_above = None

    triggered = current <= threshold + 1e-9
    cooldown_ok = True
    if last_push_utc:
        lp = parse_iso_utc(last_push_utc)
        if lp is not None:
            cooldown_ok = (now - lp).total_seconds() / 3600 >= cfg["push_cooldown_h"]

    if armed and triggered and cooldown_ok:
        armed = False
        rearm_above = current + cfg["push_reset_ct"] / 100.0
        last_push_utc = iso_utc(now)
        last_push_low = current
        return {"push": True,
                "reason": f"{(floor - current) * 100:.1f} ct unter dem Boden",
                "floor": floor,
                "state": {"armed": armed, "rearm_above": rearm_above,
                          "last_push_utc": last_push_utc,
                          "last_push_low": last_push_low}}

    if not triggered:
        why = "ueber der Ausloeseschwelle"
    elif not armed:
        why = "noch nicht wieder scharf (Preis nicht erholt)"
    else:
        why = "Cooldown laeuft"
    return keep(why, floor=floor)


def send_ntfy(topic: str, server: str, title: str, body: str, url: str) -> bool:
    """Schickt eine Nachricht an ntfy. Title-Header nur ASCII (Umlaute wuerden
    den Header zerlegen) -- die Details stehen im UTF-8-Body."""
    try:
        r = httpx.post(
            f"{server.rstrip('/')}/{topic}",
            content=body.encode("utf-8"),
            headers={"Title": title, "Tags": "fuelpump,chart_with_downwards_trend",
                     "Click": url, "Priority": "default"},
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()
        return True
    except Exception as e:                        # Netz, ntfy down, falsches Topic
        print(f"Push fehlgeschlagen: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def maybe_push(current: float | None, label: str, ana: dict | None,
               cfg: dict, now: datetime) -> None:
    """Orchestriert den Seltenheits-Push. Ohne NTFY_TOPIC komplett aus --
    dann wird auch keine alerts.json angelegt."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").strip()
    lows = (ana or {}).get("lows") or []

    alert = load_json(ALERTS_FILE) or {}
    dec = push_decision(current, lows, alert, cfg, now)
    save_json(ALERTS_FILE, dec["state"])          # Merker fortschreiben

    if not dec["push"]:
        print(f"Kein Push: {dec['reason']}.")
        return

    body = (f"Diesel {fmt_eur_l(current)} EUR/l bei {label} -- "
            f"{dec['reason']}, so guenstig ist selten. {PAGE_URL}")
    if send_ntfy(topic, server, "Diesel guenstig", body, PAGE_URL):
        print(f"Push gesendet: {dec['reason']} ({fmt_eur_l(current)} EUR/l).")


def cheapest_label(stations: list[dict], state: dict) -> str:
    """Name der aktuell guenstigsten offenen Tankstelle -- fuer die Nachricht."""
    by_id = {s["id"]: s for s in stations}
    best = None
    for sid, st in (state.get("stations") or {}).items():
        d = st.get("diesel")
        if d is None:
            continue
        if best is None or d < best[0]:
            best = (d, by_id.get(sid))
    return station_label(best[1]) if best and best[1] else "einer Tankstelle"


# ---------------------------------------------------------------------------
# 3b. HISTORIE AUSWERTEN
# ---------------------------------------------------------------------------
# Grundgedanke: in der CSV steht nur, WANN sich ein Preis geaendert hat. Der
# Preis dazwischen ist der letzte bekannte -- eine Treppenfunktion. Alles hier
# baut aus den Stufen wieder einen zeitlich gleichmaessigen Verlauf; ohne das
# wuerde jede Auswertung die Tankstellen ueberbewerten, die oft nachziehen.
def month_keys(start: datetime, end: datetime) -> list[str]:
    """Monats-Dateinamen (YYYY-MM), die das Fenster beruehrt."""
    keys, cur = [], start.astimezone(timezone.utc).replace(day=1)
    end = end.astimezone(timezone.utc)
    while cur <= end:
        keys.append(f"{cur:%Y-%m}")
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return keys


def read_history(start: datetime, end: datetime) -> list[tuple]:
    """Aenderungen als (zeit, station_id, status, preis|None), chronologisch.

    Gelesen wird ab einem Monat VOR dem Fenster: die Treppe braucht den letzten
    Preis von davor, sonst beginnt die Kurve mit Luecken."""
    events: list[tuple] = []
    for key in month_keys(start - timedelta(days=31), end):
        path = PRICES_DIR / f"{key}.csv"
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    ts = parse_iso_utc(row.get("timestamp_utc", ""))
                    if ts is None or ts > end:
                        continue
                    raw = (row.get("diesel") or "").strip()
                    try:
                        price = float(raw) if raw else None
                    except ValueError:
                        price = None
                    events.append((ts, row.get("station_id", ""),
                                   row.get("status", ""), price))
        except OSError as e:
            print(f"Warnung: {path.name} nicht lesbar ({e}).", file=sys.stderr)
    events.sort(key=lambda e: e[0])
    return events


def daily_lows(end: datetime, cfg: dict, tz: ZoneInfo) -> list[tuple]:
    """Tiefster Preis je Kalendertag ueber ein langes Fenster -> [(date, low)].

    Weil die CSV jede Preisstufe festhaelt, ist das Minimum aller gemeldeten
    offenen Preise eines Tages exakt der Tagesboden -- ohne Treppen-Rekonstruktion.
    Grundlage fuer Trend-Sparkline und Seltenheits-Push."""
    start = end - timedelta(days=cfg["trend_days"])
    start_d = start.astimezone(tz).date()
    lows: dict = {}
    for ts, _sid, status, price in read_history(start, end):
        if status != "open" or price is None:
            continue
        d = ts.astimezone(tz).date()
        if d < start_d:
            continue
        lows[d] = price if d not in lows else min(lows[d], price)
    return sorted(lows.items())


def build_curve(events: list[tuple], start: datetime, end: datetime,
                step_min: int) -> list[tuple]:
    """Guenstigster Marktpreis je Zeitschritt -- [(zeit, preis|None), ...].

    None heisst: zu dem Zeitpunkt war keine Tankstelle mit Preis bekannt
    (ganz am Anfang der Historie, oder nachts alles geschlossen)."""
    state: dict[str, float | None] = {}
    step = timedelta(minutes=step_min)
    pts: list[tuple] = []
    i = 0
    # Zustand bis zum Fensterstart vorspulen.
    while i < len(events) and events[i][0] <= start:
        _, sid, status, price = events[i]
        state[sid] = price if status == "open" else None
        i += 1
    t = start
    while t <= end:
        while i < len(events) and events[i][0] <= t:
            _, sid, status, price = events[i]
            state[sid] = price if status == "open" else None
            i += 1
        vals = [v for v in state.values() if v is not None]
        pts.append((t, min(vals) if vals else None))
        t += step
    return pts


def hourly_profile(pts: list[tuple], tz: ZoneInfo, cfg: dict) -> list[dict]:
    """Mittlere Abweichung des Bestpreises je Tagesstunde, in ct.

    Bezugsgroesse ist das Mittel des JEWEILIGEN Tages, nicht des Fensters --
    sonst misst man den Trend der Woche und nicht die Tageszeit. Angebrochene
    Tage fliegen raus: deren Mittel steht schief, weil ihnen genau die teure
    oder genau die billige Tageshaelfte fehlt."""
    min_days = cfg["hourly_min_days"]
    need = cfg["hourly_min_hours"] * 60 / max(1, cfg["curve_step_min"])
    by_day: dict = {}
    for t, v in pts:
        if v is not None:
            by_day.setdefault(t.astimezone(tz).date(), []).append(v)
    day_mean = {d: sum(vs) / len(vs) for d, vs in by_day.items() if len(vs) >= need}

    devs: dict[int, list] = {h: [] for h in range(24)}
    days: dict[int, set] = {h: set() for h in range(24)}
    for t, v in pts:
        if v is None:
            continue
        loc = t.astimezone(tz)
        base = day_mean.get(loc.date())
        if base is None:
            continue
        devs[loc.hour].append((v - base) * 100)
        days[loc.hour].add(loc.date())

    out = []
    for h in range(24):
        if len(days[h]) >= min_days and devs[h]:
            out.append({"hour": h, "dev_ct": sum(devs[h]) / len(devs[h]),
                        "days": len(days[h])})
    return out


def price_verdict(current: float | None, pts: list[tuple],
                  cfg: dict) -> dict | None:
    """Einordnung des aktuellen Bestpreises im beobachteten Fenster.

    Bewusst ein Perzentil und keine Prognose: mit wenigen Tagen Historie laesst
    sich sagen, ob der Preis gerade niedrig ist -- nicht, ob er morgen faellt."""
    vals = sorted(v for _, v in pts if v is not None)
    if current is None or len(vals) < 16:
        return None
    lo, hi = vals[0], vals[-1]
    ueber_min_eur = (current - lo) * cfg["tank_liter"]

    if hi - lo < 0.0005:
        return {"cls": "warn", "head": "Unver&auml;ndert",
                "detail": "Der g&uuml;nstigste Preis hat sich im beobachteten "
                          "Zeitraum nicht bewegt.",
                "pct": 0.0, "min": lo, "max": hi}

    # Getrennt zaehlen statt "100 minus": wer genau auf dem Tiefstand steht, war
    # nicht "guenstiger als 100 % der Zeit", sondern gleichauf.
    below = sum(1 for v in vals if v < current - 0.0005)
    above = sum(1 for v in vals if v > current + 0.0005)
    pct = 100.0 * below / len(vals)

    if pct <= 25:
        cls, head = "good", "Guter Zeitpunkt"
        detail = (f"G&uuml;nstiger als {100.0 * above / len(vals):.0f}&nbsp;% "
                  f"der beobachteten Zeit. Tanken.")
    elif pct <= 60:
        cls, head = "warn", "Mittelfeld"
        detail = (f"Teurer als {pct:.0f}&nbsp;% der beobachteten Zeit &middot; "
                  f"{fmt_eur(ueber_min_eur)}&nbsp;EUR &uuml;ber dem Tiefstand "
                  f"auf {cfg['tank_liter']:.0f}&nbsp;l.")
    else:
        cls, head = "bad", "Eher teuer"
        detail = (f"Teurer als {pct:.0f}&nbsp;% der beobachteten Zeit &middot; "
                  f"{fmt_eur(ueber_min_eur)}&nbsp;EUR &uuml;ber dem Tiefstand "
                  f"auf {cfg['tank_liter']:.0f}&nbsp;l. Wenn es sich aufschieben "
                  f"l&auml;sst: warten.")
    return {"cls": cls, "head": head, "detail": detail, "pct": pct,
            "min": lo, "max": hi}


def timing_hint(hours: list[dict], end: datetime, tz: ZoneInfo,
                cfg: dict) -> dict | None:
    """Aus dem Stundenprofil eine Handlung ableiten: jetzt oder spaeter?

    Bewusst nur, was die Tageskurve wirklich hergibt -- keine Prognose ins
    Blaue. Fehlt fuer die aktuelle oder die spaeteren Stunden die Datenlage,
    schweigt die Funktion lieber, als zu raten (gibt None zurueck)."""
    if not hours:
        return None
    dev = {h["hour"]: h["dev_ct"] for h in hours}
    cur = end.astimezone(tz).hour
    if cur not in dev:
        return None
    now_dev = dev[cur]

    # Gibt es spaeter heute eine Stunde, die spuerbar guenstiger zu erwarten ist?
    later = [(h, d) for h, d in dev.items()
             if h > cur and d <= now_dev - cfg["warten_min_ct"]]
    if later:
        best_h, best_d = min(later, key=lambda kv: kv[1])
        return {"wait": True, "hour": best_h, "gain_ct": now_dev - best_d}
    # Nichts Guenstigeres in Sicht -- nur dann ermutigen, wenn wir gerade auch
    # wirklich in der billigen Tageshaelfte sind (unter dem Tagesmittel).
    if now_dev <= 0.0:
        return {"wait": False}
    return None


def savings_stat(hours: list[dict], cfg: dict) -> dict | None:
    """Was die Tageszeit ueber alle Tankstellen hinweg ausmacht, in Euro.

    Grundlage ist die Spanne des Stundenprofils (billigste vs. teuerste
    Tagesstunde), nicht der einmalige Tagesausreisser -- das ist die Ersparnis,
    die sich zuverlaessig wiederholen laesst. Unter 3 ct Spanne bleibt der
    Hinweis weg, sonst wird aus Rauschen eine Aussage."""
    if not hours:
        return None
    best = min(h["dev_ct"] for h in hours)
    worst = max(h["dev_ct"] for h in hours)
    swing_ct = worst - best
    if swing_ct < 3.0:
        return None
    per_fill = swing_ct / 100.0 * cfg["tank_liter"]
    per_year = per_fill * cfg["fuellungen_pro_monat"] * 12
    return {"swing_ct": swing_ct, "per_fill": per_fill, "per_year": per_year}


def analyse_history(state: dict, cfg: dict, now: datetime) -> dict | None:
    """Alles, was die Auswertung anzeigt. Reine Dateiarbeit -- laeuft auch,
    wenn der Abruf gescheitert ist, dann eben ohne den letzten Punkt."""
    tz = ZoneInfo(cfg["timezone"])
    end = parse_iso_utc((state or {}).get("checked_utc", "")) or now
    events = read_history(now - timedelta(days=cfg["history_days"]), end)
    if not events:
        return None

    # Nicht weiter zurueck als die Historie reicht -- sonst haengt links ein
    # leerer Streifen an der Kurve.
    start = max(events[0][0], end - timedelta(days=cfg["history_days"]))
    pts = build_curve(events, start, end, cfg["curve_step_min"])
    known = [v for _, v in pts if v is not None]
    if len(known) < 8:
        return None

    prices_now = [st.get("diesel") for st in (state.get("stations") or {}).values()
                  if st.get("diesel") is not None]
    current = min(prices_now) if prices_now else None

    span_h = (end - start).total_seconds() / 3600
    hours = hourly_profile(pts, tz, cfg)
    return {
        "pts": pts,
        "start": start,
        "end": end,
        "days": len({t.astimezone(tz).date() for t, v in pts if v is not None}),
        "span_h": span_h,
        "min": min(known),
        "max": max(known),
        "hours": hours,
        "verdict": price_verdict(current, pts, cfg),
        "timing": timing_hint(hours, end, tz, cfg),
        "savings": savings_stat(hours, cfg),
        "lows": daily_lows(end, cfg, tz),
        "current": current,
    }


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
/* MUSS nach .alert stehen und braucht den Attribut-Selektor: ein eigenes
   display: in einer Author-Regel schlaegt die eingebaute Browser-Regel
   [hidden]{display:none}. Ohne das hier waere der Veraltungs-Kasten immer
   sichtbar -- nur eben ohne Text, weil das Skript nichts hineinschreibt. */
.alert[hidden]{display:none;}
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

/* Einordnung "jetzt tanken oder warten" */
.verdict{background:var(--chip);border:1px solid var(--line);
  border-left:5px solid var(--c,var(--line));border-radius:14px;
  padding:13px 15px;margin-bottom:22px;}
.verdict.good{--c:var(--good);} .verdict.warn{--c:var(--warn);}
.verdict.bad{--c:var(--bad);}
.verdict__t{font-weight:600;font-size:16px;}
.verdict__t.good{color:var(--good-ink);}
.verdict__t.warn{color:var(--warn-ink);}
.verdict__t.bad{color:var(--bad-ink);}
.verdict__d{color:var(--muted);font-size:13.5px;margin-top:3px;}
.verdict__hint{font-size:13.5px;margin-top:9px;padding-top:9px;
  border-top:1px solid var(--line);display:flex;gap:8px;align-items:flex-start;}
.verdict__hint b{color:var(--ink);font-weight:600;}
.verdict__ar{flex:none;color:var(--faint);}

/* Ersparnis-Kachel */
.savings{background:var(--chip);border:1px solid var(--line);border-radius:14px;
  padding:14px 16px;margin:0 0 26px;display:flex;gap:15px;align-items:center;}
.savings__big{font-family:var(--serif);font-weight:600;font-size:clamp(26px,7vw,34px);
  line-height:1;color:var(--good-ink);white-space:nowrap;flex:none;}
.savings__d{color:var(--muted);font-size:13px;}
.savings__d b{color:var(--ink);font-weight:600;}
/* Auf schmalen Schirmen stapeln -- sonst quetscht sich der Text in 2-Wort-
   Zeilen neben die grosse Zahl. */
@media (max-width:520px){
  .savings{flex-direction:column;align-items:flex-start;gap:7px;}
}

/* Diagramme.
   Die Zeichenflaeche ist bewusst handy-breit (360 Einheiten): ein SVG skaliert
   seine Schrift mit, eine 700er Flaeche auf 343 px Display macht die
   Achsenbeschriftung unleserlich. Deshalb oben gedeckelt statt aufgeblasen. */
.chart{margin-bottom:10px;}
.chart svg{width:100%;max-width:480px;height:auto;display:block;margin:0 auto;
  overflow:visible;}
.chart .ax{fill:var(--faint);font-size:10.5px;font-family:var(--sans);
  font-variant-numeric:tabular-nums;}
.chart .grid{stroke:var(--line);stroke-width:1;}
.chart .line{fill:none;stroke:var(--warn);stroke-width:2.5;
  stroke-linejoin:round;stroke-linecap:round;}
.chart .area{fill:var(--warn);opacity:.10;}
.chart .now{fill:var(--warn);}
.chart .bar.good{fill:var(--good);} .chart .bar.bad{fill:var(--bad);}
.chart .zero{stroke:var(--faint);stroke-width:1;}
.chart .spark{fill:none;stroke:var(--muted);stroke-width:2;
  stroke-linejoin:round;stroke-linecap:round;}
.chart .spark-dot{fill:var(--ink);}
.chart-note{color:var(--faint);font-size:12.5px;margin:0 0 26px;line-height:1.55;}

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


WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Client-seitiger Veraltungshinweis. Laeuft im Browser, nicht beim Bauen --
# das ist der Witz: wenn der Workflow ausfaellt, wird die Seite nicht mehr neu
# gebaut und kann sich selbst nicht als alt melden. Der Browser vergleicht den
# eingebackenen Stand mit seiner eigenen Uhr und blendet die Warnung ein.
# __CHECKED__ ist ein UTC-Zeitstempel (oder leer -> Skript tut nichts),
# __LIMIT__ die Schwelle in Minuten. Beide werden beim Bauen ersetzt.
STALE_JS = """<script>
(function(){
  var checked = "__CHECKED__", limit = __LIMIT__;
  if(!checked) return;
  var box = document.getElementById('stale'), txt = document.getElementById('stale-d');
  if(!box || !txt) return;
  function upd(){
    var age = (Date.now() - Date.parse(checked)) / 60000;
    if(age > limit){
      var h = Math.floor(age/60), m = Math.round(age%60);
      txt.textContent = 'Der angezeigte Stand ist ' + (h ? h + ' Std ' : '')
        + m + ' Min alt. Der automatische Abruf haengt moeglicherweise.';
      box.hidden = false;
    } else { box.hidden = true; }
  }
  upd(); setInterval(upd, 60000);
})();
</script>"""


def fmt_eur_l(v: float) -> str:
    return f"{v:.3f}".replace(".", ",")


def svg_curve(ana: dict, tz: ZoneInfo) -> str:
    """Guenstigster Diesel im Zeitverlauf, als Treppe gezeichnet.

    Bewusst keine geglaettete Linie: zwischen zwei Aenderungen gilt der alte
    Preis unveraendert weiter, eine Diagonale waere eine Erfindung."""
    W, H = 360, 130
    L, R, T, B = 38, 6, 10, 20
    pts = ana["pts"]
    lo, hi = ana["min"], ana["max"]
    pad = max(0.004, (hi - lo) * 0.15)
    y0, y1 = lo - pad, hi + pad
    t0 = ana["start"].timestamp()
    tspan = max(1.0, ana["end"].timestamp() - t0)

    def px(t: datetime) -> float:
        return L + (W - L - R) * (t.timestamp() - t0) / tspan

    def py(v: float) -> float:
        return T + (H - T - B) * (1 - (v - y0) / (y1 - y0))

    parts = []

    # Waagerechte Hilfslinien mit Preisbeschriftung.
    # Bei einem ueber das ganze Fenster konstanten Preis waeren die drei
    # Linien deckungsgleich -- dann reicht eine.
    levels = (hi,) if hi - lo < 0.0005 else (hi, (hi + lo) / 2, lo)
    for v in levels:
        y = py(v)
        parts.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" '
                     f'x2="{W - R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="ax" x="{L - 5}" y="{y + 3.5:.1f}" '
                     f'text-anchor="end">{fmt_eur_l(v)}</text>')

    # Senkrechte je Mitternacht (Ortszeit) -- Tagesgrenzen als Orientierung.
    day = ana["start"].astimezone(tz).replace(hour=0, minute=0, second=0,
                                              microsecond=0) + timedelta(days=1)
    while day <= ana["end"]:
        x = px(day)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{T}" '
                     f'x2="{x:.1f}" y2="{H - B}"/>')
        # Beschriftung nur, wenn sie nicht ueber den Rand haengt.
        if L + 28 <= x <= W - R - 28:
            parts.append(f'<text class="ax" x="{x:.1f}" y="{H - B + 13}" '
                         f'text-anchor="middle">{WEEKDAYS[day.weekday()]} '
                         f'{day:%d.%m.}</text>')
        day += timedelta(days=1)

    # Die Treppe selbst, an Luecken (keine Tankstelle bekannt) unterbrochen.
    for seg in _segments(pts):
        d = []
        for n, (t, v) in enumerate(seg):
            x, y = px(t), py(v)
            if n == 0:
                d.append(f"M{x:.1f} {y:.1f}")
            else:
                d.append(f"H{x:.1f} V{y:.1f}")
        if len(seg) > 1:
            x_end, x_start = px(seg[-1][0]), px(seg[0][0])
            parts.append(f'<path class="area" d="{" ".join(d)} '
                         f'V{H - B} H{x_start:.1f} Z"/>')
        parts.append(f'<path class="line" d="{" ".join(d)}"/>')

    # Aktueller Stand als Punkt am rechten Ende.
    last = next(((t, v) for t, v in reversed(pts) if v is not None), None)
    if last:
        parts.append(f'<circle class="now" cx="{px(last[0]):.1f}" '
                     f'cy="{py(last[1]):.1f}" r="3"/>')

    return (f'<div class="chart"><svg viewBox="0 0 {W} {H}" '
            f'role="img" aria-label="Verlauf des g&uuml;nstigsten Dieselpreises">'
            f'{"".join(parts)}</svg></div>')


def _segments(pts: list[tuple]) -> list[list[tuple]]:
    """Zerlegt die Punktfolge an den Luecken in zeichenbare Stuecke."""
    segs, cur = [], []
    for t, v in pts:
        if v is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append((t, v))
    if len(cur) > 1:
        segs.append(cur)
    return segs


def svg_hours(hours: list[dict]) -> str:
    """Tagesstunden als Abweichung vom Tagesmittel, in ct.

    Nach unten = guenstiger als der Tagesschnitt. Die Richtung traegt die
    Aussage, die Farbe verstaerkt sie nur."""
    W, H = 360, 108
    L, R, T, B = 6, 6, 16, 18
    span = max(abs(h["dev_ct"]) for h in hours)
    span = max(span, 0.5)
    mid = T + (H - T - B) / 2
    half = (H - T - B) / 2
    slot = (W - L - R) / 24
    bw = slot * 0.62

    parts = [f'<line class="zero" x1="{L}" y1="{mid:.1f}" '
             f'x2="{W - R}" y2="{mid:.1f}"/>']
    by_hour = {h["hour"]: h["dev_ct"] for h in hours}
    for h in range(24):
        x = L + slot * (h + 0.5)
        if h % 3 == 0:
            parts.append(f'<text class="ax" x="{x:.1f}" y="{H - B + 12}" '
                         f'text-anchor="middle">{h}</text>')
        dev = by_hour.get(h)
        if dev is None:
            continue
        length = abs(dev) / span * half * 0.85
        # Teurer zeigt nach oben, guenstiger nach unten -- in SVG waechst y
        # nach unten, deshalb sitzt der teure Balken bei mid - length.
        cls = "bad" if dev > 0 else "good"
        y = mid - length if dev > 0 else mid
        parts.append(f'<rect class="bar {cls}" x="{x - bw / 2:.1f}" '
                     f'y="{y:.1f}" width="{bw:.1f}" height="{length:.1f}" '
                     f'rx="2"/>')

    parts.append(f'<text class="ax" x="{L}" y="{T - 5}">+{fmt_ct(span)}'
                 f'&#8201;ct teurer</text>')
    parts.append(f'<text class="ax" x="{L}" y="{H - B - 3}">'
                 f'&#8722;{fmt_ct(span)}&#8201;ct g&uuml;nstiger</text>')
    return (f'<div class="chart"><svg viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="Preisabweichung nach Tagesstunde">'
            f'{"".join(parts)}</svg></div>')


def svg_sparkline(lows: list[tuple]) -> str:
    """Tagestiefstwerte als kleine Linie -- zeigt den Trend des Bodens.

    Bewusst schlicht: ein Punkt je Tag, Linie dazwischen, erster und letzter
    Wert beschriftet. Kein Gitter, keine Achsen -- es geht nur um die Richtung."""
    W, H = 360, 66
    L, R, T, B = 40, 34, 10, 12
    vals = [v for _, v in lows]
    lo, hi = min(vals), max(vals)
    pad = max(0.003, (hi - lo) * 0.2)
    y0, y1 = lo - pad, hi + pad
    n = len(lows)

    def px(i: int) -> float:
        return L + (W - L - R) * (i / (n - 1) if n > 1 else 0.5)

    def py(v: float) -> float:
        return T + (H - T - B) * (1 - (v - y0) / (y1 - y0))

    pts_attr = " ".join(f"{px(i):.1f},{py(v):.1f}"
                        for i, (_, v) in enumerate(lows))
    parts = [f'<polyline class="spark" points="{pts_attr}"/>']
    # Nur den ersten und letzten Punkt betonen, sonst wird es unruhig.
    for i in (0, n - 1):
        _, v = lows[i]
        parts.append(f'<circle class="spark-dot" cx="{px(i):.1f}" '
                     f'cy="{py(v):.1f}" r="2.6"/>')
    # Wertebeschriftung links (erster) und rechts (letzter).
    parts.append(f'<text class="ax" x="{L - 5}" y="{py(lows[0][1]) + 3.5:.1f}" '
                 f'text-anchor="end">{fmt_eur_l(lows[0][1])}</text>')
    parts.append(f'<text class="ax" x="{W - R + 5}" '
                 f'y="{py(lows[-1][1]) + 3.5:.1f}" text-anchor="start">'
                 f'{fmt_eur_l(lows[-1][1])}</text>')
    return (f'<div class="chart"><svg viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="Tagestiefstwerte im Zeitverlauf">'
            f'{"".join(parts)}</svg></div>')


def build_html(stations: list[dict], state: dict, cfg: dict, now: datetime,
               history_rows: int, error: str | None = None,
               ana: dict | None = None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    loc = cfg["location"]
    by_id = {s["id"]: s for s in stations}

    # Offene Tankstellen mit Preis, sortiert nach Preis; Rest ans Ende.
    rows = []
    for sid, st in (state.get("stations") or {}).items():
        s = by_id.get(sid)
        if s:
            rows.append((s, st))
    # Preis zuerst, bei Gleichstand die naehere Tankstelle nach oben. In der
    # Stadt liegen oft ein Dutzend Betreiber auf demselben Zehntelcent -- dann
    # entscheidet nicht der Preis, sondern der Weg.
    open_rows = sorted([r for r in rows if r[1].get("diesel") is not None],
                       key=lambda r: (r[1]["diesel"], r[0].get("dist_km") or 99))
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

    # Veraltungshinweis (leerer Container + Skript, per Browser-Uhr gesteuert).
    stale_script = (STALE_JS
                    .replace("__CHECKED__", iso_utc(checked))
                    .replace("__LIMIT__", str(int(cfg["stale_after_min"]))))
    stale_box = (
        '<div id="stale" class="alert" hidden>'
        '<div class="alert__ic">&#9888;&#65039;</div><div>'
        '<div class="alert__t">Daten m&ouml;glicherweise veraltet</div>'
        '<div class="alert__d" id="stale-d"></div></div></div>'
    )

    # --- Auswertung: Einordnung, Verlauf, Tagesstunden ---
    verdict_html = curve_html = hours_html = ""
    if ana:
        v = ana["verdict"]
        if v:
            # Den absoluten Schwellen-Hinweis nur zeigen, wenn die Einordnung
            # ohnehin "guenstig" sagt -- sonst stuende "eher teuer" direkt neben
            # "unter deiner Schwelle" (die 2,25 sind bewusst locker gesetzt und
            # werden fast taeglich unterschritten). Die relative Einordnung ist
            # das staerkere Signal; die Schwelle bestaetigt sie nur.
            unter = ""
            if (v["cls"] == "good" and best_price is not None
                    and best_price <= cfg["alarm_schwelle"]):
                unter = (f' Und unter deiner Wunschmarke von '
                         f'{fmt_eur_l(cfg["alarm_schwelle"])}&nbsp;EUR.')
            # Zeitbewusste Empfehlung aus dem Stundenprofil -- nur wenn vorhanden.
            hint = ana.get("timing")
            hint_html = ""
            if hint and hint.get("wait"):
                hint_html = (
                    '<div class="verdict__hint"><span class="verdict__ar">'
                    '&#8595;</span><div>Erfahrungsgem&auml;&szlig; ist es gegen '
                    f'<b>{hint["hour"]}&nbsp;Uhr</b> am g&uuml;nstigsten, rund '
                    f'{fmt_ct(hint["gain_ct"])}&nbsp;ct billiger als jetzt. '
                    'Wenn dein Tank reicht: sp&auml;ter tanken.</div></div>'
                )
            elif hint is not None:
                hint_html = (
                    '<div class="verdict__hint"><span class="verdict__ar">'
                    '&#10003;</span><div>Du bist in der g&uuml;nstigen Tageszeit '
                    '&mdash; sp&auml;ter wird es erfahrungsgem&auml;&szlig; nicht '
                    'billiger.</div></div>'
                )
            verdict_html = (
                f'<div class="verdict {v["cls"]}">'
                f'<div class="verdict__t {v["cls"]}">{v["head"]}</div>'
                f'<div class="verdict__d">{v["detail"]}{unter}</div>'
                f'{hint_html}</div>'
            )

        # Beobachtungsdauer ehrlich benennen -- bei zwei Tagen ist das hier
        # eine Momentaufnahme und kein Muster.
        if ana["span_h"] >= 48:
            basis = f"{ana['span_h'] / 24:.0f} Tage"
        else:
            basis = f"{ana['span_h']:.0f} Stunden"
        spread_ct = (ana["max"] - ana["min"]) * 100
        curve_html = (
            '<div class="sec-head"><div class="sec-label">Verlauf</div>'
            f'<div class="sec-note">g&uuml;nstigster Preis, letzte {basis}</div>'
            '</div>'
            + svg_curve(ana, tz)
            + f'<p class="chart-note">Zwischen {fmt_eur_l(ana["min"])} und '
              f'{fmt_eur_l(ana["max"])}&nbsp;EUR &middot; {fmt_ct(spread_ct)}&nbsp;ct '
              f'Spanne, das sind {fmt_eur((ana["max"] - ana["min"]) * cfg["tank_liter"])}'
              f'&nbsp;EUR auf eine {cfg["tank_liter"]:.0f}-l-Tankf&uuml;llung. '
              f'Gezeichnet als Treppe: zwischen zwei Meldungen gilt der alte '
              f'Preis weiter.</p>'
        )

        # --- Laengerfristiger Trend: Tagestiefstwerte ---
        lows = ana.get("lows") or []
        if len(lows) >= cfg["trend_min_days"]:
            first_v, last_v = lows[0][1], lows[-1][1]
            delta_ct = (last_v - first_v) * 100
            days_span = (lows[-1][0] - lows[0][0]).days or len(lows)
            if delta_ct > 1.0:
                pfeil, rich = "&#8599;", (f"gestiegen (+{fmt_ct(delta_ct)}&nbsp;ct "
                                         f"in {days_span} Tagen)")
            elif delta_ct < -1.0:
                pfeil, rich = "&#8600;", (f"gefallen ({fmt_ct(delta_ct)}&nbsp;ct "
                                          f"in {days_span} Tagen)")
            else:
                pfeil, rich = "&#8594;", f"seitw&auml;rts (letzte {days_span} Tage)"
            tief = min(v for _, v in lows)
            curve_html += (
                '<div class="sec-head" style="margin-top:24px">'
                '<div class="sec-label">Trend</div>'
                f'<div class="sec-note">Tagestiefstwerte, letzte {len(lows)} Tage'
                '</div></div>'
                + svg_sparkline(lows)
                + f'<p class="chart-note">Der g&uuml;nstigste Tagespreis ist '
                  f'{pfeil}&nbsp;{rich}. Niedrigster Tagesboden im Fenster: '
                  f'{fmt_eur_l(tief)}&nbsp;EUR. Anders als die Detailkurve oben '
                  f'gl&auml;ttet das die Tagesschwankung weg und zeigt nur die '
                  f'Richtung.</p>'
            )

        if ana["hours"]:
            best_h = min(ana["hours"], key=lambda h: h["dev_ct"])
            worst_h = max(ana["hours"], key=lambda h: h["dev_ct"])
            n_days = max(h["days"] for h in ana["hours"])
            hours_html = (
                '<div class="sec-head"><div class="sec-label">Tageszeit</div>'
                f'<div class="sec-note">Abweichung vom Tagesmittel</div></div>'
                + svg_hours(ana["hours"])
                + f'<p class="chart-note">Am g&uuml;nstigsten gegen '
                  f'{best_h["hour"]}&nbsp;Uhr ({fmt_ct(abs(best_h["dev_ct"]))}'
                  f'&nbsp;ct unter dem Tagesmittel), am teuersten gegen '
                  f'{worst_h["hour"]}&nbsp;Uhr ({fmt_ct(abs(worst_h["dev_ct"]))}'
                  f'&nbsp;ct dar&uuml;ber). '
                  f'Basis sind {n_days} Tage &mdash; genug f&uuml;r eine Tendenz, '
                  f'nicht f&uuml;r ein Wochenmuster. Stunden ohne ausreichende '
                  f'Datenlage bleiben leer.</p>'
            )

        # --- Ersparnis in Euro (aus der Tageszeit-Spanne) ---
        sv = ana.get("savings")
        if sv:
            n = cfg["fuellungen_pro_monat"]
            hours_html += (
                f'<div class="savings"><div class="savings__big">rund '
                f'{fmt_eur(sv["per_fill"])}&nbsp;EUR</div><div class="savings__d">'
                f'Unterschied pro Tankf&uuml;llung ({cfg["tank_liter"]:.0f}&nbsp;l) '
                f'zwischen der g&uuml;nstigsten und der teuersten Tageszeit. '
                f'Wer konsequent zur billigen Zeit tankt, spart bei {n}&nbsp;'
                f'F&uuml;llungen im Monat rund <b>{fmt_eur(sv["per_year"])}&nbsp;'
                f'EUR im Jahr</b>.</div></div>'
            )

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

  {stale_box}
  {alert_html}
  {hero_html}
  {verdict_html}

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

  {curve_html}
  {hours_html}

  <div class="foot">
    <strong>{history_rows}</strong> Preis&auml;nderungen aufgezeichnet, seit der
    Tracker l&auml;uft. Wochentagsmuster folgt, sobald mehrere volle Wochen
    beobachtet sind.<br>
    Preisdaten von
    <a href="https://creativecommons.tankerkoenig.de">Tankerk&ouml;nig</a>
    (Markttransparenzstelle f&uuml;r Kraftstoffe), Lizenz
    <a href="https://creativecommons.org/licenses/by/4.0/">CC&nbsp;BY&nbsp;4.0</a>.
    Alle Angaben ohne Gew&auml;hr &middot; nicht-kommerzielle Nutzung.
  </div>
{stale_script}
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

    # Auswertung aus den CSVs -- unabhaengig vom Abruf, damit die Seite auch
    # bei einem Netzfehler noch Verlauf und Einordnung zeigt.
    try:
        ana = analyse_history(state, cfg, now)
    except Exception as e:                      # nie die ganze Seite riskieren
        ana = None
        print(f"Warnung: Auswertung uebersprungen ({type(e).__name__}: {e}).",
              file=sys.stderr)

    # Seltenheits-Push -- nur bei frischem Abruf (bei einem Fehler haben wir
    # keinen aktuellen Preis) und nie die Seite riskieren.
    if error is None and ana:
        try:
            maybe_push(ana.get("current"), cheapest_label(stations, state),
                       ana, cfg, now)
        except Exception as e:
            print(f"Warnung: Push uebersprungen ({type(e).__name__}: {e}).",
                  file=sys.stderr)

    html_out = build_html(stations, state, cfg, now, count_history_rows(),
                          error, ana)
    (out_dir / "index.html").write_text(html_out, encoding="utf-8")
    print(f"Dashboard geschrieben: {out_dir / 'index.html'}")

    # Bewusst Exit 0 auch bei Abrufproblemen: die Seite bleibt online und zeigt
    # den letzten Stand samt Hinweis. Der Fehler steht im Actions-Log.
    return 0


if __name__ == "__main__":
    sys.exit(main())
