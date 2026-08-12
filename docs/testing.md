# Tests

Pytest er projektets autoritative test runner. Suiten indeholder både unittest-baserede klasser, pytest-funktioner og Streamlit AppTest-scenarier; pytest samler dem alle. Den tidligere discovery-kommando overså de pytest-baserede Hub-tests og må derfor ikke bruges som fuld suite.

## Installation og komplet suite

Installer website- og testafhængigheder fra repositoryets rod:

```powershell
py -3.14 .\scripts\install_locked_dependencies.py
py -3.14 -m pip install --no-deps --no-build-isolation -e .
```

Den ene repositorylås indeholder core-, website-, Parquet-, test- og
UI-testgrafen for Windows/Python 3.14. Opdatér den kun med den fail-closed
resolver- og OSV-kontrol, som sender offentlige PyPI-navne og versioner, men
ingen repositoryfiler eller private data. Da pips `lock`-kommando og
`pylock.toml`-format stadig er eksperimentelle, validerer installationsscriptet
låsen og installerer dens eksakte wheel-URL'er med SHA-256-kontrol:

```powershell
py -3.14 .\scripts\refresh_dependency_lock.py
py -3.14 .\scripts\refresh_dependency_lock.py --check
```

Kør derefter hele suiten:

```powershell
py -3.14 -m pytest tests -q
```

De almindelige tests må ikke kontakte Holdet.dk. HTTP-svar kommer fra lokale fixtures eller injicerede fetch-funktioner, og filesystemtests bruger test-ejede midlertidige `AppPaths`.

## Parser-canary

Den markerede canary-suite er opt-in og er den eneste test, som kontakter offentlige Holdet-endpoints:

```powershell
py -3.14 -m pytest tests/parser_canary -q --run-parser-canary
```

Uden flaget skippes canary-testene, og standardsuiten er helt offline. Canarien kalder `HoldetClient.fetch_players` for de fem aktuelle fodbold-, cykel-, Formel 1- og golfspil. Den validerer format, enhed, positiv runde, ikke-tomme entries, obligatoriske spillerfelter og kontrakten for `popularity`, `popularityChange`, `trend`, `index`, `stats` og `totalStats`. Rå `stats`/`totalStats` skal findes med en kendt mappe-/listeform; en legitim tom mappe er tilladt, mens ikke-tomme rå værdier skal overleve parseren. Drift i de valgfrie felter rapporteres som kildedrift; produktionsparseren forbliver fail-closed og kompatibel med de obligatoriske felter.

## Testområder

| Område | Eksempler på dækning |
| --- | --- |
| URL, parsere og HTTP | Normalisering, Flight-dekodning, payloadvalidering, retries, proxy og redaction |
| Spillere og scouting | Schema 1–3 → 4, valgfrie payloadfelter, percentilties og kohortegates, potentiale/risiko/ownership, peers, similaritet, smartlister, intra-runde-diff og evidence-first decomposition |
| Watchlist og formler | Absolutte/procentvise krydsninger, op/ned, Form 3/5, statusbedring, reset, samme-runde-event-ID'er, atomiske bulkhandlinger samt sikker AST-parser med angrebstests og synlige cellefejl |
| Hold | Kontoopdagelse, roster, historik, rang, gruppeplacering og ændringer |
| Transfer | Fire regelprofiler, gebyr, kontrakter, formation, klubgrænser, kaptajnregler og `final`/`preliminary`/`unverified` |
| Beslutningsanalyse | Formhuller, stabilitet, nul/negativ vækst, kaptajnmismatch, 0/0,5/1 % rente og gebyr, afrunding, transferhuller, kontrafaktisk sum og bankhitrater |
| Idealhold og model | Brute-force-paritet, tie-break, infeasible, timeout/bound, cirka 400 kandidater, deterministisk seed, fælles-spiller-annullering, intervaller, dækning og walk-forward-backtest |
| Gruppe og alarmer | Ledersammenligning, eksponeringsnævnere, manglende hold, alarmtransitioner, deduplikering, læst/afvist og separat spillerfallback |
| Rundecenter | Næste handling, handelsvindue, `completed_needs_refresh`, afvigelser, sessionfiltre, nærmeste tidligere runde, latest-corrected time machine og fuld gruppematrix |
| Refresh og manifest | Cache-only preview, alt/forældet/retry-planer, holddeduplikering, fremdrift pr. kilde, cachefallback, manifest schema 2 og dual-read af schema 1 |
| Historik | Huller, seneste snapshot pr. runde og omvendte rangakser på spil-, gruppe- og holdniveau |
| Managers og sæsoner | Identitetsgraf, stabilt ID ved merge/rename/unmerge, legacy-remapping uden writes, bedste hold, locale- og puljeisoleret Elo, awards, streaks, historier, H2H-aggregater, sæsonredigering og pointprofil |
| Eventledger og kalender | Revisioner, legacy-events, manglende metadata, cache-only events og nul navigation-writes |
| Dataportabilitet | DataPackage, Unicode, rå taltyper, CSV-injection, XLSX-ark, valgfri Parquet, rapportescaping, forklarlige historiefakta, sikre dual links og anonymiseringsprofiler |
| Lagring og backup | AppData, atomiske writes, HubSettings 1–3 → 4, spiller-snapshot 1–3 → 4, inbox 1 → 2 og GameMetadata 1 → 2 uden startup-write, integritetsindeks, importklassifikation, arkivgrænser, SHA-256, path traversal, preview, staging og rollback |
| Lokalt API | Catalog, filtre, pagination, CSV/JSON-paritet, ETag/304, Host/loopback, sikre headere, nul writes og nul netværk |
| Turnering | Liga, Swiss, gruppespil + knockout, double elimination, fuld Swiss-afslutning, custom byepoint, Buchholz, seedning, tie-breakers, bronzekamp, kontekstvalidering og konflikter for frosne parringer |
| Dashboard | `/scouting`, globale notesøgninger, atomiske bulkhandlinger, formler, fem spillerpaneler, statusprovenance, alarmfaner og badges, kompatibilitetsruter, deeplinks og nul netværk/writes ved navigation |
| Native navigation | Canonical path-matrix, alle legacy-redirects, query-bevarelse, ugyldige entiteter, arkiveret read-only og filbaseret `AppTest.switch_page` |
| Reruns og state | Inaktive faner med spies, fragmentgrænser, Apply/Reset, fulde reruns efter writes, stabile dataframe-keys og scroll/sortering gennem fragment-rerun |
| Dokumentation og API | Links, Mermaid-hegn, kommandoer, AppData-stier, `holdet_lib.__all__` og evaluerbare type hints |

## Streamlit AppTest

AppTest åbner hovedroutes og query-parametre uden en virkelig server. Tests beviser blandt andet, at `/scouting` og de fem spillerpaneler bevarer query-kontekst, at globale notesøgninger og formelvisning læser cache, at Statusalarmer ikke er en global sidebardestination, at hvert managerspil har en spilfiltreret alarmfane med unread-badges, og at legacy Hall of Fame-routen viderestiller. Rundecenterets preview, time machine og sessionfiltre samt Scouting-, Analyse-, spiller-, alarm-, manager-, kalender- og almindelig navigation og transfersimulation må hverken kalde netværk eller skrive persistent data. Den kanoniske funktionskontrakt står i [Rundecenter og daglig arbejdsgang](round-center.md).

## Lokal UI-regression og accessibility

Den fulde lås installerer UI-testpakkerne. Installér derefter kun den af
Playwright-versionen fastlagte Chromium-build én gang:

```powershell
py -3.14 -m playwright install chromium
```

Opret først de lokale baselines. PNG-filerne er maskinspecifikke testdata og er
ignoreret af Git; de skal derfor genereres på hver udviklingsmaskine og må ikke
committes:

```powershell
py -3.14 -m pytest tests/ui -q --run-ui --browser chromium --update-ui-snapshots
```

Kør derefter den opt-in Windows/Chromium-suite:

```powershell
py -3.14 -m pytest tests/ui -q --run-ui --browser chromium
```

En session-fixture starter `website/server.py` på en tilfældig fri loopback-port med et isoleret, deterministisk `HOLDET_DATA_DIR`. Suiten bruger reduceret bevægelse og indeholder ti baselines: forsiden ved 375, 768, 1280 og 1920 px, Rundecenter ved 375 og 1280 px, en tæt spillerliste ved 1280 px, Scouting ved 375 og 1280 px samt spillerdetaljen ved 1280 px.

Pillow-sammenligningen tillader højst 0,1 % ændrede pixels efter en per-kanal-tolerance på 10. Ved fejl gemmes `actual` og `diff` i den ignorerede mappe `tests/ui/artifacts/`. Manglende baselines fejler med bootstrap-kommandoen ovenfor, og eksisterende lokale baselines må kun erstattes eksplicit:

```powershell
py -3.14 -m pytest tests/ui -q --run-ui --browser chromium --update-ui-snapshots
```

Accessibility-smoken kontrollerer én H1, overskriftshierarki, navngivne knapper, tab/tabpanel-state, tastatur og synligt fokus, 200 % zoom/reflow, dokument-overflow samt datatabeller ved hvert scoutingplot. En browsertest kontrollerer desuden, at spillerlistens sorteringsvalg og faktiske dataframe-scrollposition bevares gennem et fragment-rerun.

## Afsluttende accept

Kør standardsuite, de to opt-in-suiter efter behov og miljø-/diffkontrol:

```powershell
py -3.14 -m pytest tests -q
py -3.14 -m pytest tests/ui -q --run-ui --browser chromium
py -3.14 -m pytest tests/parser_canary -q --run-parser-canary
py -3.14 -m pip check
py -3.14 .\scripts\refresh_dependency_lock.py --check
git diff --check
```

## Fixtures og privatliv

Tests læser ikke brugerens AppData eller repositoryets tidligere `/config`. Konfiguration, snapshots og backuptræer oprettes i midlertidige mapper. Konto-, profil-, bruger- og fantasy-team-identiteter skal være fiktive; offentlige sportsnavne må bruges som parserfixtures.

## Sideeffektfrihed

Import, `resolve_paths()`, cacheindeksering, type-hint-evaluering og almindelig dashboardnavigation må ikke oprette mapper, skrive filer eller starte netværkskald. Kun eksplicitte save-, export-, backup-, restore-, discovery- og fetch-handlinger må have deres respektive sideeffekter.
