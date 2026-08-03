# Spillerstatistik

Spillerstatistik kan bruges som selvstændig side eller inde i et managerspil. Den selvstændige side vælger ét managerspil; managerspilsfanen genbruger allerede valgt spil. Ingen af dem registrerer eller henter data ved almindelig navigation.

## Runde og paneler

Én fælles rundevælger styrer tre paneler:

- **Spillerliste** med filtre, sortering og eksport.
- **Sammenligning og watchlist** for 2–5 spillere fra samme managerspil og runde.
- **Ændringer** mellem valgt runde og den foregående tilgængelige runde.

Den nyeste kompatible cache vises med det samme. Netværk bruges kun af **Hent seneste spillerstatistik**, **Hent manglende runder**, **Hent runde X**, **Opdater runde X** eller **Prøv igen**. Hver hentning gemmer hele det ufiltrerede resultat som et kanonisk JSON-snapshot; rundestatus og hentetidspunkt fryses sammen med dataene.

## Sammenligning og watchlist

Vælg 2–5 spillere for at sammenligne pris eller point, totalvækst, rundevækst, status og historiske kurver. Spillere identificeres primært med spilidentitet og `entry_id`. Ældre snapshots uden sikker ID bruger en tydeligt markeret fallback med navn, hold og position.

Watchlisten gemmes atomisk i `%APPDATA%\Holdet Fantasy Hub\config\hub-settings.json` og følger med i en Hub-backup. En favorit gemmer kun identiteten; visning og kurver bygges fortsat af lokale snapshots. Valg af eller navigation til en favorit starter ikke en hentning.

Historikken bruger de seneste snapshots for hver runde. Runder uden data forbliver huller i grafen og forbindes ikke kunstigt.

## Hvad har ændret sig?

Ændringspanelet finder:

1. det seneste spillersnapshot i den valgte runde;
2. det seneste spillersnapshot i den foregående tilgængelige runde.

Begge rundenumre og hentetidspunkter vises. Resultatet opdeles i nye, fjernede og ændrede spillere samt pris-/point- og statusskift. En manglende forgænger giver en forklarende tom tilstand i stedet for at sammenligne med et vilkårligt snapshot.

Hvis en af runderne er `in_progress` eller `unknown`, eller den afsluttede runde ikke er genhentet efter sluttidspunktet, markeres sammenligningen som **Foreløbig**. Markeringen beskriver datagrundlaget og ændrer ikke de rå snapshots.

## Formater og enheder

| Spiltype | Enhed | Primære labels |
| --- | --- | --- |
| Fodbold | Penge | Pris, Totalvækst, Vækst |
| Tourspillet og anden salary-cap-cykling | Penge | Pris, Totalvækst, Vækst |
| Tour/Vuelta Manager og anden pointcykling | Point | Point, Totalændring, Rundeændring |
| Motor Manager | Penge | Pris, Totalvækst, Vækst |
| Golf Manager | Point | Point, Totalændring, Rundeændring |

Cykling kan ikke klassificeres ud fra route-navnet alene; rulesettets `salaryCap` afgør enheden.

## Filtre og tabel

Filtre påvirker kun den viste og eksporterede tabel, aldrig det kanoniske snapshot. De omfatter fritekst, hold/land, position/kategori, pris/point, vækst, status, kolonner og sortering. Status kan ignoreres, kræves eller udelukkes; flere krævede statustyper skal alle være til stede.

Numeriske felter bevares som tal, så Streamlits sortering virker, men vises med danske tusindtalsseparatorer. Manglende værdier vises som `–`. Navn bruges som stabil tie-breaker.

## Eksport

**Opret eksport** fryser det aktuelle filtrerede resultat som TXT, JSON og/eller Markdown, gemmer filerne og opretter downloads med præcis de samme bytes. Et tomt filterresultat kan ikke eksporteres.

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\players\<game-slug>\data-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

Se [Klienter](clients.md) for CLI-eksempler og [Datalagring](data-storage.md) for forskellen mellem snapshots, Hub-indstillinger og afledte eksporter.