# Klienter

Holdet Fantasy Hub har et lokalt Streamlit-dashboard og en kildebaseret CLI. Begge bruger `holdet_lib` og de samme AppData-stier.

## Dashboard

Start fra repositoryets rod:

```powershell
py -3.14 -m streamlit run .\website\app.py
```

Åbn [http://localhost:8501](http://localhost:8501). `.streamlit/config.toml` binder serveren til `127.0.0.1`; standardporten er 8501.

Dashboardets vigtigste områder er:

- **Mine managerspil**: vedvarende spil med grupper, hold og seneste lokale datarunde.
- **Spillerstatistik**: hent, filtrér og eksportér et vilkårligt spil uden at registrere det.
- **Holdstatistik**: åbn cachede hold, find hold på gemte konti eller brug en direkte hold-URL/ID.
- **Arkiverede managerspil**: se arkiverede spil offline og gendan dem fra spillets hovedside.
- **Data og lager**: administrér gemte konti og se de effektive Windows-stier.

Et managerspil indeholder faner til grupper, spillerstatistik, holdstatistik, gruppeadministration og spilindstillinger. Navigering, faneskift, rundeskift og cachevisning udløser ingen hentning. Netværk bruges kun af tydeligt navngivne handlinger som **Hent**, **Find hold** eller **Opdater**.

Læs mere om [spillerstatistik](player-statistics.md), [holdstatistik](team-statistics.md) og [grupper og turneringer](groups-and-tournaments.md).

## Kildebaseret CLI

CLI'en køres direkte fra projektets kildekode. Brug `--help` som autoritativ og opdateret oversigt:

```powershell
py -3.14 .\cli\main.py --help
```

```powershell
py -3.14 .\cli\main.py players --help
```

```powershell
py -3.14 .\cli\main.py teams --help
```

### Spillerstatistik

Hent den aktuelle statistik og opret standardeksporten som TXT:

```powershell
py -3.14 .\cli\main.py players https://www.holdet.dk/da/fantasy/super-manager-fall-2026
```

Hent en historisk runde i flere formater:

```powershell
py -3.14 .\cli\main.py players https://www.holdet.dk/da/fantasy/tour-de-france-2026 --round 7 --format txt --format json --format md
```

Et eksempel med filtrering og kolonnevalg:

```powershell
py -3.14 .\cli\main.py players https://www.holdet.dk/da/fantasy/super-manager-fall-2026 --min-value 5000000 --status disabled=exclude --column name --column team --column value --column status
```

Hver hentning gemmer et komplet kanonisk spillersnapshot. Filtre og kolonnevalg påvirker kun eksporten.

### Fantasyhold

Hent alle matchende hold fra konfigurerede konti:

```powershell
py -3.14 .\cli\main.py teams https://www.holdet.dk/da/fantasy/tour-de-france-2026
```

Brug en direkte, fiktiv fantasy-team-URL uden kontoopdagelse:

```powershell
py -3.14 .\cli\main.py teams https://www.holdet.dk/da/fantasy/tour-de-france-2026/fantasyteams/900000000001
```

Eksportér én historisk runde som Markdown og JSON:

```powershell
py -3.14 .\cli\main.py teams https://www.holdet.dk/da/fantasy/tour-de-france-2026/fantasyteams/900000000001 --round 7 --format md --format json
```

Uden `--round` indeholder eksporten seneste overblik, aktuel opstilling og komplet offentlig historik. Med `--round` kræves et rundesammendrag; en opstilling følger kun med, hvis et snapshot blev gemt præcis i runden.

### Dataplaceringer

Vis de effektive stier:

```powershell
py -3.14 .\cli\main.py data paths
```

Åbn konfigurations-, snapshot- eller eksportmappen i Stifinder:

```powershell
py -3.14 .\cli\main.py data open --config
```

```powershell
py -3.14 .\cli\main.py data open --snapshots
```

```powershell
py -3.14 .\cli\main.py data open --exports
```

Se [Datalagring](data-storage.md) for overrides og forskellen mellem snapshots og eksporter.

## Batchadfærd

Flere URL'er eller teams behandles uafhængigt. En fejl rapporteres for det konkrete input, mens resterende input fortsætter. Kommandoen afslutter med en fejlkode, hvis mindst ét input fejlede, og opretter ikke en delvis eksport for det fejlede input.
