# Holdrådgivning fra CLI og Codex

Skillens kildefiler ligger i [skills/holdet-advisor](../skills/holdet-advisor/SKILL.md). Den samler research, personlige strategier og Python-validering for nye eller eksisterende hold. Den køber eller sælger aldrig på Holdet.dk.

CLI'en køres direkte med projektets Python. Der kræves hverken en installeret holdet-kommando eller en kørende hjemmeside:

    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py --help
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py data paths --json
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py games
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py context example-game --history

Kommandoeksemplerne bruger det opdigtede spilnavn example-game. Erstat det med et gemt spil fra games.

## Data og afgrænsning

Games viser registrerede spil, konkurrencer, kendte hold, arkivstatus og eventuelle strategier. Context bruger den samme samlede eksportbuilder som hjemmesidens Download JSON og bevarer hele det kanoniske spillerkatalog med stabile ID'er, statistik og numeriske værdier. Den læser kun lokal cache.

Vælg én konkurrence med --competition ID eller bestemte hold med gentaget --team ID. Standardscopet indeholder konkurrencernes hold samt spillets valgte eget hold. Andre cachede hold kan vælges eksplicit. --history medtager seneste korrigerede spillersnapshot pr. lokal runde; --analysis tilføjer beskrivende nøgletal med provenance.

--output FIL gemmer JSON eksplicit. Ellers kommer JSON på stdout og diagnostik på stderr. Manglende data og uverificerede kilder bevares synligt. De ældre players/teams-kommandoer henter og gemmer data; deres tabeleksport er ikke den komplette rådgivningskontekst.

Dataplaceringer kan vælges uafhængigt:

    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py --data-dir "C:\Data\Holdet" --config-dir "C:\Config\Holdet" games

Data-roden indeholder data/ og exports/. Konfigurationsmappen indeholder selve JSON-filerne. Hvis Windows omdirigerer standardstien til en sandbox, og desktopcachen findes, vælges desktopcachen med en advarsel. Eksplicitte parametre og HOLDET_DATA_DIR har forrang.

Ved denne automatiske omdirigering bevares en eksportmappe angivet med HOLDET_OUTPUT_DIR. Uden denne indstilling bruges desktopcachens exports/.

## Opdatering

    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py refresh example-game
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py refresh example-game --execute
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py refresh --all --execute

Den første kommando viser kun et cachebaseret preview. --execute bruger hjemmesidens worker med lås, indhentning af manglende historik, turneringsefterbehandling og varig kørselsjournal. --all udelader arkiverede spil. --force genhenter også aktuelle kilder. Kommandoen venter på afslutning og udskriver ét samlet resultat; delvise fejl giver exitkode 1.

Journalen læses med refresh --status ID, genoptages med --resume ID eller anmodes stoppet med --cancel ID. Et nyt spil kan tilføjes eksplicit med games --add URL --name NAVN.

Journalhandlingerne kan køres, selv om gruppekonfigurationen ikke kan læses. Selve workeren kræver stadig gyldig konfiguration for at hente data. Ulæselige spilmetadata giver en synlig advarsel og medtages som en metadataopdatering i planen. En optaget worker giver en kort fejl på stderr og exitkode 1.

## Regler og strategier

    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py rules example-game
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py rules example-game --fetch
    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py rules example-game --fetch --save

Uden --fetch læses kun cache og registrerede profiler. Hentning følger det officielle iframe-dokument og læser også sammenklappede sektioner. Den gemmer kun med --save. Kildesider, HTML, fingerprints, hentetid, tabeller, sektioner og mangler bevares under data/rules/locale/spil/.

Regelkommandoens JSON har en warnings-liste. Ulæselige valgfrie spilmetadata giver metadata_unreadable i listen og på stderr; en vellykket regelhandling returnerer stadig sit resultat. Verifikationsstatus afhænger af de tilgængelige regelkilder. Fejl ved hentning, profilvalidering eller lagring giver fortsat exitkode 1.

Komplet ekstraktion er ikke automatisk regelverifikation. En gennemgået profil registreres med rules SPIL --audit PROFIL.json --review-note BEGRUNDELSE og bindes til det gemte kildedokument. En ændret kilde kræver ny gennemgang. Der medfølger ingen sæsonprofiler; alle spil kræver lokalt gennemgåede regler, og sportsdefaults etablerer ikke verifikation.

Seneste kilde og gennemgang vælges efter deres tidsstempler med tidszone, også i ældre filer fra skiftet til vintertid. En beskadiget historisk HTML-kopi blokerer ikke en intakt aktuel kilde. Kan rækkefølgen eller den aktuelle regelverifikation ikke fastslås, bevarer context hold- og spillerdata med uverificerede regler og advarslen rules_evidence_unreadable.

Kan en arkiveret turneringsdefinition ikke læses, udelades den fra context med en kildeadvarsel i JSON. Advarslen viser antallet af kildeproblemer uden at medtage fejltekst eller lokale filstier.

Personlige strategier ligger i config/team-strategies.json, nøglebestemt ved locale, spil og hold-ID. Strategy SPIL --team ID læser; --set FIL gemmer eksplicit. Udskiftningspolitik, kaptajnpolitik, personligt restbudget, kontraktreserve, horisont og holdidentitet kan fastlægges uafhængigt af de officielle regler.

Samtidige gemninger fra flere chats låser hele opdateringen af strategifilen, så ændringer for forskellige hold bevares. Gemning og gendannelse deler en lås ved siden af konfigurationsmappen, også når chats bruger forskellige datarødder. Gendannelse holder låsen gennem både installation og eventuel tilbagerulning. En gemning venter højst fem sekunder på låsen og giver derefter en kort fejl med exitkode 1; prøv igen, når den anden handling er færdig. Læsning opretter ingen låsefil.

Regelkilder og strategier følger med i Hub-backup og gendannelse. Regelkilder indgår også i integritetskontrol og lageroverblik.

Konfigurationens låsefil og ældre strategilåsefiler medtages ikke i backup eller integritetsindekset. Repositoryets regeltests bruger opdigtede profiler og HTML-eksempler. Officielle regelsider og deres gennemgåede profiler gemmes kun lokalt ved eksplicit handling; URL, adgangsdato og fingerprint bevarer provenancen i den lokale profil.

## Validering og skill

    .\.venv\Scripts\python.exe -X utf8 .\cli\main.py validate example-game --proposal forslag.json

--proposal - læser JSON fra stdin uden en fil. Validatoren kontrollerer opstilling, pris/bank, gebyr, kontrakter, kaptajn og personlige begrænsninger. Den viser eksponering og kan håndhæve en valgfri overlapgrænse. Exit 0 betyder valid; invalid og unverified giver exit 1. Datagrundlagets sikkerhed beskrives separat fra regelgyldighed. Prognoser er fortsat vurderinger.

Beholdte spilleres klub, position og tilgængelighed hentes fra det aktuelle spillerkatalog, mens holdsnapshotets finansielle værdier bevares. Skader og karantæner giver advarsler for alle valgte spillere, også ved uændrede eller helt nye hold.

Se skillens [CLI-reference](../skills/holdet-advisor/references/cli.md), [regelreference](../skills/holdet-advisor/references/rules-and-validation.md) og [JSON-formater](../skills/holdet-advisor/references/strategies.md) for felter og arbejdsgang.

Skillmappen kan kopieres til brugerens Codex-skillsmappe og bruges i en ny chat:

    $holdet-advisor Review my Superliga teams using their saved strategies.

Read-only-opgaver bruger cache, research og validering via stdin. De gemmer ikke profiler, ændrer ikke strategier og starter ikke opdateringskørsler.
