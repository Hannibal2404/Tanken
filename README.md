# Diesel-Tracker Magdeburg-Buckau

Verfolgt die Dieselpreise der Tankstellen im Umkreis der Bleckenburgstraße
(Magdeburg-Buckau), zeichnet jede Preisänderung auf und zeigt den aktuellen
Stand als mobiles Dashboard.

- **Motor:** GitHub Actions (`schedule` alle 20 Min + `workflow_dispatch`)
- **Anzeige:** GitHub Pages, nativer Deploy via `actions/deploy-pages@v4`
- **Datenquelle:** [Tankerkönig](https://creativecommons.tankerkoenig.de)
  (Markttransparenzstelle für Kraftstoffe), CC BY 4.0, nicht-kommerziell
- **Historie:** wird vom Workflow ins Repo zurück-committet → `data/prices/`

## Einrichtung

### 1. API-Key beantragen

Auf https://creativecommons.tankerkoenig.de → „API-Key beantragen". Als
Verwendungszweck das angeben, was es ist: privates, nicht-kommerzielles
Hobbyprojekt zur Anzeige der Dieselpreise in der eigenen Nachbarschaft.
Der Key kommt per Mail (manchmal sofort, manchmal ein bis zwei Tage).

### 2. Key als GitHub-Secret hinterlegen

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Feld | Wert |
| --- | --- |
| Name | `TANKERKOENIG_API_KEY` |
| Secret | der Key aus der Mail |

Der Key steht damit **nicht** im Code und **nicht** im Repo. Actions maskiert
ihn in den Logs, und da alle API-Aufrufe im Runner stattfinden, landen auf der
öffentlichen Seite nur die fertigen Zahlen.

Lokal zum Testen:

```powershell
$env:TANKERKOENIG_API_KEY = "dein-key"
python spritpreise.py
```

### 3. Pages aktivieren

Repo → **Settings → Pages → Source: GitHub Actions**.

### 4. Ersten Lauf starten

Actions-Tab → *Spritpreise* → **Run workflow**. Der erste Lauf macht die
Umkreissuche, legt `data/stations.json` an und schreibt die erste Preiszeile.

## Konfiguration

Alles Einstellbare steht im `CONFIG`-Block oben in
[`spritpreise.py`](spritpreise.py):

| Schlüssel | Bedeutung |
| --- | --- |
| `location` | Standort der Umkreissuche (Bleckenburgstraße, 52.1114 / 11.6370) |
| `radius_km` | Suchradius, Start 10 km (API-Limit: 25 km) |
| `max_stations` | wie viele der nächstgelegenen Tankstellen verfolgt werden |
| `station_refresh_days` | wie oft die Umkreissuche wiederholt wird |
| `diff_good_ct` / `diff_warn_ct` | Farbschwellen für den Aufpreis in Cent |
| `tank_liter` | Tankgröße für die Euro-Ersparnis (50 l) |
| `alarm_schwelle` | Preisalarm in EUR/l — greift ab Schritt 2 |

Nach einer Änderung von `radius_km`, `max_stations` oder `location` einmal
manuell mit **Umkreissuche neu durchführen** starten (Häkchen im
`workflow_dispatch`-Dialog). Das Abfrageintervall steht im `cron`-Ausdruck in
[`.github/workflows/spritpreise.yml`](.github/workflows/spritpreise.yml).

## Wie die Persistenz funktioniert

```
data/
├── stations.json        Stammdaten der Tankstellen (selten aktualisiert)
├── latest.json          aktueller Stand je Tankstelle (Preis, Status, seit wann)
└── prices/
    └── 2026-07.csv      die Historie, eine Datei pro Monat
```

`prices/YYYY-MM.csv`:

```csv
timestamp_utc,station_id,status,diesel
2026-07-20T09:27:11Z,51d4b61a-a095-1aa0-e100-80009459e03a,open,1.679
```

Geschrieben wird **nur bei einer Änderung**. Spritpreise sind eine
Treppenfunktion — ein unveränderter Wert braucht keine Zeile. Die Historie
bleibt dadurch exakt, das Repo aber klein (etwa 5–10 statt 72 Zeilen pro
Tankstelle und Tag).

Zeiten stehen konsequent in **UTC**, angezeigt und ausgewertet wird
**Europe/Berlin**. Sonst zerreißt die Sommerzeitumstellung im Oktober die
Tageskurve aus Schritt 2.

### Das Zurück-Committen aus dem Workflow

Vier Zutaten, mehr ist es nicht:

1. `permissions: contents: write` — gibt dem automatisch bereitgestellten
   `GITHUB_TOKEN` Schreibrechte. Kein PAT nötig, weil wir ins *eigene* Repo
   schreiben.
2. `actions/checkout@v4` legt diesen Token beim Auschecken schon als
   Git-Credential ab. Da ist nichts weiter zu konfigurieren.
3. Der Runner hat keine Git-Identität, also wird eine gesetzt
   (`github-actions[bot]`).
4. Committen nur, wenn sich wirklich etwas geändert hat — sonst schlägt
   `git commit` fehl und färbt den Job rot. Deshalb der
   `git diff --staged --quiet`-Test.

Zwei Fallen, die hier schon entschärft sind:

- **Endlosschleife.** Hätte der Workflow einen `push`-Trigger, würde sein
  eigener Commit ihn neu starten. Deshalb kein `push`-Trigger und zusätzlich
  `[skip ci]` in der Commit-Message.
- **Kollisionen.** `concurrency: group: spritpreise` verhindert, dass zwei
  Läufe gleichzeitig am selben Branch schreiben. Falls doch mal einer
  dazwischenkommt, versucht der Push-Schritt bis zu dreimal mit `pull --rebase`.

## API-Nutzung

Zweistufig, damit die CC-Schnittstelle nicht unnötig belastet wird:

| Aufruf | Wann | Zweck |
| --- | --- | --- |
| `list.php` | erster Lauf, Config-Änderung, alle 30 Tage | Tankstellen im Umkreis finden |
| `prices.php` | jeder Lauf | aktuelle Preise, max. 10 IDs pro Request |

Tankerkönig erlaubt einen Abruf alle 5 Minuten. Der Workflow läuft alle 20
Minuten — Faktor 4 Sicherheitsabstand.

## Stand

**Schritt 1** (aktuell): aktuelle Preise, günstigste hervorgehoben, Historie
wird gesammelt.

**Schritt 2** (sobald genug Daten da sind): Tageskurve je Tankstelle,
„jetzt tanken oder warten?", Wochentagsmuster, Preisalarm, Ersparnis in Euro
pro Tankfüllung.
