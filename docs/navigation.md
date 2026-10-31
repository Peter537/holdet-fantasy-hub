# Navigation og kompatibilitet

Streamlit ejer browserhistorik og paths via `st.navigation(..., position="hidden")` og filbaserede `st.Page`-moduler. Sidebaren er fortsat appens synlige, dynamiske navigation. `website/navigation.py` er den eneste route-tabel; UI-kode bruger `go_to()` eller `page_link()` og må ikke vedligeholde egne URL-maps.

Den daglige arbejdsgang starter i **Mine managerspil**. Åbn et managerspil, vælg en liga eller turnering, og brug **Spillere** til at undersøge statistik og spillerværdi. Værktøjer som Scouting, statusalarmer, analyse, managerkarrierer, kalender og Rundens historie er tilvalg. De aktiveres under **Indstillinger**; deaktivering skjuler indgangen og sletter ingen gemte data.

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
| Indstillinger og valgfrie værktøjer | `/settings` |
| Spil | `/game?locale=…&game=…&section=…` |
| Gruppe/turnering | `/group?group=…&section=…` |
| Hold-detalje | `/team?group=…&team=…&panel=…` |
| Spiller-detalje | `/player?locale=…&game=…&player=…&round=…` |

De kontekstuelle paths kræver en gyldig lokal entitet. Et ukendt spil eller en ukendt gruppe giver en forklarende fejltilstand og et link hjem. Et ukendt legacy-view går til den kontrollerede `/not-found`-side; det åbner ikke forsiden lydløst.

## Managerspillets navigation

Managerspillet har tre hovedfaner:

| Fane | Bevaret `section` | Indhold |
| --- | --- | --- |
| Overblik | `round-center` | Gemte runder, spillerdækning, datastatus, stillingsudsnit og kampe |
| Ligaer og turneringer | `groups` | Spillets grupper og turneringer |
| Spillere | `players` | Spillerliste, statistik og spillerværdi |

**Flere funktioner** er en direkte menu til holdstatistik, historik, administration og spilindstillinger. Valget åbner visningen med det samme. Spil og konkurrencer deler en header med brødkrummer og de samme opdaterings- og downloadhandlinger. Statusalarmer og analyse vises her, når de er tilvalgt. De eksisterende links med `section=teams`, `history`, `administration`, `settings`, `alerts` og `analysis` bevarer deres destination; de omfortolkes ikke til en anden hovedfane.

Et direkte link til et deaktiveret tilvalg viser en forklaring og handlingerne **Åbn denne gang** og **Aktivér værktøj**. Den første giver adgang i den aktuelle session uden at ændre gemte indstillinger. Den anden gemmer tilvalget ved et eksplicit klik. Et arkiveret managerspil forbliver skrivebeskyttet, også når et værktøj åbnes denne gang.

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
| `settings` | `/settings` |
| `game` | `/game` |
| `group` | `/group` |
| `team` | `/team` |
| `player` | `/player` |
| `alerts` | `/alerts` kompatibilitetsside |
| enhver anden værdi | `/not-found` |

`alerts` viderestiller til `/game?...&section=alerts`, når et registreret managerspil kan bestemmes. For et ikke-registreret, men gyldigt spil viser `/alerts?locale=…&game=…` den selvstændige cachebaserede fallback. Uden tilstrækkelig kontekst vises en forklaring frem for en automatisk hentning.

Arkiverede managerspil bruger de samme canonical paths. `UiContext.read_only` deaktiverer muterende handlinger på spil-, gruppe-, hold- og spillersider; kun den eksplicitte gendannelseshandling på spillet kan ophæve tilstanden.

Spillerstatistikkens kontekstuelle paneler er `list`, `scouting`, `compare`, `watchlist` og `changes`. Eksisterende `panel=compare` bevares, mens tidligere watchlistlinks bruger `panel=watchlist`. Den globale Scouting-side anvender kun `view`; skift mellem views eller spil starter ikke netværk og skriver ikke indstillinger.

`section`, `panel`, `round`, `locale`, `game`, `group` og entitets-ID'er hører til deres respektive side. Et skift mellem managerspillets hovedfaner rydder det tidligere spillerpanel, så et panel fra Spillere ikke vælger en utilsigtet visning på den næste fane. Globale sider som Managers og Data har deres egne `section`-værdier.

Se [Rundecenter og daglig arbejdsgang](round-center.md) for preview, afgrænset opdatering og indhentning af manglende runder efter en pause.
