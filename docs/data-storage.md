# Datalagring

Mutable, personlige og voksende data ligger i Windows AppData og ikke i repositoryet. `resolve_paths()` beregner stierne uden at oprette mapper; mapper oprettes først af en eksplicit skrive-, eksport-, backup- eller åbningshandling.

## Standardplaceringer

```text
%APPDATA%\Holdet Fantasy Hub\config\
├── accounts.json
├── analysis-inbox.json
├── groups.json
├── hub-settings.json
└── seasons.json

%LOCALAPPDATA%\Holdet Fantasy Hub\
├── data\
│   ├── snapshots\
│   ├── manifests\
│   ├── refresh-runs\
│   ├── group-revisions\
│   ├── fixtures\
│   ├── game-metadata\
│   ├── hall-of-fame\
│   ├── imports\
│   ├── tournament-pairings\
│   └── integrity-index.json
└── exports\
    ├── players\
    ├── teams\
    ├── reports\
    ├── archives\
    └── backups\
```

Der findes ikke implicitte `cache`- eller `logs`-mapper i `AppPaths`.

## Indhold

| Placering | Indhold |
| --- | --- |
| `config/accounts.json` | Offentlige profiler til eksplicit kontoopdagelse |
| `config/analysis-inbox.json` | Deduplikerede watchlistregel-hændelser med snapshotsovergang, læst- og afvisttidspunkt |
| `config/groups.json` | Managerspil, grupper, officielle links og turneringsdefinitioner |
| `config/hub-settings.json` | Watchlists med regler/begrundelser, beregnede spillerkolonner, managerprofiler, annotationer, filterprofiler, standardhold, model-opt-in og global pointprofil |
| `config/seasons.json` | Manuelle sæsondefinitioner og arkivstatus |
| `data/snapshots` | Komplette kanoniske spiller- og teamsnapshots |
| `data/manifests` | Uforanderlige `RefreshManifest`-resultater pr. datakilde fra eksplicitte gruppe- og managerspilopdateringer |
| `data/refresh-runs` | Lokale journaler for samlede opdateringer, genoptagelse og eventuelle ventende manifestskrivninger |
| `data/group-revisions` | Uforanderlige arkiverede turneringsrevisioner |
| `data/game-metadata` | Schedule, deadlines, format og hentetid pr. spil |
| `data/fixtures` | Eksplicit cachede, offentligt verificerede fixtures og kildeprovenance |
| `data/hall-of-fame` | Append-only manager-events og legacy Hall of Fame-events |
| `data/imports` | Read-only legacy-JSON og ufortolkede TXT/Markdown/CSV-filer |
| `data/integrity-index.json` | Afledt schema-1-indeks med metadata og SHA-256; aldrig sandhedskilden |
| `data/tournament-pairings` | Publicerede fixtures/parringer pr. turneringsrevision |
| `exports/players` / `exports/teams` | Afledte TXT-, JSON-, Markdown-, CSV-, XLSX- og eventuelle Parquet-dokumenter |
| `exports/reports` | Selvstændige managerspil- og sæsonrapporter i HTML |
| `exports/archives` | Checksumvaliderede mellemversionsarkiver med oprindelige relative stier |
| `exports/backups` | Manuelt oprettede Hub-backups og rollback-ZIP'er |

Manglende nye filer eller mapper behandles som tomme stores. Ældre installationer omskrives ikke ved opstart. En korrupt arkiveret turneringsrevision springes over med en afgrænset advarsel i Hubben, så den aktive gruppe stadig kan vises; den strikte store-læsning for backup og validering afviser fortsat filen.

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

`hub-settings.json` schema 4 opdateres atomisk. Watchlistposter identificerer spil og `entry_id` med legacy-fallback og kan have flere standardbegrundelser, 280 tegn fritekst og højst otte regler. Op til 20 sikre `ComputedPlayerColumn`-definitioner gemmes pr. spil. `ManagerProfile` samler identitetsnøgler, manuel linkproveniens og profil-URL'er, mens pointprofilen er adskilt fra manager-eventledgeren. Schema 1–3 migreres kun i hukommelsen. Ældre watchlistposter får statusændring som standardregel; filen skrives først som schema 4 ved en eksplicit save.

Spillerannotationer har højst 2.000 tegn og 12 normaliserede tags á 24 tegn. Gemte filterprofilnavne er unikke pr. spil og indeholder en versioneret `PlayerStatisticsQuery`, inklusive valgte beregnede kolonne-ID'er. `analysis-inbox.json` schema 2 er et separat atomisk store. Schema 1 dual-reades; nye hændelser fryser regel-ID, begge snapshottidspunkter og overgang i eventidentiteten.

## Filnavne og uforanderlighed

Spillersnapshots bruger `player-round<round>_<MMDD>_<HHmmss>[_N].json`. Spiller- og teameksporter bruger:

```text
data-round<round>_<MMDD>_<HHmmss>[_N].<format>
team-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

`_1`, `_2` osv. bruges ved samme-sekund-kollisioner. Flerformatseksporter deler stem og suffix og publiceres først, når hele sættet kan skrives.

## Skemaer og kompatibilitet

Produktversion `0.1.0` er uafhængig af lokale dataformater. Aktuelle formater er:

- gruppekonfiguration schema 8;
- `hub-settings.json` schema 4 og `analysis-inbox.json` schema 2;
- manager-eventledger schema 2;
- teamsnapshot schema 2 og spillersnapshot schema 4;
- `seasons.json` schema 1;
- turneringsparringer schema 1;
- `GameMetadata` schema 2 og fixturecache schema 1;
- `RefreshManifest` schema 2 og 3, turneringsrevisioner og `integrity-index.json` schema 1 samt `backup-manifest.json` schema 2. Manifest-store læser fortsat refresh-manifest schema 1, og restore læser fortsat backup schema 1.

Spillersnapshot schema 4 bevarer valgfrie popularitets-, trend-, indeks-, `stats`- og `total_stats`-felter. Schema 1–3 indlæses uden omskrivning. Manglende rundestatus bliver `unknown` og giver foreløbige beregninger, indtil en manuel genhentning bekræfter `complete`. Defekte eller inkompatible snapshots ignoreres med en synlig advarsel.

Flere immutable snapshots med samme rundenummer bevares og sorteres kronologisk i `PlayerStatisticsIndex`. Historiske rundekurver vælger fortsat nyeste snapshot pr. runde, mens **Mellem hentninger**, watchregler og ændringsforklaring kan bruge hver intra-runde-observation.

Spillereksport schema 3 fryser både de valgte beregnede kolonnedefinitioner, deres celleværdier og antal formelfejl. Eksporten er afledt og ændrer aldrig snapshots eller `hub-settings.json`.

Refresh-manifest schema 2 forklarer udfald, cachegenbrug, fejl og retryrelation pr. trin. Schema 1 får `not_recorded` for datakilder, det gamle format ikke registrerede. Se den brugerrettede statusordbog i [Rundecenter og daglig arbejdsgang](round-center.md).

Schema 3 bevarer desuden den ønskede runde for hvert trin og kan registrere afbrudte trin, der ikke blev forsøgt. Schema 1–3 læses uden omskrivning. Ved en samlet opdatering gemmes en separat journal i `refresh-runs`, så et afbrudt eller delvist forsøg kan identificeres og genoptages. Et gemt snapshot er ikke i sig selv bevis for, at hele opdateringen blev fuldført.

Kørselsjournalen skriver schema 2 og læser fortsat schema 1 uden startup-migration. En ventende spillertransition fastholder de oprindelige snapshotreferencer, alarmregler og en entydig forsøgsidentitet. Det validerede kildesvar stages uforanderligt som `refresh-runs/<run-id>/player-<attempt-id>.json`, før det publiceres som snapshot. Genoptagelse genbruger dette svar og gentager den nødvendige idempotente efterbehandling, også efter stop mellem spillerhentning og alarmbehandling. Reglerne for det allerede bestilte arbejde fastholdes; ændrede alarmindstillinger gælder nye opdateringer. Manglende bevis i en ældre afbrudt journal giver en synlig fejl frem for en påstået fuldført alarmbehandling. Journaler og stages medtages i backup og integritetskontrol.

En metadatarevision er en uforanderlig indsamling, mens den aktuelle metadatafil viser den værdi, der er anvendt. Revisionen skrives før den aktuelle fil; hvis den sidste skrivning fejler, bevares den tidligere aktuelle værdi. En revision nyere end den aktuelle fil er ikke bekræftet som anvendt, og for ældre revisioner kan anvendelse ikke dokumenteres ud fra revisionen alene. Revisioner er derfor ikke en fuldstændig log over anvendte værdier.

## Backup og gendannelse

**Data og lager → Import og backup** opretter én ZIP med konfiguration, alarmindbakke, sæsoner, snapshots, manifester, turneringsrevisioner, publicerede parringer, spilmetadata, fixturecache, importerede historiske data og manager-eventledger. Afledte eksporter, integritetsindekset og gamle backups medtages ikke.

`backup-manifest.json` indeholder schema-version, tidspunkt, filstørrelser og SHA-256 for hver fil. Før restore vises en preview, og hele arkivet valideres. Restore afvises ved:

- absolutte stier, backslashes, `..` eller ukendte rødder;
- links, mapper, dubletter eller ikke-manifesterede filer;
- grænser for medlemstal, enkeltfil, total udpakket størrelse, manifest og kompressionsratio;
- utilstrækkelig ledig diskplads eller en kildefil, der ændrer sig under backup;
- størrelse- eller checksumfejl;
- ugyldig JSON eller ukendt schema-version.

Efter brugerens bekræftelse oprettes først en valideret rollback-ZIP. Nye `config`- og `data`-træer stages og installeres med hver sine mappeflytninger. En fanget fejl udløser tilbagerulning af de gennemførte flytninger. Hvis tilbagerulningen også fejler, angiver fejlmeddelelsen den bevarede rollback-ZIP. Denne garanti gælder fangede undtagelser; der er ingen varig restorejournal, som automatisk genopretter installationen efter processtop eller strømsvigt midt i flytningerne. Efter succes ryddes Streamlit-caches, og appen genindlæses. En rigtig restore bør kun afprøves mod test-ejede `AppPaths`.

Offentlig backup og indeksreparation tager datalåsen og derefter konfigurationslåsen. Konfigurationsstores og appens direkte hentehandlinger deltager i de samme ejerskabsgrænser, så backup ikke indsamler en halv konto-/gruppeændring eller overlapper en igangværende publicering. Den interne ZIP-writer tager ikke nye låse, når restore allerede ejer begge. Låsene koordinerer Hub-kald; eksterne programmer, som redigerer filerne direkte, er fortsat dækket af filstabilitets- og checksumkontrollerne.

Indstillings- og sæsonredigeringer sammenligner deres indlæste grundlag med den aktuelle fil under konfigurationslåsen. Et forældet grundlag afvises med besked om at genindlæse; det overskriver ikke nyere ændringer eller en netop gendannet konfiguration. Stores' eksplicitte `save()` bevarer funktionen som fuld erstatning, mens almindelige redigeringer bruger `save_if_current()`.

Der er ingen automatisk eller cloud-baseret backup. ZIP-backup er altid en manuel handling.

## Integritet, lager og retention

**Data og lager → Overblik** viser datastatus og eksakt lagerforbrug pr. managerspil og kategori. **Integritet og oprydning** tilbyder hurtig metadata-/schemakontrol, fuld streamet SHA-256-kontrol og et preview af en indeksreparation. Reparation erstatter kun `integrity-index.json`; korrupte data ændres eller skjules aldrig.

Retention bevarer den nyeste gyldige spillerfil pr. `(locale, spil, runde)` og den nyeste gyldige holdfil pr. `(locale, spil, hold, runde)`. Snapshotreferencer i læsbare manifester bevares også; ved ældre manifester og fejl i efterbehandling bevares flere snapshots, som kan være nødvendige til historik eller genforsøg. En uafsluttet eller ulæselig opdateringsjournal og ulæselige manifester standser retention konservativt. Korrupte og uklassificerbare filer vælges aldrig automatisk. Arkivering opretter og validerer først en ZIP og kontrollerer igen, at de valgte filer må fjernes. Eksisterende mellemversionsarkiver udelades fra sletning, hvis de indeholder refererede snapshots, eller hvis sikker sletning ikke kan fastslås. Alle handlinger er manuelle, viser preview og kræver bekræftelse.

Arkivering og sletning tager samme lokale lås som en samlet opdatering. En handling afvises, mens en anden ejer låsen; prøv igen, når den igangværende handling er færdig. Preview tager ikke låsen og skriver ikke filer.

## Overrides

Kilde-CLI'en understøtter `--data-dir`, `--accounts-file`, `--output-dir`, `HOLDET_DATA_DIR`, `HOLDET_CONFIG_DIR` og `HOLDET_OUTPUT_DIR`. Præcedensen er specifikt CLI-argument, samlet CLI-root, specifik miljøvariabel, samlet miljøroot og Windows-standard. En eksportoverride flytter ikke kanonisk cache.

## Privatliv og sletning

Profil-URL'er, bruger-ID'er, hold, grupper, watchlists, aliaser og historiske medlemskaber er personlige data og må ikke committes. Luk dashboardet og slet disse to mapper for at fjerne alle programdata:

```text
%APPDATA%\Holdet Fantasy Hub
%LOCALAPPDATA%\Holdet Fantasy Hub
```

Sletning fjerner også lokale backups, hvis de ikke er kopieret ud af backupmappen.
