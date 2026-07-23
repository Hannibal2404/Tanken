# Diesel-Tracker Magdeburg-Buckau

Verfolgt die Dieselpreise der Tankstellen im Umkreis der Bleckenburgstraße
(Magdeburg-Buckau), zeichnet jede Preisänderung auf und zeigt den aktuellen
Stand als mobiles Dashboard.

- **Motor:** externer Trigger (cron-job.org) → `repository_dispatch` alle 20 Min;
  GitHub-eigener `schedule` alle 2 h nur als Sicherheitsnetz
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
| `alarm_schwelle` | Hinweis, wenn der günstigste Preis darunter liegt (EUR/l) |
| `history_days` | Fenster der Verlaufskurve (4 Tage) |
| `curve_step_min` | Auflösung der rekonstruierten Kurve (15 min) |
| `hourly_min_days` / `hourly_min_hours` | wann eine Tagesstunde im Profil auftaucht |

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
Tageskurve.

Weil nur Änderungen in der Datei stehen, baut die Auswertung den Verlauf als
**Treppe** wieder auf: zwischen zwei Meldungen gilt der letzte Preis weiter.
Ohne diesen Schritt würden Tankstellen, die häufig nachziehen, jede Statistik
dominieren.

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

## Warum der Takt von außen kommt

GitHubs eigener `schedule` ist „best effort": Läufe kommen Stunden zu spät oder
fallen ganz aus. Beim Schwesterprojekt *Wetter* lief er an drei Tagen gar nicht,
sonst bis zu 7 Stunden verspätet. Für einen Preistracker ist das gravierender
als für eine Wettervorhersage — **Tankerkönig liefert keine Historie nach**, ein
ausgefallener Lauf ist dauerhaft fehlende Datenlage.

Deshalb feuert cron-job.org alle 20 Minuten ein `repository_dispatch`
(Reaktionszeit ~40 s). Der GitHub-Cron bleibt mit 2-Stunden-Takt als Netz
bestehen: fällt der externe Dienst aus oder läuft das Token ab, sammelt das
Repo grob weiter, statt still zu verstummen. Doppelte Läufe kosten nichts, weil
bei unveränderten Preisen keine Zeile geschrieben wird.

### Einrichtung des Triggers

`POST https://api.github.com/repos/Hannibal2404/Tanken/dispatches`
mit Body `{"event_type":"spritpreise"}` und den Headern `Accept:
application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` sowie
`Authorization: Bearer <Token>`. Erfolg ist **HTTP 204**.

Das Token ist ein Fine-grained PAT, nur auf dieses Repo beschränkt, mit
**Contents: Read and write** *und* **Actions: Read and write**. Fehlt die
Actions-Berechtigung, antwortet die API mit 403. Ablaufdatum notieren — ein
abgelaufenes Token sieht von außen aus wie „die Seite hängt".

## Stand

**Schritt 1** (fertig): aktuelle Preise, günstigste hervorgehoben, Historie
wird gesammelt.

**Schritt 2** (fertig seit 23.07.2026): Verlaufskurve des günstigsten Preises,
Einordnung „jetzt tanken oder warten?", Tagesstunden-Profil, Preisalarm,
Ersparnis in Euro pro Tankfüllung.

Die Auswertung liest ausschließlich die CSV-Historie und läuft deshalb auch
dann, wenn der API-Abruf scheitert. Was sie zeigt, hängt an der Datenmenge:

| Auswertung | Bedingung |
| --- | --- |
| Verlauf + Einordnung | ab 16 Messpunkten, also praktisch sofort |
| Tagesstunden-Profil | jede Stunde aus ≥ `hourly_min_days` Tagen, gezählt werden nur Tage mit ≥ `hourly_min_hours` Stunden Abdeckung |
| Wochentagsmuster | noch nicht gebaut — braucht mehrere volle Wochen |

Angebrochene Tage bleiben beim Stundenprofil außen vor: ihr Tagesmittel steht
schief, weil ihnen genau die teure oder genau die billige Tageshälfte fehlt.
Deshalb sind einzelne Stunden anfangs leer, und das ist richtig so.
