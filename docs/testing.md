# Tests

Pytest er projektets autoritative test runner. Suiten indeholder både unittest-baserede klasser, pytest-funktioner og Streamlit AppTest-scenarier; pytest samler dem alle. Den tidligere discovery-kommando overså de pytest-baserede Hub-tests og må derfor ikke bruges som fuld suite.

## Installation og komplet suite

Opret projektets eget Python 3.14-miljø, og installer website- og testafhængigheder fra repositoryets rod:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe .\scripts\install_locked_dependencies.py
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation --no-index -e .
```

Den ene repositorylås indeholder core-, website-, Parquet-, test- og UI-testgrafen for Windows/Python 3.14. Installationsscriptet validerer den eksisterende lås og installerer dens eksakte wheel-URL'er med SHA-256-kontrol. Installation kræver ikke, at låsen genberegnes. Brug samme `.venv`-interpreter til appen og testene.

Kør hele suiten:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

De almindelige tests må ikke kontakte Holdet.dk. HTTP-svar kommer fra lokale fixtures eller injicerede fetch-funktioner, og filesystemtests bruger test-ejede midlertidige `AppPaths`.

## Afhængighedslåsen

Genberegn kun `pylock.toml`, når afhængighederne skal ændres. Scriptet kræver Windows og Python 3.14. Den låste installation omfatter også en kontrolleret pip-version med `pip lock`, så `.venv`-miljøet kan bruges til både kontrol og genberegning.

```powershell
.\.venv\Scripts\python.exe .\scripts\refresh_dependency_lock.py
.\.venv\Scripts\python.exe .\scripts\refresh_dependency_lock.py --check
```

Kontrollen bruger resolver, wheelmetadata og OSV. Både genberegning og `--check` kræver netværk og sender offentlige pakkenavne og versioner; de sender ingen repositoryfiler eller private data. De er særskilte kontroller ud over den almindelige offline-testsuite.

## Parser-canary

Den markerede canary-suite er opt-in og er den eneste test, som kontakter offentlige Holdet-endpoints:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/parser_canary -q --run-parser-canary
```

Uden flaget skippes canary-testene, og standardsuiten er helt offline. Canarien kalder `HoldetClient.fetch_players` for de syv aktuelle spil: to fodboldspil (Superliga og Premier League), to cykelspil, ét Formel 1-spil, ét golfspil og Football Fantasy 2026 (`nfl`). Den validerer format, enhed, positiv runde, ikke-tomme entries, obligatoriske spillerfelter og kontrakten for `popularity`, `popularityChange`, `trend`, `index`, `stats` og `totalStats`. I preseason-payloads skal hvert af de fire numeriske scoutingfelter mappe `null` til `None` og et endeligt tal til den tilsvarende numeriske værdi. Rå `stats`/`totalStats` skal findes med en kendt mappe-/listeform; en legitim tom mappe er tilladt, mens ikke-tomme rå værdier skal overleve parseren. Drift i de valgfrie felter rapporteres som kildedrift; produktionsparseren forbliver fail-closed og kompatibel med de obligatoriske felter.

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
| Refresh og manifest | Cache-only preview, alt/forældet/retry-planer, holddeduplikering, fremdrift pr. kilde, cachefallback og læsning af manifest schema 1–3 |
| Indhentning efter en pause | Metadata og seneste spillerdata før historiske runder, indhentning af manglende/foreløbige data, genbrug af komplet historik, nul historiske alarmer og konkurrencespecifik retry |
| Worker og kørselsjournal | Én worker pr. datarod, tilslutning efter genindlæsning, eksplicit stop/genoptagelse, usikre afbrudte kildeoperationer og bevaring af færdige manifestreferencer |
| Scope og samlet eksport | Spillets konkurrencer plus valgt eget hold, faste konkurrencemedlemmer, deduplikerede hold og én JSON med alle spillere, korrigerede runder, kildeoplysninger og valgfri spillerhistorik |
| Valgfrie værktøjer | Seks tilvalg starter deaktiveret, settings schema 1–4 læses uden writes, schema 5 bevarer data ved tilvalg/fravalg, sessionadgang og eksplicit permanent aktivering |
| Historik | Huller, seneste snapshot pr. runde og omvendte rangakser på spil-, gruppe- og holdniveau |
| Managers og sæsoner | Identitetsgraf, stabilt ID ved merge/rename/unmerge, legacy-remapping uden writes, bedste hold, locale- og puljeisoleret Elo, awards, streaks, historier, H2H-aggregater, sæsonredigering og pointprofil |
| Eventledger og kalender | Revisioner, legacy-events, manglende metadata, cache-only events og nul navigation-writes |
| Dataportabilitet | DataPackage, Unicode, rå taltyper, CSV-injection, XLSX-ark, valgfri Parquet, rapportescaping, forklarlige historiefakta, sikre dual links og anonymiseringsprofiler |
| Lagring og backup | AppData, atomiske writes, HubSettings 1–4 → 5, spiller-snapshot 1–3 → 4, inbox 1 → 2 og GameMetadata 1 → 2 uden startup-write, parringer schema 1–2, kørselsjournaler, integritetsindeks, importklassifikation, arkivgrænser, SHA-256, path traversal, preview, staging og rollback |
| Lokalt API | Catalog, filtre, pagination, CSV/JSON-paritet, ETag/304, Host/loopback, sikre headere, nul writes og nul netværk |
| Turnering | Liga, Swiss, gruppespil + knockout, double elimination, fuld Swiss-afslutning, custom byepoint, Buchholz, seedning, tie-breakers, bronzekamp, publicerede knockoutopgør og konflikter efter resultatkorrektioner |
| Dashboard | Tre hovedfaner, valgfrie værktøjer, spillerpaneler, filtre, statusprovenance, badges, kompatibilitetsruter og nul netværk/writes ved navigation |
| Native navigation | Canonical path-matrix, legacy-redirects, bevarede core- og værktøjslinks, query-kontekst, ugyldige entiteter, arkiveret read-only og filbaseret `AppTest.switch_page` |
| Reruns og state | Inaktive faner med spies, fragmentgrænser, Apply/Reset, fulde reruns efter writes, stabile dataframe-keys og scroll/sortering gennem fragment-rerun |
| Dokumentation og API | Links, Mermaid-hegn, kommandoer, AppData-stier, `holdet_lib.__all__` og evaluerbare type hints |

## Streamlit AppTest

AppTest åbner hovedroutes og query-parametre uden en virkelig server. Navigationstests kontrollerer **Overblik**, **Ligaer og turneringer** og **Spillere**, mens eksisterende links til historik, holdstatistik, administration, indstillinger, alarmer og analyse fortsat åbner den samme funktion. Inaktive hovedfaner og valgfrie værktøjer må ikke rendere deres datavisninger. Rundecenterets detaljer åbnes eksplicit i testene, ligesom de gør i brugerfladen.

Tests for valgfrie værktøjer skelner mellem et deaktiveret direkte link, **Åbn denne gang** og **Aktivér værktøj**. Sessionadgang må ikke oprette en indstillingsfil og gælder ikke for en ny AppTest-session. Permanent aktivering kræver et eksplicit klik. Et arkiveret managerspil bevarer sin synlige konkurrencenavigation, men kan ikke opdatere data eller gemme et tilvalg fra den skrivebeskyttede kontekst.

Spillerpaneler og den globale Scouting-side bevarer deres query-kontekst. Alarmtests kontrollerer spilafgrænsning, ulæste badges og oprydning alene i det valgte spil. Rundecenterets preview, time machine og sessionfiltre samt Scouting-, Analyse-, spiller-, alarm-, manager-, kalender- og almindelig navigation og transfersimulation må hverken kalde netværk eller skrive persistent data.

Den afgrænsede konkurrenceopdatering testes gennem den faktiske UI-handling, coordinator, catch-up-service og manifest. Testen injicerer en lokal fake client og en deterministisk executor, så ingen eksterne kald er mulige. Den kontrollerer den gemte slutrunde og mester samt den opdaterede visning og H2H. En hurtig kørsel, der er færdig før første statuspoll, skal også rydde gammel snapshotcache. De særskilte worker-tests kontrollerer asynkron drift, låsning, genoptagelse og annullering.

Den kanoniske funktionskontrakt står i [Rundecenter og daglig arbejdsgang](round-center.md); eksportens kontrakt står i [Dataportabilitet](data-portability.md).

En fokuseret kontrol af de ændrede arbejdsgange kan køres med:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_navigation.py tests/test_website.py tests/test_tournament_website.py tests/test_catch_up.py tests/test_refresh_runs.py tests/test_refresh_service.py tests/test_refresh_recovery.py tests/test_scopes.py tests/test_scope_exports.py tests/test_published_matches.py tests/test_feature_preferences.py tests/test_optional_tool_behavior.py tests/test_round_history_ui.py tests/test_completed_scope_export.py -q
```

## Lokal UI-regression og accessibility

Den fulde lås installerer UI-testpakkerne. Installér derefter kun den af
Playwright-versionen fastlagte Chromium-build én gang:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Opret først de lokale baselines. PNG-filerne er maskinspecifikke testdata og er
ignoreret af Git; de skal derfor genereres på hver udviklingsmaskine og må ikke
committes:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui -q --run-ui --browser chromium --update-ui-snapshots
```

Kør derefter den opt-in Windows/Chromium-suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui -q --run-ui --browser chromium
```

En session-fixture starter `website/server.py` på en tilfældig fri loopback-port med et isoleret, deterministisk `HOLDET_DATA_DIR`. Den opretter ti aktive managerspil, ét arkiveret spil, en liga med otte hold, en turnering med fire hold og en særskilt historik på 20 runder. Suiten bruger reduceret bevægelse og indeholder 15 baselines:

- Forsiden ved 375, 768, 1280 og 1920 px.
- Managerspillets overblik, en liga, en turnering, historikgrafen og Indstillinger ved 1920 × 910 px.
- Det udvidede Rundecenter ved 375 og 1280 px, efter **Datadetaljer og rundecenter** er åbnet.
- En tæt spillerliste og spillerdetaljen ved 1280 px.
- Scouting ved 375 og 1280 px. Fixturedata aktiverer Scouting eksplicit; standardtilstanden med deaktiverede tilvalg kontrolleres særskilt i AppTest.

Pillow-sammenligningen tillader højst 0,1 % ændrede pixels efter en per-kanal-tolerance på 10. Ved fejl gemmes `actual` og `diff` i den ignorerede mappe `tests/ui/artifacts/`. Manglende baselines fejler med bootstrap-kommandoen ovenfor, og eksisterende lokale baselines må kun erstattes eksplicit:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ui -q --run-ui --browser chromium --update-ui-snapshots
```

Accessibility-smoken kontrollerer én H1, overskriftshierarki, navngivne knapper, tab/tabpanel-state, tastatur og synligt fokus, 200 % zoom/reflow, dokument-overflow samt datatabeller ved hvert scoutingplot. En browsertest kontrollerer desuden, at spillerlistens sorteringsvalg og faktiske dataframe-scrollposition bevares gennem et fragment-rerun.

En særskilt browsertest henter de faktiske JSON-filer fra både managerspil og turnering. Den filtrerer spillerlisten til én af 60 spillere og kontrollerer, at eksporten stadig har hele kataloget. Spileksporten skal indeholde otte hold og begge konkurrencer, mens turneringseksporten kun må have sine fire medlemmer og den valgte turnering. Filhashes før og efter samt registrering af browser- og serverkald kontrollerer, at download hverken skriver til de kanoniske data eller bruger eksternt netværk.

## Afsluttende accept

Kør standardsuite, de to opt-in-suiter efter behov og miljø-/diffkontrol:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests/ui -q --run-ui --browser chromium
.\.venv\Scripts\python.exe -m pytest tests/parser_canary -q --run-parser-canary
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe .\scripts\refresh_dependency_lock.py --check
git diff --check
```

## Fixtures og privatliv

Tests læser ikke brugerens AppData eller repositoryets tidligere `/config`. Konfiguration, snapshots og backuptræer oprettes i midlertidige mapper. Konto-, profil-, bruger- og fantasy-team-identiteter skal være fiktive; offentlige sportsnavne må bruges som parserfixtures.

## Sideeffektfrihed

Import, `resolve_paths()`, cacheindeksering, type-hint-evaluering og almindelig dashboardnavigation må ikke oprette mapper, skrive filer eller starte netværkskald. Kun eksplicitte save-, export-, backup-, restore-, discovery- og fetch-handlinger må have deres respektive sideeffekter.

Den målrettede browserkontrol læser også de faktisk renderede SVG-etiketter: x-aksen må kun vise hele runder i både korte og lange forløb, legendens holdnavne skal være synlige, og værdiernes akseenheder skal stemme med data. En turnerings historiske slutrunde skal fjerne senere observationer fra grafen. Tastaturadgang kontrolleres gennem **Åbn spil**, mens tabelknapperne kontrolleres med mus både før og efter sortering. Datastatus for foreløbige kampe må ikke skubbe turneringens faner under første skærmbillede ved 1920 × 910 px.
