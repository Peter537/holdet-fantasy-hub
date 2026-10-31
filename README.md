# Holdet Fantasy Hub

![Version 0.1.0](https://img.shields.io/badge/version-0.1.0-ff4b4b)
![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)
![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows&logoColor=white)

Holdet Fantasy Hub er et uofficielt, lokalt Windows-værktøj til offentlige fantasydata fra [Holdet.dk](https://www.holdet.dk/). Følg dine managerspil, ligaer og turneringer, og undersøg spillernes statistik og værdi i samme app. Almindelig navigation bruger kun lokal cache.

> Projektet er ikke udviklet, godkendt eller supporteret af Holdet.dk.

## Funktioner

- **Mine managerspil** viser aktive spil, konkurrencer, datadækning og kommende frister. Hvert spil har hovedfanerne **Overblik**, **Ligaer og turneringer** og **Spillere**.
- **Opdater alle** indhenter data for aktive managerspil efter en pause. Vælg bestemte spil eller opdater én liga eller turnering; manglende og tidligere foreløbige afsluttede spillerunder indhentes, mens bekræftet historik genbruges.
- Liga, schweizersystem, gruppespil + knockout og double elimination med frosne seeds og publicerede modstandere. Korrigerede resultater kan genberegnes uden at omskrive allerede publicerede opgør.
- Spiller- og holdstatistik med spillerdetaljer, sammenligning, historik og intra-runde-ændringer. Avancerede filtre, kolonner og særskilte eksportformater findes ved tabellerne.
- **Download JSON** samler alle spillere, scopets unikke hold, deres rundehistorik og konkurrencerne i én fil. Spillerhistorik pr. runde er et tilvalg, og tabelfiltre begrænser ikke eksporten. **Opdater før download** opdaterer først det samme scope og viser downloadknappen ved det færdige kørselsresultat.
- En lokal kørselsjournal bevarer fremdrift og delresultater. En browsergenindlæsning kan tilslutte sig kørslen igen; stop og genoptagelse er eksplicitte handlinger.
- Scouting og watchlists, statusalarmer, beslutningsanalyse og transferlaboratorium, Managers og sæsoner, kalender samt Rundens historie er seks valgfrie værktøjer. De starter deaktiveret og vælges under **Indstillinger**. Eksperimentelle fixture- og Monte Carlo-modeller kræver yderligere tilvalg.
- Dataadministration med import, integritetskontrol, lageroverblik, ZIP-backup, sikker gendannelse og et lokalt read-only API til Excel, Power BI og egne scripts.

## Kom hurtigt i gang

Projektet kræver Windows og Python 3.14. Opret et separat miljø, og installer låsens præcise afhængigheder fra repositoryets rod i PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe .\scripts\install_locked_dependencies.py
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation --no-index -e .
```

`pylock.toml` låser hele Windows/Python 3.14-miljøet, inklusive Parquet- og
UI-testafhængigheder, til kontrollerede PyPI-wheels med SHA-256. Pips
`lock`/`pylock.toml`-understøttelse er fortsat eksperimentel og platformsspecifik;
installationsscriptet validerer derfor låsen og giver pip de eksakte wheel-URL'er
og hashes. Brug samme `.venv`-interpreter til appen og testene. Genberegn kun
låsen ved ændringer i afhængighederne på Windows med Python 3.14. Den låste
installation omfatter også pip, så det samme miljø kan kontrollere og
genberegne låsen:

```powershell
.\.venv\Scripts\python.exe .\scripts\refresh_dependency_lock.py
```

Start appen med:

```powershell
.\start.ps1
```

Launcheren vælger altid projektets `.venv`, også hvis en ældre global
`streamlit`-kommando ligger i din PATH. Den svarer til:

```powershell
.\.venv\Scripts\python.exe -m streamlit run .\website\server.py
```

Åbn [http://localhost:8501](http://localhost:8501). Data hentes kun efter et eksplicit klik på en hente-, opdaterings- eller genopbygningshandling.

## Navigation

Start i **Mine managerspil**, åbn et spil, og vælg **Ligaer og turneringer** eller **Spillere**. **Overblik** samler konkurrencer og datastatus; **Datadetaljer og rundecenter** åbner de mere detaljerede previews og historiske visninger.

Sidebaren prioriterer managerspillene og det åbne spils konkurrencer. **Værktøjer** indeholder selvstændig spiller- og holdstatistik samt de aktiverede globale tilvalg. **Flere funktioner** på managerspillet giver adgang til holdstatistik, historik, administration, spilindstillinger og aktiverede analyse- og alarmværktøjer. **Indstillinger** indeholder værktøjsvalg og indgangen til **Data, import og backup**.

Deaktivering af et værktøj bevarer dets gemte data. Et eksisterende direkte link giver mulighed for **Åbn denne gang** uden at gemme et tilvalg. Arkiverede spil kan læses og eksporteres; hentning og redigering kræver gendannelse af spillet.

Streamlit ejer routingen via `st.navigation`/`st.Page`. Eksisterende `section=round-center`, spillerpaneler og gamle `?view=…`-links bevares. Se [Navigation](docs/navigation.md) for den fulde kompatibilitetskontrakt og [Rundecenter og daglig arbejdsgang](docs/round-center.md) for opdatering efter en pause, afgrænsning, retry og historiske visninger.

## Dokumentation

| Emne | Dokument |
| --- | --- |
| Rundecenter, refresh, afvigelser og Rundens historie | [Rundecenter og daglig arbejdsgang](docs/round-center.md) |
| Arkitektur, dataflow og offentlige API'er | [Arkitektur](docs/architecture.md) |
| Dashboard, deeplinks og CLI | [Klienter](docs/clients.md) |
| Canonical routes og legacy-kompatibilitet | [Navigation](docs/navigation.md) |
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

Personlige konti, profiler, grupper, sæsoner, noter, filtre, alarmer, snapshots, metadata, kørselsjournaler, manager-events, importer, backups, rapporter og eksporter ligger uden for repositoryet under `%APPDATA%\Holdet Fantasy Hub` og `%LOCALAPPDATA%\Holdet Fantasy Hub`. Mapper oprettes først ved en eksplicit skrivehandling. Anonymiserede supportpakker er irreversible og markeret som ikke-gendannelige.

Repositoryet må ikke indeholde virkelige profil-ID'er, fantasy-team-ID'er eller personlige holdnavne. Dokumentation og tests bruger fiktive identiteter.

## Verifikation

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Browser-, parser-canary- og afhængighedskontroller er beskrevet i [Tests](docs/testing.md). Canary-testene og genberegning af låsen, også med `--check`, bruger netværk og er særskilte tilvalg.

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
