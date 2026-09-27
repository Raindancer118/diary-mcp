# Design.md — diary-web „Observatorium"

Visuelle Entscheidungen für das lokale Frontend (`diary-web`). Wird **zuerst**
aktualisiert, wenn sich Design-Entscheidungen ändern.
(v0.23: löst den „Terminal-Kartograph" ab.)

## Kontext

Localhost-only Werkzeug für eine Person (Tom), die ihr eigenes Gedächtnis-System
inspiziert: navigieren, lesen, suchen, Zustand messen, syncen. Kein Produkt.

## Idee

Das Gedächtnis als **Nachthimmel, beobachtet aus einer Sternwarte**. Jede Memory
ist ein Stern, Links sind Sternbilder, die Stats sind die Messinstrumente.
Die Metapher ist nicht Deko, sie trägt Entscheidungen:

- **Typfarben = Spektralklassen.** Sterne werden nach Farbe klassifiziert,
  Memories nach Typ. Deshalb ist Farbe hier Identität, nicht Schmuck.
- **Wichtigkeit = Helligkeit (Magnitude).** Im Graph leuchten wichtige
  Memories heller und größer, statt einer Prozentleiste.
- **Nur Nacht.** Sternwarten arbeiten im Dunkeln; es gibt bewusst keinen
  Hell-Modus. Die Oberfläche ist warm (Tinte, nicht Blau-Schwarz), damit sie
  sich vom KI-Default „Cyan auf Dunkel" absetzt.

Verworfen: „Archiv/Editorial" (schön, aber zu ruhig für Graph + Stats),
„Kartograph 2.0" (zu nah am alten Look).

## Tokens

### Flächen (warm)
```
--night   #0d0b09   Grund
--vault   #15120f   Panels
--vault-2 #1c1814   gehobene Flächen, Hover
--rule    #2c261f   Linien
--ink     #ece4d4   Text (Sternenlicht)
--dust    #8f8472   Sekundärtext
--faint   #5c5446   Tertiär, Achsen
--accent  #e8a84c   Bernstein: aktive Zustände, Logo, Fokus — sparsam
```

### Spektralklassen (Typfarben) — validiert
Mit dem dataviz-Validator gegen `#15120f` geprüft (Lightness-Band, Chroma,
CVD-Abstand, Kontrast: alle PASS). **Feste Reihenfolge, nie umsortieren.**
```
feedback   #cc7d1b  oklch(.66 .14 65)
user       #3e8cc9  oklch(.62 .12 245)
project    #819f47  oklch(.66 .12 125)
reference  #7555a8  oklch(.52 .13 300)
note       #ca5551  oklch(.60 .15 25)
category   --faint (Struktur, keine Serie)
```
Sequenziell (Heatmap): ein Farbton, Bernstein, dunkel = wenig → hell = viel.

### Typografie (lokal ausgeliefert, OFL, `diary_web_assets/fonts/`)
- **Fraunces** (variabel, opsz/wght) — Titel und große Messwerte. Hat den
  Charakter alter Sternatlanten/Almanache. Tracking bei ≥ 48 px leicht
  negativ. **opsz 144 nur für Ziffern:** bei Buchstaben verschwinden dort die
  Haarlinien-Querstriche („H" → „I I", „+" → „|"). Titel: opsz 48–72, Gewicht
  ≥ 400.
- **Instrument Sans** — UI und Fließtext.
- **JetBrains Mono** — alle Pfade/Slugs. Das Pfad-Motiv bleibt Identität.

## Layout

Topbar (Wortmarke `✦ diary` · Ansichten `Archiv / Sternkarte / Messwerte` ·
Suche `⌘K` · Sync). Darunter wechselt die Ansicht:

- **Archiv:** Tree │ Memory │ Metadaten + Links (wie bisher, drei Spalten).
- **Sternkarte:** Vollflächiger Graph. Verknüpfte Memories ziehen zu einem
  Anker pro Projekt (Goldener-Winkel-Spirale) → echte Sternbilder mit Lücken;
  unverknüpfte bilden den Feldstern-Ring. Hover hebt das Sternbild hervor,
  Klick fliegt zum Stern und öffnet ihn im Archiv.
- **Messwerte:** Dashboard, vertikal scrollend, Abschnitte:
  Überblick (Hero-Zahlen) → Aktivität (Heatmap 1 Jahr + Verlauf) → Verteilung
  (Zweige, Spektralbalken, Wichtigkeit) → Qualität/Health → Graph →
  Injection → Projekt-Journal → Ranglisten → Instanz/Föderation.

Suche ist eine Befehls-Palette (Overlay), keine Dropdown-Liste.

## Motion

Leitlinie aus Toms Feedback (`/feedback/motion-design-subtle`): Arbeitsflächen
bleiben ruhig, Charakter kommt aus Federn und einem kräftigen Einstieg.
- **Einstieg (der eine laute Moment):** Himmel zoomt auf, Horizontlinie
  zeichnet sich unter der Leiste, der Stern zündet, die Headline steigt Wort
  für Wort aus der Unschärfe. Nur beim Laden (`body.boot`, 2,6 s).
- **Federkurven** (`--spring`, `--spring-soft` als `linear()`) für kleine
  Zustandswechsel: Tab-Unterstrich, Tree-Pfeil, Schalter, Dialog, Sterne der
  Wichtigkeit.
- **Wege kurz:** Aufstiege 6 px, Hover-Lift höchstens 1 px. Keine Bewegung an
  Navigations-Icons, kein Lichtfleck am Cursor, kein Glanz-Sweep.
- **Laufende Vorgänge pulsieren deutlich** (Sync-Knopf).
- **Hintergrund:** zwei Sternfeld-Ebenen driften extrem langsam (nur
  `transform`).
- **Ansichtswechsel:** View Transitions API, Fallback sofort.
- **Messwerte:** Zahlen zählen hoch, Balken wachsen, Linien zeichnen sich,
  Heatmap blendet diagonal ein, Abschnitte erscheinen scroll-getrieben.
- **Sternkarte:** Intro-Zoom, Sterne funkeln minimal, Hover dimmt alles außer
  dem Sternbild, Klick fliegt zum Stern.
- **`prefers-reduced-motion: reduce`:** alles aus, Endzustände sofort. In
  verborgenen Tabs (kein rAF) wird ebenfalls sofort der Endzustand gezeichnet.

## Charts (dataviz-Regeln)
Dünne Marken, 4px gerundete Datenenden an der Grundlinie, 2px Lücke
zwischen gestapelten Segmenten, zurückhaltende Achsen. Jede Chart-Marke hat
einen Hover-Tooltip; Text trägt nie die Serienfarbe; Legende ab 2 Serien.

## Barrierefreiheit
- Sichtbarer Fokus (Bernstein-Ring), alle Aktionen per Tastatur.
- `⌘/Ctrl+K` Suche, `1/2/3` Ansichten, `Esc` schließt Overlays.
- Touch-Targets ≥ 44 px in der Topbar.
- Alle aus der DB kommenden Strings werden escaped (kein `innerHTML` mit
  Rohdaten); Markdown wird erst escaped, dann in ein festes Tag-Set übersetzt.
- Ein Stylesheet (`app.css`), inline nur datengetriebene Custom Properties.
