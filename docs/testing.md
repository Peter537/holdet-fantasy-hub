# Tests

Pytest er projektets autoritative test runner. Suiten indeholder både unittest-baserede klasser, pytest-funktioner og Streamlit AppTest-scenarier; pytest samler dem alle. Den tidligere discovery-kommando overså de pytest-baserede Hub-tests og må derfor ikke bruges som fuld suite.

## Installation og komplet suite

Installer website- og testafhængigheder fra repositoryets rod:

```powershell
py -3.14 -m pip install -e ".[website,test]"
```

Kør derefter hele suiten:

```powershell
py -3.14 -m pytest tests -q
```

De almindelige tests må ikke kontakte Holdet.dk. HTTP-svar kommer fra lokale fixtures eller injicerede fetch-funktioner, og filesystemtests bruger test-ejede midlertidige `AppPaths`.

## Live-smoke-tests

Den ene eksisterende live-test er opt-in og kontakter offentlige Holdet-endpoints:

```powershell
$env:HOLDET_LIVE_TESTS="1"; py -3.14 -m pytest tests -q
```

Fjern miljøvariablen igen:

```powershell
Remove-Item Env:HOLDET_LIVE_TESTS
```

Live-testen afhænger ikke af private profiler eller fantasyhold og antager ikke ustabile eksakte spillerantal.

## Testområder

| Område | Eksempler på dækning |
| --- | --- |
| URL, parsere og HTTP | Normalisering, Flight-dekodning, payloadvalidering, retries, proxy og redaction |
| Spillere | Formater, enheder, filtre, watchlist-identitet, 2–5 sammenligninger og round-aware diffing |
| Hold | Kontoopdagelse, roster, historik, rang, gruppeplacering og ændringer |
| Transfer | Fire regelprofiler, gebyr, kontrakter, formation, klubgrænser, kaptajnregler og `final`/`preliminary`/`unverified` |
| Beslutningsanalyse | Formhuller, stabilitet, nul/negativ vækst, kaptajnmismatch, 0/0,5/1 % rente og gebyr, afrunding, transferhuller, kontrafaktisk sum og bankhitrater |
| Idealhold og model | Brute-force-paritet, tie-break, infeasible, timeout/bound, cirka 400 kandidater, deterministisk seed, fælles-spiller-annullering, intervaller, dækning og walk-forward-backtest |
| Gruppe og alarmer | Ledersammenligning, eksponeringsnævnere, manglende hold, alarmtransitioner, deduplikering, læst/afvist og separat spillerfallback |
| Historik | Huller, seneste snapshot pr. runde og omvendte rangakser på spil-, gruppe- og holdniveau |
| Managers og sæsoner | Identitetsgraf, stabilt ID ved merge/rename/unmerge, legacy-remapping uden writes, bedste hold, locale- og puljeisoleret Elo, awards, streaks, historier, H2H-aggregater, sæsonredigering og pointprofil |
| Eventledger og kalender | Revisioner, legacy-events, manglende metadata, cache-only events og nul navigation-writes |
| Lagring og backup | AppData, atomiske writes, HubSettings 1/2 → 3 og GameMetadata 1 → 2 uden startup-write, alarmindbakke, SHA-256, path traversal, preview, staging og rollback |
| Turnering | Liga, Swiss, gruppespil + knockout, double elimination, fuld Swiss-afslutning, custom byepoint, Buchholz, seedning, tie-breakers, bronzekamp, kontekstvalidering og konflikter for frosne parringer |
| Dashboard | Managers-navigation, Analyse-paneler, spilfiltrerede alarmfaner og badges, spiller- og kompatibilitetsruter, noter/tags, standardhold, filterprofiler, opt-in, historier, turneringsguide, deeplinks og nul netværk/writes |
| Dokumentation og API | Links, Mermaid-hegn, kommandoer, AppData-stier, `holdet_lib.__all__` og evaluerbare type hints |

## Streamlit AppTest

AppTest åbner hovedroutes og query-parametre uden en virkelig server. Tests beviser blandt andet, at Statusalarmer ikke er en global sidebardestination, at hvert managerspil har en spilfiltreret alarmfane med unread-badges, at legacy Hall of Fame-routen viderestiller, og at Data og lager indeholder Datastatus og Backup og gendannelse. Analyse-, spiller-, alarm-, manager-, kalender- og almindelig navigation samt transfersimulation må hverken kalde netværk eller skrive persistent data.

Manuel browser-QA køres ved 375, 768, 1280 og 1920 px, ved 200 % tekstzoom, med tastaturnavigation og reduceret bevægelse. Lange danske tekster og store tabeller kontrolleres, og konsollen må ikke indeholde empty-chart- eller Vega-advarsler.

## Fixtures og privatliv

Tests læser ikke brugerens AppData eller repositoryets tidligere `/config`. Konfiguration, snapshots og backuptræer oprettes i midlertidige mapper. Konto-, profil-, bruger- og fantasy-team-identiteter skal være fiktive; offentlige sportsnavne må bruges som parserfixtures.

## Sideeffektfrihed

Import, `resolve_paths()`, cacheindeksering, type-hint-evaluering og almindelig dashboardnavigation må ikke oprette mapper, skrive filer eller starte netværkskald. Kun eksplicitte save-, export-, backup-, restore-, discovery- og fetch-handlinger må have deres respektive sideeffekter.
