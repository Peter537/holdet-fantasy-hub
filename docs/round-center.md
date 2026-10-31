# Rundecenter og daglig arbejdsgang

Managerspillets **Overblik** viser gemte runder, datastatus, spillerdækning samt stillinger og kampe. Rundecenteret åbnes under **Datadetaljer og rundecenter**. De to øvrige hovedfaner er **Ligaer og turneringer** og **Spillere**. Det eksisterende link med `section=round-center` åbner fortsat Overblik. Historik, analyse, holdstatistik og administration findes under **Flere funktioner**; de valgfrie værktøjer følger tilvalgene i Indstillinger.

Rundecenter samler handelsvindue, datastatus, opdatering, rundeafvigelser, sammenligning og gruppematrix. Rundens historie vises, når værktøjet er aktiveret. Selve overblikket er cache-only: navigation og previews læser lokale snapshots, metadata, alarmer og manifester uden netværk eller vedvarende writes.

## Efter en pause: afgræns og opdater

På **Mine managerspil** starter **Opdater alle** indhentning for alle aktive managerspil. **Vælg spil og omfang** giver mulighed for at vælge bestemte spil og tilvælge **Genhent også aktuelle data**. På et managerspil eller en konkurrence starter **Opdater data** den samme indhentning for det åbne scope. Arkiverede managerspil indgår ikke i hentningen; gendan først spillet, hvis det skal opdateres igen.

Et managerspils scope omfatter gruppernes og turneringernes medlemmer samt det valgte eget hold, hvis det er konfigureret. Et konkurrencescope omfatter kun den valgte konkurrences medlemmer. Hold deduplikeres inden for scopet, og opdateringen ændrer ikke gruppernes medlemmer eller tilføjer tilfældige hold fra anden cache.

Disse opdateringsknapper starter kørslen ved det eksplicitte klik. Det udvidede Rundecenter har desuden det cachebaserede planlægningspreview beskrevet nedenfor. Under indhentningen hentes nødvendige metadata og seneste spillerdata, hvorefter afsluttede spillerunder med manglende eller utilstrækkelige data indhentes i stigende rækkefølge. En afsluttet runde genbesøges, hvis den gemte statistik er foreløbig, har ukendt status eller blev hentet før rundens sluttid. Allerede bekræftet komplette historiske runder genbruges. Holddata hentes for scopets unikke hold; et holds rundehistorik følger med kildens holddata.

Ny metadata eller seneste spillerdata kan afsløre runder, som det første preview ikke kendte. Derfor genvurderes de resterende trin under kørslen. Manglende runder udfyldes med kildedata; der interpoleres ingen værdier. Historiske spillerhentninger opretter ikke nye statusalarmer.

Fremdriften tilhører en worker, og en lokal journal under `data/refresh-runs/<run-id>/run.json` gør det muligt at se kørslen igen efter en browsergenindlæsning. Kun én worker kan opdatere samme datarod ad gangen. Polling starter ingen hentning og skriver ikke journalen.

Rundecentrets **Start opdatering** bruger den samme worker som de øvrige opdateringsknapper, inklusive fastfrysning af eksisterende turneringskampe før hentning. Et manifestretry behandler kun de fejlede kilder. Den ønskede spillerunde gemmes særskilt fra en eventuel ældre fallback-cache, så et retry ikke kan godkende den forkerte runde.

**Stop efter denne datakilde** anmoder om stop mellem kildeoperationer; et allerede igangværende kald kan afsluttes først. En genindlæst browser starter ikke en afbrudt kørsel automatisk. Efter appens proces er stoppet, kræver fortsættelse et klik på **Genoptag / prøv fejlede trin igen**. Afsluttede delresultater og manifestreferencer bevares, og et kildetrin med usikkert udfald skal kontrolleres ved genoptagelse.

**Download JSON** henter en samlet fil fra lokal cache for det åbne managerspil eller den åbne konkurrence. **Opdater før download** starter først en afgrænset opdatering. Når den er færdig, vises **Download opdateret JSON** direkte ved kørselsresultatet; en delvis kørsel tilbyder **Download JSON med kendte mangler**. Valget om spillerhistorik følger med kørslen. Eventuelle mangler beskrives i eksporten. Se [Dataportabilitet](data-portability.md) for indhold og kildeoplysninger.

## Overblik og konkurrencesider

Forsiden viser aktive spil i én sammenlignelig tabel med konkurrencer, hold, senest gemte runde, datastatus, ældste datakilde og næste handelsfrist. Spil åbnes i samme browserfane. **Åbn spil** og sidebaren giver også tastaturadgang.

Spillets Overblik viser spillerantal, gemte runder, kildealder, eget hold samt et udsnit af stillinger og kampe. **Ligaer og turneringer** er den særskilte liste over konkurrencer og formater. Rundecenterets detaljer er fortsat tilgængelige under **Datadetaljer og rundecenter**.

Turneringssiden viser stilling og kampe før den sammenklappede Datastatus. Foreløbige gamle snapshots beskrives som afventende afsluttende data; de bruges ikke som bevis for, at runden stadig er i gang. Et valgt historisk slutpunkt afgrænser også historikgraf og datatabel.

Historikgrafer har kun hele runder på x-aksen og bevarer huller i datagrundlaget. Værdigrafen bruger en tydeligt mærket, tilpasset y-akse og tilbyder **Ændring fra første runde**, beregnet fra hvert holds første tilgængelige observation. Rankgrafer viser bedre placeringer øverst.

## Næste bedste handling og handelsvindue

**Næste bedste handling** er én prioriteret anbefaling med link til den relevante visning:

1. Hent manglende spilmetadata, hvis deadline eller handelsvindue ikke kan verificeres.
2. Opdater forældede eller manglende snapshots.
3. Gennemgå ulæste statusalarmer.
4. Gennemgå holdet, når handelsvinduet er åbent.
5. Vis ingen handling, når der ikke er noget konkret at gøre.

Handelsvinduet vises som **Lukket**, **Åbner om …**, **Åbent** eller **Lukker om …**. Starttidspunktet er inklusivt, mens lukketidspunktet er eksklusivt. Manglende schedulemetadata giver en ikke-verificeret tilstand frem for et gættet tidspunkt.

## Opdaterings-preview, fremdrift og retry

Under **Datadetaljer og rundecenter** findes det udvidede opdaterings-preview. Her bygger Hubben en `RefreshPlan` fra den eksisterende cache, før denne opdatering bekræftes. Previewet viser de valgte datakilder, hvorfor hvert trin er valgt, og hvor mange unikke hold der omtrent bliver hentet. Hold deduplikeres på tværs af grupper. Previewet er et øjebliksbillede: det kontakter ikke Holdet.dk, skriver ikke filer og garanterer ikke, at kilden er uændret ved den efterfølgende kørsel.

Der er tre tilstande:

- **Opdater alt** vælger metadata, spillere og relevante hold.
- **Opdater kun forældede data** vælger manglende data, data der er mindst 24 timer gamle, og data ældre end den senest passerede schedulemilepæl. En afsluttet runde uden bekræftet komplette data er også forældet.
- **Prøv kun fejlede deltrin igen** bygger en ny plan fra det seneste valgte manifest og medtager trin med en retrybar fejl. Når metadata, spillere eller hold genhentes, køres den nødvendige idempotente efterbehandling også igen; øvrige tidligere succeser bæres med som genbrugte resultater.

Efter bekræftelse vises fremdrift pr. datakilde med antal afsluttede trin. Et trin ender som hentet, genbrugt aktuel cache, genbrugt cache efter fejl, fejlet uden cache eller sprunget over som utilgængeligt. En delvis kørsel bevarer gyldig cache og kan danne et nyt retry-preview; den starter aldrig automatisk et nyt netværksforsøg.

```mermaid
stateDiagram-v2
    [*] --> CachePreview: Byg RefreshPlan fra lokal cache
    CachePreview --> CachePreview: Skift alt / kun forældede / retry
    CachePreview --> Running: Bekræft eksplicit
    Running --> Complete: Alle valgte trin lykkes
    Running --> Partial: Mindst ét trin fejler
    Running --> Unavailable: Valgt kilde er utilgængelig
    Partial --> RetryPreview: Vælg kun retrybare manifesttrin
    Unavailable --> RetryPreview: Kilden kan prøves igen senere
    RetryPreview --> Running: Bekræft eksplicit
    Complete --> CachePreview: Byg nyt cache-only preview
```

## Datastatus og refresh-manifest

En runde kan være aktuel, foreløbig, forældet, manglende, fejlet eller ikke verificeret. **Runde afsluttet, men data bør genhentes** (`completed_needs_refresh`) er en særskilt tilstand: rundens sluttid er passeret, men mindst én kilde mangler, er hentet før afslutningen, er ufuldstændig eller har en nyere fejl. Den er ikke det samme som et endeligt resultat.

`RefreshManifest` forklarer den eksplicitte kørsel pr. trin. Det gemmer start/slut, kilde og begrundelse, data- eller cachereference, fejl, metadataændringer samt run- og retryrelationer. UI'et oversætter til:

- **Lykkedes**: nye data blev hentet.
- **Genbrugte cache**: en aktuel cache blev bevidst genbrugt.
- **Fejlede, cache genbrugt**: hentningen fejlede, men en kompatibel cache findes.
- **Fejlede uden cache**: der er intet sikkert fallback.
- **Ikke tilgængelig**: trinnet kunne ikke køres med den kendte kontekst.
- **Annulleret**: trinnet blev ikke afsluttet, fordi kørslen blev stoppet; det kan vælges igen ved en eksplicit genoptagelse.

Indhentning af historiske runder og annullerede kørsler skriver schema 3. Formatet bevarer schema 2's trinprovenance og tilføjer den ønskede runde (`requested_round`) og status for annullerede trin. Store-laget læser fortsat schema 1, 2 og 3. Den eksisterende opdatering uden historik eller annullering kan fortsat skrive schema 2.

Schema 2 og 3 gemmer både et samlet resultat (`complete`, `partial` eller `failed`) og hvert trins oprindelige run-ID, så genbrug og retry kan forklares uden at følge en kæde af filer. Hvis selve manifestskrivningen fejler, bevares det komplette manifest og kan skrives igen lokalt uden at gentage allerede afsluttede netværkshentninger. Den workerstyrede indhentning gemmer et ventende manifest sammen med kørselsjournalen. Et schema-1-manifest normaliseres i hukommelsen med `not_recorded` for trin, som det gamle format ikke registrerede, og omskrives ikke ved læsning. Korrupte manifestfiler isoleres med en advarsel, så andre gyldige kørsler fortsat kan vises.

## Rundens afvigelser og sammenligning

**Rundens afvigelser** er én samlet, forklarlig liste med stabile ID'er, gammel/ny værdi og links til berørte hold, når de findes. Den kan indeholde:

- store rangspring;
- nye skader eller statusændringer;
- klubskifter;
- manglende hold eller snapshots;
- ændringer i regler eller schedule fra uforanderlige metadatarevisioner.

Kategori og grænse for store rangspring kan justeres, men lever kun i den aktuelle Streamlit-session og gemmes ikke i brugerindstillinger. Grænsen begrænser kun rangspring; andre valgte afvigelsestyper skjules ikke af den.

Rundesammenligningen viser den valgte runde mod den nærmeste tidligere runde, som findes i lokal cache—eksempelvis runde 3 mod runde 2. Hvis runde 2 mangler, bruges den seneste tidligere tilgængelige runde og betegnes tydeligt. Der hentes eller estimeres ingen manglende værdier.

## Time machine: latest-corrected read-only

Time machine gengiver Rundecenter for en valgt afsluttet runde ud fra de nyeste lokalt tilgængelige, korrigerede fakta. Det er en **latest-corrected read-only** visning, ikke et arkiv af præcis det, brugeren så på et historisk klokkeslæt.

Senere snapshots, metadatarevisioner eller rettede manager-events kan derfor ændre visningen af en gammel runde. Valg af runde foretager ingen fetch, publicerer ingen events og omskriver ingen historik. Manglende historiske fakta vises som manglende eller ikke tilgængelige frem for at blive rekonstrueret ved gæt.

## Gruppematrix

Matrixen viser alle medlemmer i den valgte gruppe med placering, afstand til lederen og næste modstander. For almindelige grupper kommer placering og afstand fra den valgte rundes totaler, og næste modstander vises som **Ingen kampplan**. For turneringer bruges den publicerede turneringsstilling og næste fixture; bye, afventer parring, ikke publiceret, elimineret og afsluttet er særskilte tilstande.

Matrixen følger det valgte gruppe- og rundescope. Manglende hold bliver stående som manglende rækker i stedet for at blive udeladt eller estimeret.

## Rundens historie og delbar HTML

`RoundStory` bevarer sin eksisterende tekst og tilføjer typed `RoundStoryFact`-fakta. Hvert faktum har stabilt `fact_id`, kilderunder, status (`final`, `preliminary` eller `unavailable`), genereringstidspunkt, forklaring, eventuel værdi/enhed og typed referencer til berørte hold. Historien kan derfor forklare rundevinder, føringsskifte, comeback, nærmeste duel og streak ud fra de samme beregnede datapunkter som UI'et.

Den selvstændige HTML-version er escaped, har indlejret CSS, ingen scripts og ingen eksterne assets. Når en holdreference kan valideres, kan den vise to links:

- et HTTPS-link til det korrekte locale og managerspil på Holdet.dk;
- **Åbn i lokal Hub**, når UI'et leverer en fuld URL på `localhost` eller `127.0.0.1`.

Core-biblioteket kender ikke dashboardets routes og modtager derfor allerede byggede lokale URL'er fra UI'et. Lokale Hub-links virker kun, mens Hubben kører. Alle links åbnes med `noopener noreferrer`. Filnavnet er `rundens-historie-<slug>-runde-<N>.html`.

## Relateret dokumentation

- [Klienter](clients.md) beskriver navigation, deeplinks og session state.
- [Datahentning](data-retrieval.md) beskriver HTTP-, cache- og fejlprincipper.
- [Datalagring](data-storage.md) beskriver stier, skemaer og kompatibilitet.
- [Dataportabilitet](data-portability.md) beskriver eksport- og rapportkontrakten.
- [Tests](testing.md) beskriver verifikation og acceptkommandoer.
