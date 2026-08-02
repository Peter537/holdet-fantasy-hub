# Datalagring

Mutable, personlige og voksende data ligger i Windows AppData og ikke i repositoryet. `resolve_paths()` beregner stierne uden at oprette mapper; mapper oprettes først af en eksplicit skrive- eller åbningshandling.

## Standardplaceringer

```text
%APPDATA%\Holdet Fantasy Hub\config\
├── accounts.json
└── groups.json

%LOCALAPPDATA%\Holdet Fantasy Hub\
├── data\
│   ├── snapshots\
│   ├── manifests\
│   └── group-revisions\
└── exports\
    ├── players\
    └── teams\
```

Der findes ikke implicitte `cache`- eller `logs`-mapper i den aktuelle `AppPaths`-model.

## Hvad gemmes hvor?

| Område | Indhold |
| --- | --- |
| `config/accounts.json` | Profiler, som kan bruges til eksplicit kontoopdagelse |
| `config/groups.json` | Managerspil, grupper, medlemsreferencer og aktive turneringsplaner |
| `data/snapshots` | Komplette, kanoniske spiller- og teamsnapshots |
| `data/manifests` | Resultater fra eksplicitte gruppe- og managerspilopdateringer |
| `data/group-revisions` | Uforanderlige arkiverede turneringsrevisioner |
| `exports/players` | Filtrerede spillerresultater som TXT, JSON eller Markdown |
| `exports/teams` | Brugerrettede teameksporter som TXT, JSON eller Markdown |

Snapshots og manifester er programdata. Eksporter er afledte dokumenter, som brugeren kan dele eller bearbejde. En eksportoverride flytter ikke den kanoniske cache.

## Kontoformat

En manglende `accounts.json` behandles som en tom konfiguration. Kontoen kan oprettes i **Data og lager → Gemte konti**, eller filen kan følge dette fiktive format:

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

- `key` er en stabil teknisk nøgle og kan bruges som CLI-selector.
- `label` er det viste navn og kan omdøbes.
- `profile_url` er den offentlige Holdet-profil; bruger-ID'et udledes fra URL'en.

Eksemplet kontaktes aldrig af tests. Erstat alle tre værdier med dine egne, hvis du vedligeholder filen manuelt.

## Filnavne og uforanderlighed

Spillersnapshots bruger:

```text
player-round<round>_<MMDD>_<HHmmss>[_N].json
```

Spiller- og teameksporter bruger henholdsvis:

```text
data-round<round>_<MMDD>_<HHmmss>[_N].<format>
team-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

`_1`, `_2` og så videre bruges ved samme-sekund-kollisioner. Flerformatseksporter deler stem og suffix, og filer publiceres først, når hele det valgte sæt kan skrives.

## Overrides

Kilde-CLI'en understøtter:

- `--data-dir PATH` som samlet rod med `config`, `data` og `exports`.
- `--accounts-file PATH` for én bestemt kontofil.
- `--output-dir PATH` for den konkrete kommandos eksporter.
- `HOLDET_DATA_DIR` som samlet miljøoverride.
- `HOLDET_CONFIG_DIR` for konfiguration.
- `HOLDET_OUTPUT_DIR` for eksporter.

Præcedensen er specifikt CLI-argument, samlet CLI-root, specifik miljøvariabel, samlet miljøroot og til sidst Windows-standarden. `--output-dir` ændrer ikke placeringen af kanoniske snapshots.

## Schemaer og kompatibilitet

Produktversionen `0.1.0` er uafhængig af lokale dataformater. Aktuelt bruges blandt andet gruppekonfiguration schema 7, teamsnapshot schema 2 og spillersnapshot schema 3.

Nye spiller- og teamsnapshots gemmer rundestatus og rundens officielle sluttid. Ældre kompatible snapshots indlæses som `unknown`, omskrives ikke ved opstart og kan vises, men de giver ikke turneringspoint før en manuel genhentning har bekræftet runden som `complete`.

Gamle cykelsnapshots uden en sikker enhed kan kræve en ny hentning. Defekte eller inkompatible snapshots ignoreres med en synlig advarsel frem for at stoppe hele dashboardet.

## Privatliv og sletning

Profil-URL'er, bruger-ID'er, hold, grupper og historiske medlemskaber bør behandles som personlige data. De må ikke committes til et offentligt repository.

Luk dashboardet og slet disse to mapper for at fjerne alle programdata:

```text
%APPDATA%\Holdet Fantasy Hub
%LOCALAPPDATA%\Holdet Fantasy Hub
```

Der er ingen automatisk backup, cloud-synkronisering eller oprydning.
