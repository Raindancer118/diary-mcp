# Design.md — diary-web Memory Browser

Visuelle Design-Entscheidungen für das lokale Memory-Browser-Frontend (`diary-web`).
Diese Datei wird **zuerst** aktualisiert, sobald sich Design-Entscheidungen ändern.

## Kontext & Subjekt

Ein **localhost-only Werkzeug** zum Durchstöbern des Claude-Memory-Trees. Kein
Produkt-Frontend, sondern ein Entwickler-/Power-User-Instrument. Das Subjekt ist
buchstäblich ein **Dateisystem für Gedanken**: pfad-basierte Hierarchie
(`/user/...`, `/feedback/...`, `/projects/<slug>/...`, `/references/...`),
Knowledge-Graph-Verknüpfungen, Volltextsuche.

Zielgruppe: eine Person (Tom), die ihr eigenes Gedächtnis-System inspiziert.
Aufgabe der Seite: schnell navigieren, lesen, suchen, Health prüfen, syncen.

## Kreative Richtung

Gewählt: **„Terminal-Kartograph"** — eine ruhige, technische Oberfläche, die das
Pfad-Motiv (`/`) zum visuellen Leitmotiv macht. Kein generisches Dashboard,
sondern etwas zwischen einem File-Explorer und einem Knowledge-Graph-Inspektor.

Verworfen:
- Heller Karten-Dashboard-Look (zu generisch, „KI-Standard").
- Maximalistischer Graph-mit-Force-Layout (Überengineering für ein
  Lese-Werkzeug; lenkt vom Inhalt ab).

## Token-System

### Farben (`palette_commit`)
```
frame:    deep teal-black / cyan-green family
ground:   #091918   (fast schwarzes Tannengrün)
surface:  #0f2726   (Panels / Sidebar)
border:   #1c3c3a
text:     #c8dcdc   (entsättigtes Eisblau-Weiß)
muted:    #5a7878
accent:   #e8a84c   (Bernstein — der EINE Akzent, sparsam)
teal:     #5aabb8   (sekundär, für Pfade/Links)
```
Der Bernstein-Akzent ist die einzige warme Farbe und wird nur für aktive
Zustände, das Logo und Wichtigkeits-Punkte verwendet. Alles andere bleibt im
kühlen Teal-Spektrum → ruhig, ein Fokuspunkt.

### Typografie
- **UI / Body:** Inter / system-ui — neutral, lesbar für Memory-Inhalte.
- **Mono (Leitmotiv):** Cascadia Code / JetBrains Mono — für **alle Pfade**,
  Slugs, Metadaten, Logo, Buttons. Die Monospace-Schrift trägt die Identität:
  Pfade sehen aus wie im Terminal.
- Das Logo ist schlicht `/ memory` — der Slash in Bernstein, „memory" gedämpft.

### Layout
Drei Spalten (260px Tree │ flex Content │ 240px Panel), 48px Topbar:
```
┌──────────────────────────────────────────────────────┐
│ / memory   [ ⌕ search…            ]  health   sync     │
├────────────┬─────────────────────────┬─────────────────┤
│ tree       │ breadcrumb              │ metadata        │
│  ▸ user    │ # Node Title            │  Pfad / Datum   │
│  ▸ feedback│ [type] [tags] ●●●○○      │  Zugriffe / …   │
│  ▾ projects│                         │                 │
│    ▸ eduv. │ ┌─────────────────────┐ │ outgoing links  │
│    · node  │ │  body (markdown-ish)│ │  [related] →    │
│            │ └─────────────────────┘ │ referenced by   │
└────────────┴─────────────────────────┴─────────────────┘
```

## Detail-Entscheidungen

- **Typ-Farbpunkte** im Tree statt Icons: user=hellblau, feedback=bernstein,
  project=grün, reference=violett, note/category=gedämpft. Schnelle visuelle
  Klassifikation ohne Icon-Rauschen.
- **Wichtigkeit** als 5-Punkte-Leiste (Bernstein) — kompakt, kein Prozentbalken.
- **Breadcrumb** mit klickbaren Pfad-Segmenten, Slash-Separatoren in Bernstein.
- **Suche** mit `ts_headline`-Snippets, `«…»` als Highlight-Marker.
- **Knowledge-Graph** als Listen (ein-/ausgehend) statt Force-Layout — Klick
  navigiert zum verknüpften Node und expandiert den Tree-Pfad.
- **Health-Overlay** und **Sync** als modale Overlays, kein Seitenwechsel.

## Motion
Bewusst sparsam: nur Hover-/Focus-Transitions (.1–.15s) und das Aufklappen der
Tree-Toggles (Rotation). `prefers-reduced-motion` wird respektiert (alle
Transitions deaktiviert). Keine dekorative Animation.

## Barrierefreiheit
- Sichtbarer Keyboard-Focus auf Suche (Teal-Border).
- `Cmd/Ctrl+K` fokussiert Suche, `Esc` schließt Overlays.
- Kontrast: text #c8dcdc auf ground #091918 → hoher Kontrast.
