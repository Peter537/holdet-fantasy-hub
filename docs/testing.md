# Tests

Projektet bruger kun standardbibliotekets `unittest` til sin testsuite. Streamlit følger med website-extraen og bruges af `streamlit.testing.v1`-testene.

## Komplet lokal suite

Installer først projektet med website-afhængigheden, og kør derefter fra repositoryets rod:

```powershell
py -3.14 -m pip install -e ".[website]"
```

```powershell
py -3.14 -m unittest discover -s tests -v
```

De almindelige tests må ikke kontakte Holdet.dk. HTTP-svar leveres af små lokale fixtures eller injicerede fetch-funktioner, og filesystemtests bruger test-ejede midlertidige mapper.

## Live-smoke-tests

Live-tests er opt-in og kontakter Holdets offentlige endpoints:

```powershell
$env:HOLDET_LIVE_TESTS="1"; py -3.14 -m unittest discover -s tests -v
```

De kontrollerer aktuelle offentlige spillerpayloads for fodbold, pengebaseret og pointbaseret cykling, Motor Manager og Golf Manager. De afhænger ikke af private profiler eller fantasyhold og antager ikke ustabile, eksakte spillerantal.

Fjern miljøvariablen igen i den aktuelle PowerShell-session med:

```powershell
Remove-Item Env:HOLDET_LIVE_TESTS
```

## Testområder

| Område | Eksempler på dækning |
| --- | --- |
| URL og parsere | Normalisering, Flight-dekodning, payloadvalidering og Unicode |
| HTTP | Retries, HTTP-statusser, connection refused, proxyregistrering og redaction |
| Spillere | Formater, enheder, runder, filtre, statusser og sortering |
| Hold | Kontoopdagelse, roster, historik, ranks og inkonsistente værdier |
| Lagring | AppData-overrides, atomiske writes, snapshots, manifester og kollisioner |
| Eksport | TXT, JSON, Markdown, scopes, metadata og identiske downloadbytes |
| Grupper | Schema-kompatibilitet, managerspil, arkivering og refresh |
| Turnering | Fairness, draw seed, revisioner, bracket, H2H og historisk genberegning |
| Dashboard | Offline opstart, navigation, dialogs, cache, fejl og eksplicitte handlinger |
| Dokumentation | Links, kommandoformer, Mermaid-hegn, filnavne og privacy-regler |

## Fixtures og privatliv

Tests må ikke læse repositoryets tidligere `/config` eller en brugers AppData. Konfiguration oprettes direkte i en midlertidig mappe eller som dataclasses.

Fiktive identiteter skal bruges til:

- kontonavne og tekniske nøgler;
- profil- og bruger-ID'er;
- fantasy-team-ID'er og holdnavne;
- outputstier afledt af konti eller hold.

Offentlige sportsnavne i spillerfixtures er tilladt, fordi de ikke repræsenterer brugerens profiler eller fantasyhold. Fiktive URL'er bruges kun som parserinput og må aldrig hentes i en lokal test.

## Sideeffektfrihed

Tests skal særskilt bevise, at import, stiresolution, cacheindeksering og almindelig dashboardnavigation ikke opretter mapper, skriver filer eller starter netværkskald. Kun eksplicitte save-, export-, discovery- og fetch-handlinger må have de respektive sideeffekter.
