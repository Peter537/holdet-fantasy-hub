# Klienter

Holdet Fantasy Hub har et lokalt Streamlit-dashboard og en kildebaseret CLI. Begge bruger `holdet_lib` og de samme AppData-stier.

## Dashboard

Start fra repositoryets rod:

```powershell
py -3.14 -m streamlit run .\website\server.py
```

Åbn [http://localhost:8501](http://localhost:8501). `.streamlit/config.toml` binder serveren til `127.0.0.1`; standardporten er 8501.

### Sidebar

Destinationerne står i denne rækkefølge:

1. **Mine managerspil** – kun managerspilskort og overordnede handlinger; Rundecenter vises ikke her.
2. **Tilføj managerspil**.
3. **Spillerstatistik** og **Holdstatistik** – selvstændige paneler med én nødvendig managerspilvælger.
4. Aktive managerspil med deres grupper. Et spilnavn viser antal ulæste statusalarmer, når tallet er større end nul.
5. **Arkiverede managerspil**.
6. **Managers** – Rangliste, Medaljer og rekorder, Sammenlign, Sæsoner og Identiteter.
7. **Kalender** – cachebaserede fantasyopgør og manglende tidspunkter.
8. **Data og lager** med områdevælgerne Overblik, Eksport og rapporter, Import og backup, Integritet og oprydning, Lokalt API samt Konti og placeringer.

Der findes ingen global Værktøjer-sektion. De tidligere globale views `transfer`, `compare`, `history` og `changes` er fjernet og viser den kontrollerede side **Siden findes ikke**. Data-deeplinks med `accounts`, `quality`, `locations` og `backup` mappes til de nye områder.

Den tidligere `view=hall-of-fame` viderestiller til `/managers`. Managers og Kalender er globale sider og starter ingen hentning. Kalenderen kan filtreres på manager, managerspil, gruppe/turnering og dato; de fulde filtre ligger i et panel, og aktive filtre vises som chips i en sticky handlingslinje.

Managers viser Hall of Fame-point og Elo side om side. Identiteter kan samles og ophæves manuelt, H2H har officielle kampe og fælles grupperunder som separate spor, og sæsoner sammensættes af eksisterende konkurrencer.

### Managerspil og kontekst

Et managerspil åbner på **Rundecenter** og har ni lazy-loadede faner:

- Rundecenter
- Grupper
- Spillerstatistik
- Statusalarmer
- Holdstatistik
- Historik
- Analyse
- Administration
- Indstillinger

Rundecenter samler næste handling, handelsvindue, cache-only opdaterings-preview, datakildestatus, afvigelser, rundesammenligning, time machine, gruppematrix og Rundens historie. Statuskort og berørte hold linker til den relevante visning; intet hentes, før brugeren bekræfter en opdatering. Den fulde kontrakt findes i [Rundecenter og daglig arbejdsgang](round-center.md).

Analyse har query-parametret segmentvalg mellem Beslutninger, Gruppe, Idealhold og Eksperimentel. Kun det aktive segment beregnes. Standardhold gemmes pr. spil, mens et midlertidigt valg ikke ændrer standarden. Eksperimentelle fixtures og Monte Carlo kræver et gemt opt-in og viser en synlig modeladvarsel.

Spillerstatistik genbruger managerspil og runde til Spillerliste, Sammenligning og watchlist samt Ændringer. **Statusalarmer** viser kun det aktuelle managerspils hændelser, antal watchlistspillere og en genvej til watchlist-editoren. Ulæste alarmer tælles både på fanen og ved managerspillets navn. Holdstatistik vælger hold og runde én gang og genbruger dem til Overblik, Holdopstilling, Transferlaboratorium, Historik, Ændringer og Eksport. En gruppe, som allerede har valgt spil eller hold, åbner de samme paneler uden overflødige vælgere.

### Deeplinks og session state

Pathen vælger hoveddestinationen; query-parametrene bærer kun kontekst:

| Parameter | Betydning |
| --- | --- |
| `section` | Managerspillets hovedfane eller aktivt Data og lager-område |
| `analysis` | Aktivt Analyse-panel: `decisions`, `group`, `ideal` eller `experimental` |
| `panel` | Underfane i spiller-, hold-, gruppe- eller datavisningen |
| `round` | Valgt runde |
| `team` | Valgt hold |
| `group` | Valgt gruppe |
| `player` | Stabil spilleridentitet på den selvstændige spillerdetaljerute |
| `manager` / `opponent` | De to managerprofiler i Sammenlign |
| `season` | Valgt sæsonmesterskab |
| `date` | Kalenderens valgte dato |

Eksempel: `/game?locale=da&game=tour-de-france-2026&section=players`. Valg i Streamlit-state navngives efter route, spil, gruppe, hold og komponent, så de ikke lækker mellem managerspil. Faner læses først, når de vises. Se [Navigation](navigation.md) for alle paths og legacy-redirects.

### Offline-first

Navigation, faneskift, rundeskift, grafer, Analyse, spillerdetaljer, spilafgrænset alarmfiltrering, sammenligning, ændringsvisning, transfersimulation og Rundecenterets time machine læser kun kompatible snapshots. Netværk bruges kun af tydeligt navngivne og bekræftede handlinger som **Hent**, **Find hold**, **Opdater** eller **Prøv igen**. Alarmer skriver kun ved **Markér som læst**, **Afvis** eller **Ryd afviste alarmer**. Simuleringer og Rundecenterets afvigelsesfiltre lever i `st.session_state`; kun eksplicit gem af noter, filtre, standardhold eller opt-in skriver konfiguration.

Den kanoniske alarmroute er `/game?locale=…&game=…&section=alerts`. `/alerts?locale=…&game=…` er den skjulte, spilfiltrerede kompatibilitetsside for watchlists fra den selvstændige Spillerstatistik. Hvis spillet er gemt som managerspil, viderestilles den til den kanoniske fane.

Læs mere om [Spillerstatistik](player-statistics.md), [Holdstatistik](team-statistics.md), [Analyse- og beslutningscenter](decision-analysis.md), [Managers og sæsoner](managers-and-seasons.md), [Grupper og turneringer](groups-and-tournaments.md) og [Datalagring](data-storage.md).

### Sikker genstart

Hvis port 8501 indeholder en gammel Streamlit-session, kontrollér både lytteren og kommandolinjen:

```powershell
netstat -ano | Select-String ":8501"
```

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*streamlit*website\server.py*" } | Select-Object ProcessId, ParentProcessId, CommandLine
```

Stop kun de bekræftede app-processer og deres konkrete launcherproces, bekræft at porten er fri, og start én ny instans med den normale kommando. Stop aldrig alle `python.exe`- eller `py.exe`-processer. Brug eventuelt `/_stcore/health` og en ny browsernavigation til at undgå at genbruge en gammel WebSocket-session.

## Kildebaseret CLI

CLI'en køres direkte fra projektets kildekode. Brug `--help` som autoritativ oversigt:

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

```powershell
py -3.14 .\cli\main.py players https://www.holdet.dk/da/fantasy/super-manager-fall-2026
```

```powershell
py -3.14 .\cli\main.py players https://www.holdet.dk/da/fantasy/tour-de-france-2026 --round 7 --format csv --format xlsx
```

Filtre og kolonnevalg påvirker kun eksporten; hver hentning gemmer det komplette kanoniske spillersnapshot.

### Fantasyhold

```powershell
py -3.14 .\cli\main.py teams https://www.holdet.dk/da/fantasy/tour-de-france-2026
```

```powershell
py -3.14 .\cli\main.py teams https://www.holdet.dk/da/fantasy/tour-de-france-2026/fantasyteams/900000000001 --round 7 --format csv --format xlsx
```

Uden `--round` indeholder eksporten seneste overblik, aktuel opstilling og komplet offentlig historik. Med `--round` kræves et rundesammendrag; opstillingen følger kun med, hvis et snapshot blev gemt præcis i runden.

### Dataplaceringer

```powershell
py -3.14 .\cli\main.py data paths
```

```powershell
py -3.14 .\cli\main.py data open --config
```

```powershell
py -3.14 .\cli\main.py data open --snapshots
```

```powershell
py -3.14 .\cli\main.py data open --exports
```

Se [Datalagring](data-storage.md) for overrides og forskellen mellem kanoniske snapshots og afledte eksporter.

## Batchadfærd

Flere URL'er eller teams behandles uafhængigt. En fejl rapporteres for det konkrete input, mens resten fortsætter. Kommandoen afslutter med en fejlkode, hvis mindst ét input fejlede, og opretter ikke en delvis eksport for det fejlede input.
