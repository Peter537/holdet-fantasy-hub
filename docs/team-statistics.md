# Holdstatistik

Holdstatistik viser ét fantasyhold ad gangen med overblik, aktuel opstilling, offentlig rundehistorik og eksport. Den findes både som selvstændig side, som managerspilsfane og som detaljevisning fra grupper.

## Sådan findes et hold

Standalone-siden vælger først et managerspil og derefter et hold. Mulige kilder er:

- kompatible teamsnapshots i den lokale cache;
- faste holdreferencer fra grupper;
- resultater fra den aktuelle sessions eksplicitte kontoopdagelse;
- en direkte `/fantasyteams/<id>`-URL;
- et numerisk team-ID, som kombineres med det valgte spils locale og slug.

**Find hold på konfigurerede konti** kontakter hver offentlig profil individuelt, rapporterer delvise fejl og deduplikerer efter spil og team-ID. Fundne hold føjes ikke automatisk til managerspil eller grupper.

En direkte URL skal tilhøre det valgte spil. Arkiverede managerspil kan vise og eksportere cache, men kan ikke hente fra deres låste managerspilsvisning. Det samme hold kan stadig hentes eksplicit fra standalone-siden.

## Eksplicit hentning og cache

Netværk bruges kun af **Find hold**, **Hent hold**, **Opdater hold** eller **Prøv igen**. En vellykket hentning gemmer et komplet kanonisk teamsnapshot. Ved fejl bevares den seneste kompatible cache, og tekniske detaljer kan foldes ud.

Til rundesammendrag vælges det nyeste snapshot, hvis historik indeholder runden. Til opstillinger kræves et snapshot, hvis `game.current_round` er præcis den valgte runde. En nyere historik kan derfor efterfylde manglende rundesammendrag uden at opfinde en historisk opstilling.

## Holdvisning

### Overblik

Viser de tilgængelige felter for den valgte runde:

- totalværdi eller point og rundens ændring;
- bank og spillerværdi i salary-cap-spil;
- bankrente, spillerændring, transfer, kaptajnbonus og specialbonus;
- round rank, overall rank og rangbevægelse;
- topplacering og udskiftninger, når Holdet leverer dem.

### Holdopstilling

En præcis rundesnapshot-opstilling viser navn, hold/land, position/kategori, værdi/point, rundevækst, vækst siden køb, købsrunde, rolle og status. Mangler en præcis opstilling, vises rundesammendraget fortsat sammen med en tydelig forklaring.

### Historik

Historikken vises newest-first og indeholder alle offentligt tilgængelige rundesammendrag med total, bank, ændringskomponenter, udskiftninger og rangeringer. Golf og andre pointspil udelader pengefelter.

### Eksport

Eksporten har to scopes:

- **Komplet snapshot**: seneste overblik, aktuel opstilling og komplet historik.
- **Valgt runde**: rundesammendrag og kun en opstilling, hvis den er gemt præcis i runden.

TXT, JSON og Markdown kan oprettes samtidigt. JSON-eksporten er et brugerrettet dokument med `document_type: "team_export"` og må ikke forveksles med det kanoniske teamsnapshot. Browserdownloadet bruger de samme bytes som den lokale fil.

Filer gemmes under den eksisterende konti- og holdstruktur i:

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\teams\<game-slug>\...\team-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

## CLI

Standardkaldet gemmer fortsat et kanonisk snapshot og opretter TXT og JSON. Markdown og rundescope kan vælges eksplicit. Se [Klienter](clients.md), eller vis alle aktuelle argumenter:

```powershell
py -3.14 .\cli\main.py teams --help
```
