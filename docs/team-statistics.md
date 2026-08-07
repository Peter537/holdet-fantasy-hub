# Holdstatistik

Holdstatistik viser ét fantasyhold ad gangen. Den findes som selvstændig side, som managerspilsfane og som kontekstuel detaljevisning fra grupper. Standalone-siden vælger managerspil, hold og runde; de kontekstuelle visninger genbruger de valg, som allerede er foretaget.

## Hold og runde

Holdet kan komme fra lokal cache, en gruppes faste medlemmer, den aktuelle sessions eksplicitte kontoopdagelse, en direkte `/fantasyteams/<id>`-URL eller et numerisk team-ID. **Find hold på konfigurerede konti** kontakter hver offentlig profil individuelt og tilføjer ikke automatisk fundne hold til managerspil eller grupper.

Netværk bruges kun af **Find hold**, **Hent hold**, **Opdater hold** eller **Prøv igen**. Ved fejl bevares seneste kompatible cache. Til rundesammendrag bruges det nyeste snapshot, som indeholder runden; en historisk opstilling vises kun fra et snapshot gemt præcis i den runde.

## Paneler

Efter valg af hold og runde vises:

- **Overblik** – totalværdi/point, bank, rundevækst, rang, rangbevægelse og tilgængelige bonusfelter.
- **Holdopstilling** – navn, hold/land, position/kategori, pris/point, vækst, købsrunde, rolle og status.
- **Transferlaboratorium** – en sessionsbaseret simulation fra det viste holdsnapshot.
- **Historik** – værdi eller point, rundevækst, samlet rang og beregnet grupperang.
- **Ændringer** – rang, værdi, point og relevante trupændringer mod foregående tilgængelige runde.
- **Eksport** – komplet snapshot eller valgt runde i TXT, JSON, Markdown, CSV, XLSX og valgfri Parquet.

Managerspillets separate **Analyse → Beslutninger** bruger samme cache til faktisk kaptajnbonus, verificerede alternative kaptajnscenarier, bankens rente/break-even, transferregnskab, **hvis jeg ikke havde handlet** samt bedste og værste transfer. Et standardhold gemmes pr. spil; analysevælgeren kan midlertidigt skifte hold uden at ændre standarden.

Ranggrafer vender aksen, så førsteplads står øverst. Manglende runder vises som huller. Grupperang beregnes kun, når gruppens sammenlignelige snapshots findes.

## Transferlaboratorium

Laboratoriet starter fra det viste teamsnapshot og et spillersnapshot for samme spil og runde. Scenariet lever kun i `st.session_state`: det skriver aldrig til Holdet, snapshots, Hub-indstillinger eller grupper.

Bank beregnes som startsaldo plus salg minus køb minus gebyr. Gebyret rundes op til nærmeste hele værdienhed. Kontraktforbrug er antallet af udskiftede pladser. Snapshotets resterende kontrakter bruges først; ellers kan basis/guld-preset og manuel saldo vælges.

| Profil | Trup og begrænsninger | Gebyr / kontrakter |
| --- | --- | --- |
| Fodbold | 11; 1 målmand, 3–5 forsvarere, 3–5 midtbaner, 1–3 angribere; maks. 4 fra samme klub | 1 %; basis 3; fri i runde 1 |
| Tourspillet | 8 ryttere; maks. 2 fra samme hold | 1 %; basis 8; fri i runde 1 |
| Motor Manager | 4 kørere, 2 konstruktører og 1 pitcrew | Intet gebyr; basis 0/guld 25; fri til og med runde 2 |
| Golf Manager | 15 spillere; 3 i hver af 5 kategorier | Intet budget eller gebyr; basis 0/guld 50; fri i runde 1 |

Deaktiverede eller inaktive spillere kan ikke købes. Skader og karantæner giver advarsler. Ukendte fremtidige formater kan undersøges, men får ikke en falsk godkendelse.

Disse legacy-formatprofiler er fortsat bagudkompatible i Transferlaboratoriet. Beslutningscenteret aktiverer derimod kun regelafhængige resultater fra en præcis, auditeret `GameRuleProfile` for det konkrete spil og den konkrete sæson. Et formatmatch alene tæller ikke som verificeret.

### Regelgyldighed og datasikkerhed

Resultatet viser to uafhængige vurderinger:

- `valid`/`invalid` beskriver kun budget, kontrakter, formation, klubgrænser og øvrige regler.
- `final` betyder, at team- og spillersnapshot matcher samme afsluttede runde og begge er hentet efter rundeafslutning.
- `preliminary` betyder, at runden er i gang, status er ukendt, eller afsluttede data ikke er genhentet endnu.
- `unverified` betyder, at datarunder eller regelprofil ikke kan forenes.

Et foreløbigt scenarie kan simuleres, men vises aldrig som en grøn endelig godkendelse.

## Ændringer og historik

Holdets Ændringer vælger det seneste snapshot, der dækker den valgte runde, og den foregående tilgængelige runde. Begge kilder vises, og manglende forgænger giver en tom tilstand. Managerspillets Historik kan vise tværgående holdtrends med valgfrit gruppefilter; en gruppehistorik anvender gruppen direkte.

## Eksport

**Komplet snapshot** indeholder seneste overblik, aktuel opstilling og komplet historik. **Valgt runde** indeholder rundesammendrag og kun en opstilling, hvis den er gemt præcis i runden.

```text
%LOCALAPPDATA%\Holdet Fantasy Hub\exports\teams\<game-slug>\...\team-round<round>_<MMDD>_<HHmmss>[_N].<format>
```

Se [Klienter](clients.md) for CLI-eksempler.

Formler og begrænsninger er beskrevet i [Analyse- og beslutningscenter](decision-analysis.md).
