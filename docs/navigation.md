# Navigation og kompatibilitet

Streamlit ejer browserhistorik og paths via `st.navigation(..., position="hidden")` og filbaserede `st.Page`-moduler. Sidebaren er fortsat appens synlige, dynamiske navigation. `website/navigation.py` er den eneste route-tabel; UI-kode bruger `go_to()` eller `page_link()` og må ikke vedligeholde egne URL-maps.

## Canonical routes

| Side | Path og kontekst |
| --- | --- |
| Forside | `/` |
| Administrér spil | `/manage-games` |
| Arkiv | `/archive` |
| Spillere | `/players` |
| Scouting | `/scouting?locale=…&game=…&view=watchlist|smartlists|notes` |
| Hold | `/teams` |
| Managers | `/managers` |
| Kalender | `/calendar` |
| Data | `/data` |
| Spil | `/game?locale=…&game=…&section=…` |
| Gruppe/turnering | `/group?group=…&section=…` |
| Hold-detalje | `/team?group=…&team=…&panel=…` |
| Spiller-detalje | `/player?locale=…&game=…&player=…&round=…` |

De kontekstuelle paths kræver en gyldig lokal entitet. Et ukendt spil eller en ukendt gruppe giver en forklarende fejltilstand og et link hjem. Et ukendt legacy-view går til den kontrollerede `/not-found`-side; det åbner ikke forsiden lydløst.

## Legacy-matrix

Gamle links med `?view=…` genkendes kun i entrypointen og viderestilles én gang. Alle andre query-parametre bevares.

| Legacy `view` | Canonical destination |
| --- | --- |
| `home` | `/` |
| `manage-games` | `/manage-games` |
| `archive` | `/archive` |
| `players` | `/players` |
| `scouting` | `/scouting` |
| `teams` | `/teams` |
| `managers`, `hall-of-fame` | `/managers` |
| `calendar` | `/calendar` |
| `data` | `/data` |
| `game` | `/game` |
| `group` | `/group` |
| `team` | `/team` |
| `player` | `/player` |
| `alerts` | `/alerts` kompatibilitetsside |
| enhver anden værdi | `/not-found` |

`alerts` viderestiller til `/game?...&section=alerts`, når et registreret managerspil kan bestemmes. For et ikke-registreret, men gyldigt spil viser `/alerts?locale=…&game=…` den selvstændige cachebaserede fallback. Uden tilstrækkelig kontekst vises en forklaring frem for en automatisk hentning.

Arkiverede managerspil bruger de samme canonical paths. `UiContext.read_only` deaktiverer muterende handlinger på spil-, gruppe-, hold- og spillersider; kun den eksplicitte gendannelseshandling på spillet kan ophæve tilstanden.

Spillerstatistikkens kontekstuelle paneler er `list`, `scouting`, `compare`, `watchlist` og `changes`. Eksisterende `panel=compare` bevares, mens tidligere watchlistlinks bruger `panel=watchlist`. Den globale Scouting-side anvender kun `view`; skift mellem views eller spil starter ikke netværk og skriver ikke indstillinger.
