# Spillerstatistik

Spillerstatistik kan bruges som selvstændig side eller inde i et managerspil. Den selvstændige side vælger et registreret spil eller indsætter en Holdet-URL i feltet **Spil eller Holdet-URL**; managerspilsfanen genbruger allerede valgt spil. Ingen af dem registrerer eller henter data ved almindelig navigation.

## Runde og paneler

Én fælles rundevælger styrer tre paneler:

- **Spillerliste** med filtre, sortering og eksport.
- **Sammenligning og watchlist** for 2–5 spillere fra samme managerspil og runde.
- **Ændringer** mellem valgt runde og den foregående tilgængelige runde.

Den nyeste kompatible cache vises med det samme. Netværk bruges kun af **Hent seneste spillerstatistik**, **Hent manglende runder**, **Hent runde X**, **Opdater runde X** eller **Prøv igen**. Hver hentning gemmer hele det ufiltrerede resultat som et kanonisk JSON-snapshot; rundestatus og hentetidspunkt fryses sammen med dataene.

Spillerlisten tilføjer cacheberegnede kolonner for form 3/5, stabilitet og, i pengespil, historisk vækst pr. aktuel million. Hver række har et direkte link til `/player?locale=…&game=…&player=…&round=…`.

## Spillerdetalje, noter og statusalarmer

Spillerdetaljen har én H1 og viser pris-/pointkurve på faktiske rundenumre, form, stabilitet, vækst pr. million, statushistorik, watchlist samt note/tag-editor. En ikke-afsluttet runde markeres **Foreløbig**; en tom eller ikke-numerisk serie forklares uden at oprette en tom graf.

Noter og tags gemmes kun ved **Gem note og tags**. Noten er højst 2.000 tegn, og højst 12 tags á 24 tegn normaliseres case-insensitivt uden dubletter. Standardtags er `overvej`, `undgå`, `kaptajn` og `langsigtet`, men egne tags er tilladt.

Efter **Hent seneste spillerstatistik** sammenlignes watchliststatus med forrige snapshot fra samme eller seneste tidligere runde. Nye skader, deaktivering, inaktivitet, karantæne eller fjernelse fra spillerlisten vises i det relevante managerspils **Statusalarmer**-fane. Fanen viser watchlistens størrelse og linker tilbage til **Sammenligning og watchlist**; den duplikerer ikke editoren. **Solgt** bruges kun, hvis en fremtidig kilde leverer et eksplicit felt. En historisk rundehentning udløser ikke alarmer.

Watchlists for spil, der kun bruges på den selvstændige Spillerstatistik, åbner en skjult spilfiltreret alarmroute uden global sidebar-destination. Alarmvisning og filtrering er cachebaseret; kun læst, afvist og rydning skriver alarmtilstand.

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

Filtre påvirker kun den viste og eksporterede tabel, aldrig det kanoniske snapshot. De omfatter fritekst, hold/land, position/kategori, pris/point, vækst, status, kolonner og sortering. De fulde filtre ligger i panelet **Filtre**, mens anvendte filtre vises som kompakte chips i en sticky handlingslinje. De træder først i kraft ved **Anvend filtre**; **Nulstil** rydder hele filtersættet. Status kan ignoreres, kræves eller udelukkes; flere krævede statustyper skal alle være til stede.

Spillerlisten, filtrene og eksporten kører i samme sekventielle fragment. Dataframe-keyen er baseret på route og spilidentitet med en eksplicit schema-version, ikke runde, tidspunkt eller rækkeantal. Derfor bevares sorteringsvalg og den native tabelscroll ved et relateret fragment-rerun.

Aktuelle filtre, kolonner og sortering kan gemmes, omdøbes og slettes som en versioneret profil med unikt navn pr. spil. De ikke-persistente profiler **Billige forsvarere**, **Aktive under 5 mio.** og **Skadede spillere** vises kun, når enhed og felter gør dem meningsfulde. En profil anvendes først ved et eksplicit klik.

Numeriske felter bevares som tal, så Streamlits sortering virker, men vises med danske tusindtalsseparatorer. Manglende værdier vises som `–`. Navn bruges som stabil tie-breaker.

## Eksport

Eksportsektionen er kollapset som standard. **Opret eksport** fryser det aktuelle filtrerede resultat som TXT, JSON, Markdown, CSV, XLSX og/eller valgfri Parquet, gemmer filerne og opretter downloads med præcis de samme bytes. CSV bruger UTF-8 med BOM; XLSX og CSV neutraliserer regnearksformler. Et tomt filterresultat kan ikke eksporteres.

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\players\<game-slug>\data-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

Se [Analyse- og beslutningscenter](decision-analysis.md) for formler og provenance, [Klienter](clients.md) for CLI-eksempler og [Datalagring](data-storage.md) for forskellen mellem snapshots, Hub-indstillinger og afledte eksporter.
