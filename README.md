# Holdet Fantasy Hub

![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-ff4b4b)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)

Holdet Fantasy Hub er et uofficielt, lokalt Windows-værktøj til offentlige fantasydata fra [Holdet.dk](https://www.holdet.dk/). Dashboardet samler managerspil, spillere, hold, grupper, managerkarrierer, sæsoner og fleksible turneringer. Almindelig navigation bruger kun lokal cache.

> Projektet er ikke udviklet, godkendt eller supporteret af Holdet.dk.

## Funktioner

- Rundecenter med deadline, datastatus, rangbevægelser, runde-awards og en deterministisk Rundens historie.
- Spiller- og holdstatistik, watchlist, noter/tags, gemte filtre, spillerdetaljer, sammenligning, historik, ændringer, transferlaboratorium og eksport i TXT, JSON, Markdown, CSV, XLSX og valgfri Parquet.
- Cachebaseret analyse- og beslutningscenter med form, stabilitet, kaptajn, bank, transferregnskab, gruppeswing, eksponering og regelverificeret idealhold.
- Opt-in-modeller for fixturecache og Monte Carlo, tydeligt mærket som eksperimentelle og aldrig som facit.
- Managerprofiler på tværs af spil og hold, Elo-rating, medaljer, rekorder, streaks og H2H.
- Manuelt sammensatte sæsoner, som genbruger den globale redigerbare pointprofil.
- Liga, schweizersystem, gruppespil + knockout og double elimination med frosne seeds og publicerede parringer.
- Global cache-only kalender og validerede officielle Holdet-links.
- Selvstændige manager- og sæsonrapporter, anonymiserede supportpakker, preview-baseret import, integritetsindeks, lagerinventar og manuel retention.
- Loopback-only read-only API til Excel, Power BI og egne scripts samt valideret ZIP-backup og rollback-sikker gendannelse.

## Kom hurtigt i gang

Projektet kræver Windows og Python 3.14. Kør fra repositoryets rod i PowerShell:

```powershell
py -3.14 -m pip install -e ".[website,test]"
```

```powershell
py -3.14 -m streamlit run .\website\server.py
```

Åbn [http://localhost:8501](http://localhost:8501). Data hentes kun efter et eksplicit klik på en hente-, opdaterings- eller genopbygningshandling.

## Navigation

Sidebaren indeholder **Mine managerspil**, statistikvisninger, aktive og arkiverede managerspil, **Managers**, **Kalender** og **Data og lager**. Ulæste statusalarmer vises ved det relevante managerspil og i managerspillets egen **Statusalarmer**-fane. Den gamle route `?view=hall-of-fame` viderestiller til Managers.

Managers har fanerne Rangliste, Medaljer og rekorder, Sammenlign, Sæsoner og Identiteter. Kalenderen kan filtreres på managerspil og gruppe eller turnering. Gruppe-, hold- og managerkort viser officielle links, når de findes i cache eller konfiguration.

## Dokumentation

| Emne | Dokument |
| --- | --- |
| Arkitektur, dataflow og offentlige API'er | [Arkitektur](docs/architecture.md) |
| Dashboard, deeplinks og CLI | [Klienter](docs/clients.md) |
| Manageridentitet, Elo, awards, H2H og sæsoner | [Managers og sæsoner](docs/managers-and-seasons.md) |
| Grupper og turneringsformater | [Grupper og turneringer](docs/groups-and-tournaments.md) |
| Hentning fra Holdet.dk | [Datahentning](docs/data-retrieval.md) |
| AppData, skemaer og backup | [Datalagring](docs/data-storage.md) |
| Eksport, rapporter, anonymisering, import og retention | [Dataportabilitet](docs/data-portability.md) |
| Read-only API, datasæt og Excel/Power BI | [Lokalt API](docs/local-api.md) |
| Spillerstatistik | [Spillerstatistik](docs/player-statistics.md) |
| Holdstatistik | [Holdstatistik](docs/team-statistics.md) |
| Analyse, formler, provenance og modelgates | [Analyse- og beslutningscenter](docs/decision-analysis.md) |
| Teststrategi og acceptkommandoer | [Tests](docs/testing.md) |

## Lokal data og privatliv

Personlige konti, profiler, grupper, sæsoner, noter, filtre, alarmer, snapshots, metadata, manager-events, importer, backups, rapporter og eksporter ligger uden for repositoryet under `%APPDATA%\Holdet Fantasy Hub` og `%LOCALAPPDATA%\Holdet Fantasy Hub`. Mapper oprettes først ved en eksplicit skrivehandling. Anonymiserede supportpakker er irreversible og markeret som ikke-gendannelige.

Repositoryet må ikke indeholde virkelige profil-ID'er, fantasy-team-ID'er eller personlige holdnavne. Dokumentation og tests bruger fiktive identiteter.

## Verifikation

```powershell
py -3.14 -m pytest tests -q
```

```powershell
git diff --check
```

Live-kontroller er opt-in og beskrevet i [Tests](docs/testing.md).

## Fejlfinding på port 8501

Identificér altid den konkrete appproces, før den stoppes:

```powershell
netstat -ano | Select-String ":8501"
```

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*streamlit*website\server.py*" } | Select-Object ProcessId, ParentProcessId, CommandLine
```

Et sundhedstjek kan køres med:

```powershell
Invoke-RestMethod http://127.0.0.1:8501/api/v1/health
```

## Status

Versionen er `0.1.0`. Projektet er Windows-only og under aktiv udvikling. Inkompatible nødvendige Holdet-payloadfelter giver en tydelig fejl frem for et delvist snapshot.
