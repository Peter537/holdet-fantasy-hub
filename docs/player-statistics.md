# Spillerstatistik

Spillerstatistik kan bruges som en selvstændig dashboardside eller fra et registreret managerspil. En selvstændig hentning registrerer ikke spillet i **Mine managerspil**.

## Data og runder

Vælg en gemt spilreference, en slug eller en fuld Holdet-URL. En bar slug bruger locale `da`. Dashboardet viser nyeste kompatible cache med det samme og kontakter kun Holdet via:

- **Hent seneste spillerstatistik**;
- **Hent manglende runder** for et valgt fra-/til-interval;
- **Hent runde X**;
- **Opdater runde X**;
- **Prøv igen** efter en fejl.

Batchhentningen springer allerede gemte runder over, genbruger én klient, fortsætter efter enkeltfejl og viser en samlet opsummering. Enkeltrundehentningen bruges fortsat til korrektioner.

Hver hentning gemmer hele, ufiltrerede resultatet som et kanonisk JSON-snapshot. Datakilden vises med lokal dato og den frosne rundestatus fra hentetidspunktet; der foretages ingen automatisk statuskontrol.

## Formater og enheder

Holdets route-variant, normaliserede sportsformat og værdienhed behandles separat:

| Spiltype | Enhed | Primære labels |
| --- | --- | --- |
| Fodbold | Penge | Pris, Totalvækst, Vækst |
| Tourspillet og anden salary-cap-cykling | Penge | Pris, Totalvækst, Vækst |
| Tour/Vuelta Manager og anden pointcykling | Point | Point, Totalændring, Rundeændring |
| Motor Manager | Penge | Pris, Totalvækst, Vækst |
| Golf Manager | Point | Point, Totalændring, Rundeændring |

Cykling kan altså ikke klassificeres som penge eller point ud fra route-navnet alene; rulesettets `salaryCap` afgør enheden.

## Filtre

Filtre påvirker kun den viste og eksporterede tabel, aldrig det kanoniske snapshot. De kan kombineres:

- Fritekst i navn, hold/land og position/kategori.
- Et eller flere hold/lande og positioner/kategorier.
- Minimum og maksimum for pris/point, totalvækst og rundevækst.
- Manglende vækst: medtag, udelad eller vis kun manglende.
- Kolonnevalg; navn er altid obligatorisk.
- Sorteringsfelt og stigende/faldende orden med navn som stabil tie-breaker.

Hver status har tre tilstande:

| Tilstand | Betydning |
| --- | --- |
| `Ignorér` | Status påvirker ikke filtret |
| `Kræv` | Spilleren skal have statusmarkøren |
| `Udeluk` | Spilleren fjernes, hvis statusmarkøren findes |

Flere krævede statusser skal alle være til stede. En spiller fjernes, hvis bare én udelukket status er til stede. Status vises i rækkefølgen `Inaktiv · Deaktiveret · Skadet · Karantæne`.

## Tabel og sortering

Tabellen beholder pris-, point- og vækstværdier som numeriske data, så Streamlits sortering virker. De vises med danske tusindtalsseparatorer, eksempelvis `12.345.000` og `-227.000`. Manglende værdier vises som `–`.

Standardrækkefølgen er pris/point faldende og derefter navn. Den valgte sortering anvendes før eksport.

## Eksport

Vælg en eller flere af TXT, JSON og Markdown. **Opret eksport** fryser det aktuelle filtrerede resultat, gemmer filerne lokalt og opretter browserdownloads med præcis de samme bytes.

Alle formater indeholder spil, locale, format, enhed, runde, kilde, tidspunkt, filtre, kolonner og rækkeantal:

- TXT bruger danske metadata og en tab-separeret tabel.
- Markdown bruger en titel, metadata og en Markdown-tabel.
- JSON bruger stabile engelske nøgler og rå numeriske værdier.

Filer gemmes under:

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\players\<game-slug>\data-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

De valgte formater deler samme stem og kollisionssuffix. Et tomt filterresultat kan ikke eksporteres.

## CLI

Den samme filter- og eksportmotor bruges af CLI'en. Se de korte eksempler i [Klienter](clients.md), og brug den aktuelle hjælpevisning for samtlige flag:

```powershell
py -3.14 .\cli\main.py players --help
```
