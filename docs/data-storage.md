# Datalagring

Mutable, personlige og voksende data ligger i Windows AppData og ikke i repositoryet. `resolve_paths()` beregner stierne uden at oprette mapper; mapper oprettes først af en eksplicit skrive-, eksport-, backup- eller åbningshandling.

## Standardplaceringer

```text
%APPDATA%\Holdet Fantasy Hub\config\
├── accounts.json
├── groups.json
└── hub-settings.json

%LOCALAPPDATA%\Holdet Fantasy Hub\
├── data\
│   ├── snapshots\
│   ├── manifests\
│   ├── group-revisions\
│   ├── game-metadata\
│   └── hall-of-fame\
└── exports\
    ├── players\
    ├── teams\
    └── backups\
```

Der findes ikke implicitte `cache`- eller `logs`-mapper i `AppPaths`.

## Indhold

| Placering | Indhold |
| --- | --- |
| `config/accounts.json` | Offentlige profiler til eksplicit kontoopdagelse |
| `config/groups.json` | Managerspil, grupper, medlemsreferencer og aktive turneringsplaner |
| `config/hub-settings.json` | Watchlists, manageraliaser og Hall of Fame-pointprofil |
| `data/snapshots` | Komplette kanoniske spiller- og teamsnapshots |
| `data/manifests` | Resultater fra eksplicitte gruppe- og managerspilopdateringer |
| `data/group-revisions` | Uforanderlige arkiverede turneringsrevisioner |
| `data/game-metadata` | Schedule, deadlines, format og hentetid pr. spil |
| `data/hall-of-fame` | Frosne, uforanderlige råresultater |
| `exports/players` / `exports/teams` | Afledte TXT-, JSON- og Markdown-dokumenter |
| `exports/backups` | Manuelt oprettede Hub-backups og rollback-ZIP'er |

Manglende nye filer eller mapper behandles som tomme stores. Ældre installationer omskrives ikke ved opstart.

## Konto- og Hub-indstillinger

En manglende `accounts.json` er en tom konfiguration. Konti vedligeholdes under **Data og lager → Gemte konti**. Dette er et fiktivt eksempel:

```json
{
  "accounts": [
    {
      "key": "nordlys-konto",
      "label": "Nordlysmanager",
      "profile_url": "https://www.holdet.dk/da/users/900000000001/teams"
    }
  ]
}
```

`hub-settings.json` bruger schema 1 og opdateres atomisk. Watchlistposter identificerer spil og `entry_id` med legacy-fallback. Manageraliaser samler identitetsnøgler, og pointprofilen er adskilt fra de frosne Hall of Fame-events.

## Filnavne og uforanderlighed

Spillersnapshots bruger `player-round<round>_<MMDD>_<HHmmss>[_N].json`. Spiller- og teameksporter bruger:

```text
data-round<round>_<MMDD>_<HHmmss>[_N].<format>
team-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

`_1`, `_2` osv. bruges ved samme-sekund-kollisioner. Flerformatseksporter deler stem og suffix og publiceres først, når hele sættet kan skrives.

## Schemaer og kompatibilitet

Produktversion `0.1.0` er uafhængig af lokale dataformater. Blandt de aktuelle formater er gruppekonfiguration schema 7, teamsnapshot schema 2, spillersnapshot schema 3 samt schema 1 for:

- `hub-settings.json`;
- filer under `data/game-metadata`;
- Hall of Fame-events under `data/hall-of-fame`;
- refresh-manifester og turneringsrevisioner;
- `backup-manifest.json`.

Ældre kompatible snapshots indlæses uden omskrivning. Manglende rundestatus bliver `unknown` og giver foreløbige beregninger, indtil en manuel genhentning bekræfter `complete`. Defekte eller inkompatible snapshots ignoreres med en synlig advarsel.

## Backup og gendannelse

**Data og lager → Backup og gendannelse** opretter én ZIP med konfiguration, snapshots, manifester, turneringsrevisioner, spilmetadata og Hall of Fame-ledger. Afledte spiller-/teameksporter og gamle backups medtages ikke.

`backup-manifest.json` indeholder schema-version, tidspunkt, filstørrelser og SHA-256 for hver fil. Før restore vises en preview, og hele arkivet valideres. Restore afvises ved:

- absolutte stier, backslashes, `..` eller ukendte rødder;
- links, mapper, dubletter eller ikke-manifesterede filer;
- størrelse- eller checksumfejl;
- ugyldig JSON eller ukendt schema-version.

Efter brugerens bekræftelse oprettes først en rollback-ZIP. Nye `config`- og `data`-træer stages og erstatter de aktive træer samlet. Ved enhver fejl rulles der tilbage; efter succes ryddes Streamlit-caches, og appen genindlæses. En rigtig restore bør kun afprøves mod test-ejede `AppPaths`, ikke direkte mod aktive brugerdata.

Der er ingen automatisk eller cloud-baseret backup. ZIP-backup er altid en manuel handling.

## Datastatus

**Data og lager → Datastatus** viser handlingsorienterede managerspilskort som **Klar**, **Foreløbig**, **Mangler data** eller **Fejl**. Kortet viser seneste relevante runde, hold- og spillerdækning, rundestatus, cachealder, seneste refresh og manglende hold med navn. Tidligere runder ligger i detaljer, og arkiverede spil kan medtages.

Links fører til Rundecenter eller den konkrete kontekst for manuel opdatering. Datastatus har ikke egne opdateringsknapper og starter ingen hentning.

## Overrides

Kilde-CLI'en understøtter `--data-dir`, `--accounts-file`, `--output-dir`, `HOLDET_DATA_DIR`, `HOLDET_CONFIG_DIR` og `HOLDET_OUTPUT_DIR`. Præcedensen er specifikt CLI-argument, samlet CLI-root, specifik miljøvariabel, samlet miljøroot og Windows-standard. En eksportoverride flytter ikke kanonisk cache.

## Privatliv og sletning

Profil-URL'er, bruger-ID'er, hold, grupper, watchlists, aliaser og historiske medlemskaber er personlige data og må ikke committes. Luk dashboardet og slet disse to mapper for at fjerne alle programdata:

```text
%APPDATA%\Holdet Fantasy Hub
%LOCALAPPDATA%\Holdet Fantasy Hub
```

Sletning fjerner også lokale backups, hvis de ikke er kopieret ud af backupmappen.