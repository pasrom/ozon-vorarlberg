# Ozon Vorarlberg

Ozonwerte der vier Vorarlberger Messstationen holen, mit bis zu 39 Jahren
Messhistorie unterlegen und als Trainingsampel anzeigen.

Zwei Quellen, bewusst kombiniert:

| Quelle | Was sie liefert | Verzug |
|---|---|---|
| [vorarlberg-luft.at](https://www.vorarlberg-luft.at/tab1O3.htm) | den Jetzt-Wert | keiner |
| EEA Air Quality e-Reporting | Stundenwerte seit 1988 | 1–25 Stunden |

```
python3 -m pip install -r requirements.txt
python3 eea_archive.py --build                        # ganze Historie laden (einmalig, ~26 MB)
python3 ozon_vorarlberg.py --log --out data.json      # Jetzt-Wert holen
python3 -m http.server 8000
open http://localhost:8000/ozon_dashboard.html
```

Ohne den Archiv-Schritt läuft alles auch — dann eben ohne Langzeitkennzahlen,
und die Tageskurve beginnt beim ersten eigenen Log-Eintrag. Nur zum Ansehen:

```
python3 ozon_vorarlberg.py --demo --out data.json     # synthetische Werte
```

Die Demo ist im Dashboard mit einem Banner markiert. Nicht verwechseln.

---

## Woher die Historie kommt

**Die offizielle Landesseite hat keine.** Sie ist ein serverseitig generierter,
statischer HTML-Export (Generator im Meta-Tag: InterConnect Software, Footer
„Land Vorarlberg 2004"), wird stündlich neu geschrieben und enthält pro Station
genau fünf Zahlen:

| Spalte | Bedeutung |
|---|---|
| Akt. Messwert 1-h | aktueller Stundenmittelwert |
| Tagesmax. 1-h | höchster Stundenwert heute |
| Tagesmax. 8-h gleitend | höchstes gleitendes 8-Stunden-Mittel heute |
| Vortag 1-h / 8-h | dieselben Maxima von gestern |

Die verlinkten Stationsseiten (`statATVA007.htm`) bieten einen „Grafischen
Verlauf der letzten 2 bzw. 8 Tage" an — dahinter steckt aber nur ein fertig
gerendertes JPEG (`images/ATVA007O3EU.jpg`). Kein JSON, kein XML, keine API.
Dasselbe beim Umweltbundesamt: der Zeitverlauf unter
`luft.umweltbundesamt.at/pub/map_chart/index.pl` akzeptiert beliebige Daten und
Intervalle bis 28 Tage, gibt aber ebenfalls nur PNGs zurück.

**Die EEA hat sie.** Dieselben Messwerte liegen als Parquet in öffentlich
lesbaren Azure-Blob-Containern, ohne API-Key:

| Container | Datensatz | Zeitraum | pro Station |
|---|---|---|---|
| `airquality-p` | E2a, ungeprüft | 2025-01-01 → heute −1…25 h | ~0,6 MB |
| `airquality-p-e1a` | E1a, geprüft | 2013-01-01 → 2024-12-31 | ~2,0 MB |
| `airquality-p-airbase` | AIRBASE | ab 1988 → 2012-12-31 | 1,4–3,5 MB |

```
https://eeadmz1batchservice02.blob.core.windows.net/<container>/AT/<messpunkt>.parquet
```

Die Messpunkt-Kennung ist zweiteilig und deckt sich mit dem österreichischen
Immissionsdatenverbund, wo **08 das Netz Vorarlberg** ist:

| EEA-Messpunkt | IDV | Station |
|---|---|---|
| `SPO.08.0706.983.7.1` | 08/0706 | Lustenau Wiesenrain |
| `SPO.08.2708.5527.7.1` | 08/2708 | Bludenz Herrengasse |
| `SPO.08.2801.3213.7.1` | 08/2801 | Wald am Arlberg S16 |
| `SPO.08.0503.3670.7.1` | 08/0503 | Sulzberg Gmeind |

Die letzte Stelle `.7.` ist der Schadstoffcode für Ozon.

Ergebnis: **1 010 324 Stundenwerte**. Die Reihen sind unterschiedlich lang —
der airbase-Container reicht viel weiter zurück als die EEA-Oberfläche vermuten
lässt:

| Station | ab | Stunden | Abdeckung |
|---|---|---|---|
| Lustenau Wiesenrain | 1988-01 | 322 405 | 95,2 % |
| Sulzberg Gmeind | 1989-05 | 305 518 | 93,6 % |
| Wald am Arlberg | 2003-01 | 194 280 | 93,8 % |
| Bludenz Herrengasse | 2004-01 | 188 121 | 94,9 % |

### Verifiziert, nicht vermutet

Dass es dieselben Messungen sind, ist an **23 unabhängigen Referenzpunkten**
geprüft, alle exakt:

| Prüfung | Referenz | Ergebnis |
|---|---|---|
| 11 Stundenwerte (3 Uhrzeiten × 4 Stationen) | Screenshots 30.07.2026 | 11/11 exakt |
| Tagesmax. 8 h bis 13:00, 4 Stationen | Screenshots 30.07.2026 | 4/4 exakt |
| Tagesmax. 1 h, 4 Stationen | Vortagsspalte 13.08.2026 | 4/4 exakt |
| Tagesmax. 8 h, 4 Stationen | Vortagsspalte 13.08.2026 | 4/4 exakt |

Die EEA liefert unrundet (`138.487` statt `138`), die Landesseite rundet auf
ganze µg/m³ — bei ~1000 geprüften Nachkommastellen sind die Rohwerte
quasi-kontinuierlich. Alle Prüfungen laufen als Tests in
`test_eea_archive.TestAgainstOfficialSource` und überspringen sich, wenn der
Parquet-Cache fehlt.

### Zwei Zeitkonventionen, die beide falsch geraten waren

Das war der schwierigste Teil und der Ort für stille Ein-Stunden-Fehler:

**1. `Start` steht in UTC** und bezeichnet den Beginn der gemittelten Stunde.
Die Landesseite beschriftet dagegen nach dem **Ende** der Stunde und in
Lokalzeit: der auf der Seite als „13:00" ausgewiesene Wert liegt im EEA-Datensatz
unter `Start = 10:00` (= 12:00 MESZ, Fenster 12–13 Uhr lokal).

Eine frühere Annahme „feste MEZ" war um eine Stunde falsch. Sie stammte aus
einer Phasenkorrelation gegen Modelldaten, die zwischen 0 und −1 h praktisch
nicht trennt (r 0,87 gegen 0,86). **Exakte Integer-Treffer an 11 Werten schlagen
Korrelation** — daraus wurde die Regel, Konventionen nur gegen die Quelle selbst
festzunageln.

**2. Tageskennzahlen werden in fester MEZ aggregiert** (UTC+1, ohne
Sommerzeit), obwohl die Einzelwerte in Lokalzeit angezeigt werden. Zwei
verschiedene Dinge, die leicht zu verwechseln sind. Mit der Tagesgrenze in MESZ
stimmten nur 3 von 8 Tagesmaxima, mit fester MEZ alle 8.

Dazu die EU-Konvention: ein 8-h-Mittel gehört zu dem Tag, an dem es **endet**,
wobei 24:00 noch zum alten Tag zählt. Das erste Fenster eines Tages beginnt also
am Vorabend — und ist an ruhigen Vormittagen oft schon das Tagesmaximum. Genau
so kommt die Landesseite am 30.07. um 13:00 auf 107 µg/m³ für Bludenz, obwohl
kein Fenster des Vormittags über 94 lag.

`EEA_TZ` und `AGG_TZ` in `eea_archive.py` halten beides fest, sieben Tests
sichern es ab.

Der **Tagesgang** ist dagegen nach dem Fenster-*Start* in Lokalzeit beschriftet:
„06 Uhr" heißt die Stunde 06:00–07:00. Das ist die Lesart, die ein
Trainingsfenster braucht („um 06:00 rausgehen").

## Die zwei Bewertungsachsen

Das ist die inhaltliche Korrektur gegenüber der ersten Version. Dort stand der
akute 1-h-Wert groß auf der Karte, die Ampel färbte sich aber nach dem
**8-h-Tagesmaximum**. Das ist irreführend: mittags schleppt der gleitende
8-h-Wert noch die kühlen Morgenstunden mit und liegt deutlich unter dem, was
gerade tatsächlich in der Luft ist. Am 30.07.2026 um 13:00 zeigte Bludenz
130 µg/m³ akut, aber nur 107 im 8-h-Mittel — die Karte wurde grün-gelb, während
draußen 130 anlagen.

Jetzt getrennt:

**1. Trainings-Ampel** — läuft auf dem **aktuellen 1-h-Wert**, also auf dem, was
du gerade atmest.

| µg/m³ (1 h) | Status | Wort | Bedeutung |
|---|---|---|---|
| 0–99 | good | frei | volle Einheit möglich, auch intensiv |
| 100–119 | warning | ok | kurze Einheiten unkritisch, lange harte Blocks kürzen |
| 120–179 | serious | locker | nur Grundlage; Intervalle auf morgen früh |
| ab 180 | critical | drinnen | Informationsschwelle, Outdoor-Sport absagen |

Die 100er- und 120er-Marke sind von den 8-h-Richtwerten geliehen. Das ist eine
**pragmatische Skala, kein Rechtswert** — so steht es auch im Dashboard. Nur die
180 ist ein echter gesetzlicher Wert.

**2. Tagesbewertung** — läuft auf dem **8-h-Tagesmaximum** und ist der
gesundheitliche Kontext des Tages, gemessen an den echten Referenzwerten:

| Wert | Herkunft |
|---|---|
| 60 µg/m³ | WHO 2021, Langfrist: Mittel der 8-h-Tagesmaxima über die sechs ozonreichsten Monate |
| 100 µg/m³ | WHO 2021, Kurzzeit-Leitwert, max. 8-h-Tagesmittel. Rein gesundheitsbasiert |
| 120 µg/m³ | EU-Zielwert, 8 h gleitend. Darf an begrenzt vielen Tagen überschritten werden (max. 25 im Mittel über drei Jahre) |
| 180 µg/m³ | Österreichische Informationsschwelle, 1-h-Mittel. Ab hier wird die Bevölkerung aktiv gewarnt |

Dass die WHO-Werte strenger sind als die gesetzlichen, ist kein Widerspruch: die
WHO-Werte sind rein epidemiologisch abgeleitet, die gesetzlichen sind politisch
und technisch machbare Kompromisse.

**3. Langzeitvergleich** — aus dem Archiv: wo liegt der heutige Tageswert im
Vergleich zum selben Kalenderfenster (±3 Tage) aller Vorjahre? Der Tageswert
dafür kommt aus der **Live-Quelle**, nicht aus dem Archiv — das Archiv hinkt
1 bis 25 Stunden nach, sein Maximum für den laufenden Tag wäre
unvollständig. Genau dieser Fehler produzierte reproduzierbar absurde
Perzentile (Lustenau 67 statt 163 µg/m³), bevor die Zuständigkeit getrennt war.

## Dateien

| Datei | Zweck |
|---|---|
| `ozon_vorarlberg.py` | Scraper, Historien-Logger, Bewertung |
| `eea_archive.py` | EEA-Archiv laden, cachen, zu Kennzahlen verdichten |
| `ozon_dashboard.html` | Dashboard. Eine Datei, keine externen Libraries |
| `data.json` | Ausgabe des Scrapers, Eingabe des Dashboards. Atomar geschrieben |
| `archive.json` | Archivkennzahlen. Wird von `ozon_vorarlberg.py` mit eingelesen |
| `history.jsonl` | eine Zeile pro Snapshot, trägt das 72-h-Fenster (auf 4 Tage gekürzt) |
| `cache/eea/` | rohe Parquet-Dateien (~26 MB), damit `--build` offline wiederholbar ist |
| `fixtures/tab1O3_live.htm` | echter Abzug der Quellseite, unverändert. Basis der Tests |
| `fixtures/tab1O3_minimal.htm` | handgeschriebene Minimal-Fixture (ältere Struktur) |
| `test_ozon_vorarlberg.py` | 60 Tests |
| `test_eea_archive.py` | 73 Tests; die Quellenvergleiche brauchen den Cache, der Rest läuft offline |
| `deploy.sh` | scrapen, bauen, pushen. Lock, Heartbeat, Telegram bei Fehler |
| `refresh_archive.sh` | EEA-Archiv nachziehen (täglich) |
| `mini/install.sh` | Einrichtung auf dem Agent-Server, ohne root |
| `mini/*.plist` | die beiden LaunchDaemons |

`python3 -m unittest -v` führt alle 133 aus.

## CLI

```
python3 ozon_vorarlberg.py                       # JSON auf stdout
python3 ozon_vorarlberg.py --compact             # Klartext pro Station
python3 ozon_vorarlberg.py --log --out data.json # der Cron-Aufruf
python3 ozon_vorarlberg.py --station sulzberg    # filtern
python3 ozon_vorarlberg.py --html fixtures/tab1O3_live.htm   # offline
python3 ozon_vorarlberg.py --no-archive          # Archiv ignorieren
python3 ozon_vorarlberg.py --strict              # bei Layoutänderung abbrechen

python3 eea_archive.py --build                   # laden + archive.json
python3 eea_archive.py --build --since 2003      # Reihe kürzen
python3 eea_archive.py --stats                   # Kennzahlen ausgeben
python3 eea_archive.py --coverage                # Abdeckung pro Station/Jahr
python3 eea_archive.py --build --refresh         # Cache verwerfen
```

`--compact` mit Archiv:

```
# 14.08.2026 21:00  (Quelle: vorarlberg-luft.at)
serious   locker   Lustenau         akt= 136 ▼  1h-Max= 163 8h-Max= 155 [über EU-Zielwert]
serious   locker   Bludenz          akt= 155 ▬  1h-Max= 159 8h-Max= 150 [über EU-Zielwert]
serious   locker   Wald a. Arlberg  akt= 137 ▼  1h-Max= 147 8h-Max= 136 [über EU-Zielwert]
serious   locker   Sulzberg         akt= 149 ▼  1h-Max= 156 8h-Max= 151 [über EU-Zielwert]

-> Sauberste Station: Lustenau (136 ug/m3)
-> Archiv: 1010324 Stundenwerte, 1988-2026
-> Bestes Trainingsfenster (1988-2026, Saisondaten): 06-09 Uhr, Median 37 ug/m3
   Lustenau         heute 163 =  92. Perzentil (Median 112, Max 237 seit 1988)
   Bludenz          heute 159 =  99. Perzentil (Median 101, Max 165 seit 2004)
   Wald a. Arlberg  heute 147 =  98. Perzentil (Median 96, Max 189 seit 2003)
   Sulzberg         heute 156 =  90. Perzentil (Median 114, Max 209 seit 1989)
-> Historie: 3 Punkte / 4 h
```

### `--strict`

Ohne `--strict` ist der Parser tolerant und liefert, was er findet. Mit
`--strict` bricht er mit Exit-Code 2 ab, wenn eine Station fehlt, der
Zeitstempel unlesbar ist oder die Schwellwert-Zeile der Seite nicht mehr
`180 / 180 / 120 / 180 / 120` lautet. Letzteres ist der Kanarienvogel für eine
Spaltenumsortierung: die Seite dokumentiert ihre eigenen Grenzwerte in der
letzten Tabellenzeile. Für den Cron-Job ist `--strict` die richtige Wahl — ein
harter Fehler ist besser als stillschweigend falsch zugeordnete Zahlen.

## Was das Archiv sagt

Aus `eea_archive.py --stats`, Stand August 2026:

- **Bestes Trainingsfenster: 06–09 Uhr** an den Talstationen, Median 35–38 µg/m³.
  Die Faustregel „vormittags im Tal" ist damit nachgerechnet, nicht nur plausibel.
- **Sulzberg hat ein anderes Fenster: 09–12 Uhr**, und selbst dort liegt der
  Median bei 79 µg/m³ — mehr als doppelt so hoch wie im Tal zur besten Zeit. Die
  Höhenstation hat kaum einen Tagesgang, ihr Minimum liegt am späten Vormittag.
  Das ist die Aussage aus Abschnitt 4 der Zusammenfassung, quantifiziert: für
  Ozon ist die Talsohle am Morgen klar besser als die Bergtour.
- **Langfristiger Rückgang, deutlich an der Höhenstation.** Sulzberg:
  128 µg/m³ Peak-Season-Mittel 1990 → 97–103 in 2024/25. Lustenau: 100–107 in
  1988–90 → 88–94 in 2024/25. Die Luftreinhaltung wirkt, aber langsam.
- **2003 war der Ausnahmesommer**: Lustenau 121,5 µg/m³, 101 Tage über dem
  EU-Zielwert, 82 Stunden über der Informationsschwelle. Kein Jahr danach kam in
  die Nähe.
- **2024 war das sauberste Jahr** der Reihe: Lustenau 88,1, nur 2 Tage über 120.
- **2026 ist auffällig**: bis Mitte August schon 107,8 Peak-Season-Mittel und
  43 Tage über 120 an Lustenau — höher als 2018 (107,6) und damit der zweithöchste
  Wert der ganzen Reihe. Das Jahr ist nicht zu Ende, der Wert also ein
  Zwischenstand (im Dashboard hohl gezeichnet).
- **Der EU-Zielwert wird etwa in jedem zweiten Jahr gerissen**: erlaubt sind
  25 Tage im 3-Jahres-Mittel. Sehr ungleich verteilt — Sulzberg 87 % der Jahre,
  Lustenau 65 %, Bludenz 23 %, Wald am Arlberg 9 %.
- Zum **WHO-Langfristziel von 60 µg/m³** ist der Abstand groß und stabil: keine
  Station lag in der ganzen Reihe jemals unter 80.

## Wie genau ist die Historie?

- **Übereinstimmung mit der Quelle: 23/23 Referenzpunkte exakt** (siehe oben).
- **Auflösung**: die EEA liefert unrundete Fließkommawerte, die Landesseite
  rundet auf ganze µg/m³. Die Rundung ist die einzige Abweichung zwischen beiden.
- **Validity-Filter**: von 1 060 448 Zeilen sind 1 010 324 gültig (95,3 %),
  50 124 mit Flag `-1` (ungültig) werden verworfen. Andere Codes kommen nicht vor.
  Ein ungültiger Wert ist schlimmer als eine Lücke.
- **Lücken**: rund 8 500 pro Station über die ganze Reihe, davon ganz überwiegend
  einzelne Stunden — das sind die automatischen Kalibrierzyklen der
  Referenzgeräte, etwa 220 pro Jahr. Nur 10–44 Lücken je Station sind länger als
  24 Stunden.
- **Zwei echte Ausfälle**: Sulzberg fehlt das ganze Jahr 2000 (8 820 h), Wald am
  Arlberg fehlen 1 428 h ab Februar 2003. Beide Jahre fallen über die
  Abdeckungsschwelle von 80 % automatisch aus den Kennzahlen.
- **Lücken interpolieren nicht**: `rolling_8h` bricht ein Fenster an Lücken statt
  darüber hinweg zu mitteln, und verlangt mindestens 6 von 8 Stunden. Dadurch
  fehlt bei 0,01–0,05 % der Tage ein 8-h-Maximum — vernachlässigbar, aber es
  bedeutet, dass Überschreitungstage minimal untererfasst sein können.
- **2025/2026 ist ungeprüft** (E2a). Diese Werte können nachträglich korrigiert
  werden; `--refresh` holt Korrekturen. Ab 2024 rückwärts sind die Daten
  qualitätsgesichert (E1a bzw. AIRBASE).

## Betrieb auf dem Agent-Server

Der Logger läuft auf dem Mac mini als Benutzer `agent`, nach den Konventionen
aus `tools-workflow/concepts/mac-mini-agent-server.md` im Brain:

| | |
|---|---|
| `io.ebs.agent.ozon` | dreimal pro Stunde (:07, :27, :47) — scrapen, bauen, nach `gh-pages` pushen |
| `io.ebs.agent.ozon-archive` | täglich 04:17 — EEA-Archiv nachziehen |

Beides sind **LaunchDaemons** in `/Library/LaunchDaemons/` mit `UserName=agent`,
keine LaunchAgents: per-User-Agents laufen nur mit aktiver GUI-Session und wären
nach einem Reboot des headless Servers tot.

```
ssh agent@mac-mini
git clone git@github.com:pasrom/ozon-vorarlberg.git ~/git/ozon-vorarlberg
cd ~/git/ozon-vorarlberg && ./mini/install.sh
```

`install.sh` erledigt alles ohne root — venv, Abhängigkeiten, Deploy-Key,
Archiv, Testlauf — und gibt am Ende den sudo-Block für die beiden Daemons aus.

### Warum es so gebaut ist

- **`StartCalendarInterval`, nicht `StartInterval`.** War die Maschine zur
  geplanten Zeit aus, wird der Lauf übersprungen statt nachträglich gefeuert —
  sonst gibt es nach einem Stromausfall einen Ansturm gleichzeitiger Läufe.
- **`HOME`, `PATH`, `LANG` stehen im Plist.** launchd setzt `$HOME` nicht und
  liest `.zshenv` nicht; ohne diese drei scheitern `git` und `gh`.
- **Lock über `mkdir`.** `flock(1)` gibt es auf macOS nicht. Der `trap` räumt
  den Lock bei jedem Exit ab, auch bei Signal. Steht er länger als 30 Minuten,
  meldet der Job das per Telegram, löscht ihn aber nicht selbst — das würde
  zwei parallele Läufe erlauben.
- **Eigenes venv statt System-Python.** Das System-Python 3.9 des Minis hat die
  Abhängigkeiten nicht und ist extern verwaltet; `agent` ist non-admin und kann
  nicht `brew install`. Das venv baut auf dem vorhandenen Homebrew-Python 3.12 auf.
- **Deploy-Key statt credential-Helper.** launchd-Jobs haben keinen Zugriff auf
  den Schlüsselbund; über HTTPS hinge der Push still. `deploy.sh` bricht deshalb
  ab, wenn `origin` auf HTTPS steht, statt es zu versuchen.
- **Telegram nur im Fehlerfall**, über das vorhandene `~/agents/bin/notify.sh`.
  Erfolgreiche Läufe schweigen, sonst wird der Kanal zu Rauschen.
- **Logs unter `~/agents/logs/ozon/`**, wo die `newsyslog`-Rotation greift
  (14 Tage, bzip2). Heartbeat nach jedem erfolgreichen Lauf in
  `~/agents/state/ozon/heartbeat`.

### Bedienung

```
# Status. `launchctl list` ohne sudo zeigt nur die User-Domain und findet
# System-Daemons NICHT — dann wirkt es faelschlich so, als waeren sie weg.
launchctl print system/io.ebs.agent.ozon | grep -E "state|runs|last exit"
sudo launchctl list | grep io.ebs.agent.ozon

sudo launchctl kickstart -k system/io.ebs.agent.ozon  # sofort feuern
tail -f ~agent/agents/logs/ozon/$(date +%F).log       # mitlesen
date -r $(cat ~agent/agents/state/ozon/heartbeat)     # letzter Erfolg

sudo launchctl bootout system/io.ebs.agent.ozon       # anhalten
```

## Dashboard

Eine einzelne HTML-Datei, keine externen Requests, kein Build-Schritt. Muss über
HTTP kommen, nicht per Doppelklick — unter `file://` blockiert der Browser das
Lesen von `data.json`. Das Dashboard sagt das im Fehlerfall explizit.

- **Hero** — Lage jetzt an der höchsten Station, sauberste Station, bestes
  Zeitfenster, heutiger Langzeit-Perzentilwert.
- **Stationskacheln** — akuter Wert, Trend gegen den letzten Log-Eintrag,
  Sparkline, Tagesbewertung, Höhenlage, Langzeit-Perzentil.
- **Verlauf** — 24/48/72 h, aus **zwei Quellen zusammengesetzt**: das
  eigene Log trägt das Fenster, das Archiv füllt es bei der Erstinbetriebnahme
  vor. Der Kartentext nennt, wie viele Werte aus welcher Quelle
  kommen und bis wann das Archiv reicht. Referenzlinien bei 100/120/180,
  Crosshair mit Werten für alle Serien, per Maus oder Pfeiltasten. Lücken über
  2 h werden **nicht** interpoliert, damit ein ausgefallener Cron-Job nicht als
  glatte Linie durchläuft; einzeln stehende Punkte werden als Punkt gezeichnet,
  damit ein isolierter Tagespeak nicht unsichtbar wird.
- **Tagesgang** — Median je Stunde aus fünf Jahren Saisondaten (April–September).
  Eine einzelne Station in der Legende auswählen zeigt ihr
  25.–75.-Perzentil-Band; bei vier Stationen gleichzeitig wären vier Bänder
  Matsch. Ohne Archiv fällt die Karte auf die selbst geloggten Tage zurück.
- **Jahresreihe** — seit 1988, umschaltbar zwischen Peak-Season-Mittel, Tagen
  über dem EU-Zielwert und höchstem 1-h-Wert. Drei Kennzahlen mit zwei
  Einheiten, deshalb ein Umschalter und immer nur **eine** y-Achse — nie zwei
  Skalen in einem Plot.
- **Tabellenansicht** — dieselben Zahlen ohne Farbcodierung.
- **Muster** — schaltet die Linien auf unterscheidbare Strichmuster um, für
  Farbsehschwäche, Ausdruck und Schwarzweiß.

Auto-Refresh alle 5 Minuten. Beim Nachladen bleibt das alte Bild bei reduzierter
Deckkraft stehen, kein Skeleton-Flackern, kein Layout-Sprung.

### Farben

Vier Stationen = vier kategoriale Serienfarben (Blau, Orange, Aqua, Gelb), fest
pro Station zugeordnet über das Feld `slot` im JSON. Farbe folgt der Station,
nicht ihrem aktuellen Rang — wer beim Filtern die Farbe wechseln lässt, führt
Leser in die Irre.

Die Statusfarben (good/warning/serious/critical) sind davon getrennt und werden
**nie** als Serienfarbe benutzt. Sie treten immer mit Glyph und Wort auf, damit
die Bedeutung nie allein an der Farbe hängt.

Die Palette ist mit dem Validator geprüft (Helligkeitsband, Chroma-Untergrenze,
CVD-Trennung, Normalsicht-Untergrenze, Kontrast) und besteht die Gates für
Liniendiagramme in Hell und Dunkel. Bei vier Serien gleichzeitig liegen Gelb und
Orange im all-pairs-Vergleich unter der Untergrenze — kein Vierer-Set schafft
das im Dark Mode. Deshalb tragen alle Linien zusätzlich **Direktlabels am Ende**,
es gibt eine Tabellenansicht, und der Muster-Modus liefert die Zweitkodierung
über die Form. Beide Farbschemata sind eigenständig gewählt, nicht automatisch
invertiert.

## Grenzen und Fallstricke

- **Das Archiv wird nur etwa einmal täglich neu geschrieben.** Gemessen am
  `Last-Modified` des Blobs: am 14.08. um 08:12 UTC geschrieben und 12,5 Stunden
  später unverändert. Der Verzug der Messwerte schwankt damit zwischen rund
  **1 Stunde** direkt nach dem Schreibvorgang und rund **25 Stunden** davor.
  Als Füller für die letzten Stunden ist das Archiv deshalb unbrauchbar — es
  liefert die Langzeitkennzahlen, das 72-h-Fenster trägt das eigene Log.
  `archive.json` schreibt den `Last-Modified` unter `upstream` mit, damit sich
  der Rhythmus über die Tage belegen lässt statt geschätzt zu werden.
- **Der Logger muss also wirklich laufen.** Ohne ihn ist die Verlaufskurve leer
  bis auf das, was das Archiv beim letzten Lauf hergab — im schlechtesten Fall
  25 Stunden alt. Die Seite zeigt das über das „Daten sind alt"-Banner an, aber
  reparieren muss es der Cron-Job.
- **Zwei Beschriftungen im selben JSON.** Die Verlaufsreihe (`history`) läuft
  nach Fenster*ende*, wie die Landesseite. Der Tagesgang (`hour_profile`) läuft
  nach Fenster*start*, weil „06 Uhr" dort die Stunde meint, in der man rausgeht.
  `eea_archive.recent_series` rechnet dafür eigens um; ohne das lägen Archiv und
  Eigenlog um eine Stunde versetzt aneinander.
- **Vier Ozonstationen, nicht mehr.** Der Immissionsdatenverbund kennt acht
  Vorarlberger Stationen (zusätzlich Höchst Gemeindeamt, Lustenau Zollamt,
  Dornbirn Stadtstraße, Feldkirch Bärenkreuzung), vorarlberg-luft.at zeigt für
  Ozon aber nur vier. Wer die anderen will, muss den IDV scrapen.
- **Nur Ozon.** Die Quelle hat weitere Tabs (NO2, PM10, PM2.5, CO), und die
  Stationsseiten liefern diese Werte gratis mit; hier bewusst ignoriert.
- **Höhenangaben** in den Kacheln sind Näherungswerte und stammen **nicht** aus
  der Quelle.
- **Trend** ist der Sprung zum vorigen Log-Eintrag, nicht eine geglättete
  Steigung. Bei einem ausgefallenen Cron-Job vergleicht er über die Lücke hinweg.
- **Ungeprüfte Daten für 2025/2026.** `airquality-p` ist der E2a-Datensatz und
  noch nicht qualitätsgesichert; Werte können nachträglich korrigiert werden.
  `--refresh` holt Korrekturen mit.
- **Das Layout der Quelle ist stabil, aber nicht garantiert.** Sie ist von 2004.
  Stations-IDs werden aus den Detaillinks gelesen (`statATVA007.htm` →
  `ATVA007`), was stabiler ist als Namens-Matching; fehlen die Links, greift der
  Name als Fallback. `--strict` meldet, wenn beides bricht.
- **Der Parser stolperte an verschachtelten Tabellen.** Die Seite wickelt die
  Werte-Tabelle in eine Layout-Tabelle; der äußere `<tr>` enthält damit auch den
  ersten Stations-Link und wurde als Datenzeile gelesen — Bludenz kam mit
  `akt=None, max_1h=14` heraus, während die anderen drei stimmten. Genau dafür
  liegt jetzt ein echter Seitenabzug als Fixture im Repo; die alte
  handgeschriebene Fixture hatte diese Verschachtelung nicht.

## Sackgassen (damit sie niemand zweimal geht)

- `data.gv.at` CKAN-API: alle Pfade unter `/katalog/api/3/action/…` antworten
  mit 404.
- EEA-Legacy-Schnittstelle `fme.discomap.eea.europa.eu/…/AQData_Extract.fmw`:
  abgeschaltet, HTTP 401.
- Der `/ParquetFile/urls`-Endpunkt der neuen EEA-API gibt für `dataset:1` und
  `dataset:2` bei **jedem** Land 0 Treffer (auch DE und CH geprüft). Nur
  `dataset:3` liefert etwas. Der direkte Blob-Zugriff umgeht das Problem.
- OpenAQ v3 braucht einen kostenlosen API-Key (ohne: HTTP 401), v2 ist mit 410
  abgeschaltet. Da OpenAQ dieselben EEA-Daten einspeist, bringt es hier nichts.
- **Open-Meteo Air Quality** liefert keyless Ozon-Stundenwerte samt Vergangenheit
  und Prognose, ist aber ein **CAMS-Modell**, keine Messung: systematisch 25–50
  µg/m³ zu niedrig (Bludenz 97 statt 146), und für Wald am Arlberg greift die
  Modellzelle einen 1978-m-Gipfel statt der Talstation. Als Messwertersatz
  unbrauchbar. Als *Vorhersage* für morgen wäre es interessant — bisher nicht
  eingebaut.

## Nächste Schritte (offen)

- Open-Meteo als Ozon-**Prognose** für morgen einbauen (Bias-korrigiert gegen
  die eigene Messreihe — dafür sind jetzt bis zu 39 Jahre Referenz da).
- ESP32-Ampel mit RGB-LED, die `data.json` von einem kleinen Dienst holt.
- Home-Assistant-Sensor.
- Die vier zusätzlichen IDV-Stationen mitnehmen.
- Eigene Sensorik: für den Sportzweck reicht ein guter elektrochemischer Sensor
  mit NO2-Korrektur (Alphasense OX-B431 misst OX = O3 + NO2 und braucht einen
  zweiten Sensor zum Trennen). Absolutgenauigkeit gegen die amtlichen Stationen
  ist mit billigen Sensoren schwierig; UV-Absorption bei 254 nm ist das
  Referenzverfahren, das auch die Behörde nutzt.

## Quellen

- vorarlberg-luft.at, `tab1O3.htm` (Land Vorarlberg, Umweltinstitut)
- EEA Air Quality e-Reporting, Parquet-Blobs (E2a / E1a / AIRBASE)
- Umweltbundesamt Österreich, Immissionsdatenverbund
  (`luft.umweltbundesamt.at/pub/map_chart/index.pl`) — für die Stationscodes
- WHO global air quality guidelines 2021, Table 3.10
- IARC-Monographien zur Krebseinstufung (Ozon ist **nicht** als krebserregend
  eingestuft — es ist ein Reizgas, kein DNA-schädigendes Karzinogen; die
  Langzeitrisiken laufen über oxidativen Stress und Entzündung)
- Jerrett et al. 2009 (ACS-Studie, Atemwegssterblichkeit); Children's Health
  Study (Lungenwachstum und Neu-Asthma bei Kindern)
