# Spillerstatistik og scouting

Spillerstatistik kan bruges på `/players`, i et managerspil eller fra den globale `/scouting`-side. Alle tre visninger læser lokal cache ved navigation. Kun knapper med **Hent**, **Opdater** eller **Prøv igen** kontakter Holdet.dk; kun gemme-, bulk- og eksportknapper skriver lokalt.

## Ruter og paneler

`/scouting?locale=…&game=…&view=watchlist|smartlists|notes` samler Watchlist, Smartlister og Noter på tværs af spil. En spillerrute bruger `/player?locale=…&game=…&player=…`; tabeller linker direkte dertil med den stabile spilleridentitet.

Spillerstatistikken har fem lazy-loadede paneler:

- **Spillerliste**: filtre, multi-row bulkhandlinger og eksport;
- **Scouting**: percentiler, potentiale/risiko, ownership og scatterplots;
- **Sammenligning**: 2–5 spillere, absolutte værdier, percentiler og historiske kurver;
- **Watchlist**: begrundelser, regler og dataprovenance;
- **Ændringer**: **Mellem hentninger** eller **Mellem runder**.

`panel=compare` er fortsat gyldigt. Watchlist-deeplinks bruger `panel=watchlist`. Den nyeste kompatible cache vises straks. En fælles rundevælger ændrer ikke cache og starter ikke netværk.

## Watchlist og bulkhandlinger

En watchlistpost kan have flere standardbegrundelser — **Kaptajnkandidat**, **Vent på prisfald** og **Modstander til mit hold** — samt højst 280 tegn fritekst. En spiller kan have højst otte regler:

- positivt absolut eller procentuelt pris-/værdifald;
- positiv absolut eller procentuel pris-/værdistigning;
- enhver statusovergang, også bedring, aktivering og fjernelse;
- Form 3 eller Form 5 over eller under en numerisk tærskel.

Tærskelregler alarmerer kun ved krydsning. Første observation uden baseline giver ingen tærskelalarm. En regel kan udløses igen, når signalet først har været tilbage på den modsatte side. Statusreglen beskriver hver faktisk overgang.

Schema 1–3-watchlister får statusændring som standard ved in-memory-migration. En schema 4-post kan derimod bevidst have nul regler; rydning genaktiverer ikke standardreglen. Den globale Watchlist og spillerdetaljen har den samlede editor til at tilføje eller fjerne regler, begrundelser og fritekst.

Spillerlisten og Scouting understøtter atomiske bulkhandlinger: tilføj/fjern watchlist, tilføj/fjern tags, sæt/ryd begrundelser og sæt/ryd regler. Alle valgte spillere valideres før én samlet save; en ugyldig spiller eller grænse giver ingen delvis write.

Alarmer oprettes kun efter en eksplicit aktuel spiller- eller managerspilopdatering. Historisk backfill opretter ikke aktuelle alarmer. Event-ID'et indeholder regel, forrige og nuværende snapshottidspunkt samt overgang, så to reelle hændelser i samme runde ikke deduplikeres sammen. Alarmvisning, filtrering og navigation er cache-only; læst, afvist og rydning er eksplicitte writes.

## Smartlister og global notesøgning

Smartlister persisteres ikke. De beregnes ved hver cachelæsning:

| Liste | Kontrakt |
| --- | --- |
| Billigste aktive angribere | Aktiv, ikke-deaktiveret og normaliseret angriberposition; pris og stabilt ID sorterer. |
| Lav volatilitet | Mindst tre afsluttede observationer og stabilitet ≥ 70; stabilitet faldende og pris stigende sorterer. |
| Nyligt aktiverede | Seneste overgang fra inaktiv/deaktiveret til aktiv inden for syv dage. |

Noter, tags, watchlist-begrundelser, spillernavn, hold og position kan søges på tværs af alle spil. En note bevares, selv om det aktuelle snapshot mangler spilleren; rækken mærkes **Mangler** i stedet for at blive skjult eller slettet. Noten er højst 2.000 tegn, og højst 12 tags á 24 tegn normaliseres case-insensitivt.

## Percentiler, peers og lignende spillere

Percentiler beregnes med gennemsnitsrang ved ties blandt aktive, ikke-deaktiverede spillere i samme normaliserede position eller kategori. Der kræves mindst fem numeriske peers. UI viser altid absolut værdi, percentil, kohortestørrelse eller den konkrete grund til manglende grundlag.

Positionsmedianen vises for pris, totalvækst, Form 3, Form 5, stabilitet og popularitet, når mindst tre peers har metrikken. De fem nærmeste prisalternativer er fra samme position og sorteres efter absolut prisafstand, lavere pris, højere Form 3, navn og spiller-ID.

**Find lignende spillere** bruger positionsrelative percentiler med vægtene pris 40 %, Form 3 35 % og stabilitet 25 %. Position er et hårdt filter. Mindst to fælles metrikker kræves; et manglende felt får maksimal komponentstraf. De fem laveste afstande vises med komponentdeltaer.

## Potentiale, risiko og ownership

Potentiale er en transparent 0–100-score:

```text
50 % Form 3-percentil + 20 % Form 5-percentil + 30 % værdieffektivitet
```

Værdieffektivitet er `growth_per_million` i pengespil og totalvækst i pointspil. Form 3 og mindst én anden komponent kræves; tilgængelige vægte renormaliseres.

Risiko er:

```text
70 % omvendt stabilitetspercentil + 20 % statusrisiko + 10 % datarisiko
```

Statusrisiko er maksimum af aktiv 0, inaktiv 60, skadet/karantæne 80 og deaktiveret 100. Datarisiko er 0 ved endeligt grundlag med mindst fem observationer, 40 ved 3–4, 70 ved foreløbige data og 100 ved utilstrækkeligt eller uverificeret grundlag. Manglende stabilitetspercentil fail-closer til 100 i komponenten.

Ownership holdes adskilt fra potentiale og risiko. Kildens 0–1-popularitet normaliseres til 0–100 procent i scoutingberegninger og -visninger. Popularitet ≤ 10 % mærkes **differential**, og ≥ 25 % mærkes **template**. For en spiller uden for det valgte eget hold er ownership-risiko `popularitet_i_procent * potentiale / 100`. Manglende popularitet, potentiale eller trupgrundlag vises som **Ikke tilgængelig**, aldrig nul.

## Plots og tilgængelighed

Scouting viser Altair-plots for pris mod Form 3, totalvækst mod stabilitet og potentiale mod risiko. Watchlistspillere og valgte tabelrækker fremhæves med både form, farve og tekst. Tooltip viser absolutte scoutingværdier og percentiler, og hvert plot angiver ekskluderede rækker. En ekspander med den samme datatabel er den tilgængelige, sorterbare repræsentation.

Status, præcis snapshotalder og sikkerhed vises som tilstødende felter i spillerliste, scouting, sammenligning, watchlist og spillerdetalje.

## Ændringer og forklaring

**Mellem hentninger** sammenligner de to seneste kronologiske snapshots, også når de har samme rundenummer. **Mellem runder** bruger fortsat det nyeste snapshot i valgt og foregående tilgængelige runde. Begge viser rundenumre og præcise hentetidspunkter; manglende forgænger giver en tom tilstand.

Spillerdetaljens **Hvorfor ændrede denne spiller sig?** viser observerede deltaer for pris, vækst, status, popularitet, popularitetsændring, trend, indeks samt numeriske `stats` og `totalStats`. Felter kaldes kun kausale/additive, når en verificeret sæsonprofil har komplette vægte, og bidragene afstemmer den observerede målændring. Ellers er de udtrykkeligt samtidige observationer.

## Sikker formelmotor

Der kan gemmes højst 20 beregnede kolonner pr. spil. Motoren bruger `ast.parse(..., mode="eval")` og en egen tree-walker; den bruger aldrig `eval`, `compile` eller vilkårlig Python.

Tilladt er tal, dokumenterede metriknavne, parenteser, `+ - * / %`, sammenligninger, `and/or/not` og `abs`, `min`, `max`, `round`, `coalesce`, `clamp`, `ifelse`. Attributter, subscripts, strenge, imports, comprehensions, lambdaer, potens, ukendte funktioner og andre beregnede kolonner afvises. Grænserne er 500 tegn, 100 AST-noder og dybde 12. Division med nul, manglende input og NaN/inf giver en tom celle og tælles som synlig formelfejl.

En eksport fryser de valgte definitioner, værdier og fejltal i schema 3. De beregnede formler er kun UI-/eksportfunktionalitet og eksponeres ikke som eksekverbar funktionalitet i det lokale API.

## Snapshots, formater og eksport

Spillersnapshot schema 4 bevarer valgfrie `popularity`, `popularity_change`, `trend`, `index`, `stats` og `total_stats`. Schema 1–3 læses fortsat uden startup-write. Valgfrie offentlige felter parses fail-closed, så kildedrift ikke gør den obligatoriske spillerliste inkompatibel.

Form 3/5 bruger nyeste afsluttede observation pr. runde; huller interpoleres ikke. Spillersnapshots bevarer derimod hver immutable hentning, så intra-runde-diff ikke mister observationer.

Filtre påvirker kun den viste og eksporterede tabel. Numeriske felter forbliver tal; manglende værdier vises `–`. Eksport kan oprettes som TXT, JSON, Markdown, CSV, XLSX eller valgfri Parquet under:

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\players\<game-slug>\data-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

Se [Analyse- og beslutningscenter](decision-analysis.md) for de øvrige analysekontrakter, [Datahentning](data-retrieval.md) for kildefelterne og [Datalagring](data-storage.md) for migration og backup.
