# Managers og sæsoner

Managers er den globale karriere- og sammenligningsdestination. Visningen samler data på tværs af eksisterende managerspil, grupper, turneringer og sæsoner uden at hente fra Holdet.

## Faner og deeplinks

Managers indeholder:

- **Rangliste**: den redigerbare Hall of Fame-pointprofil sammen med Elo;
- **Medaljer og rekorder**: guld, sølv, bronze, titler, podier, træskeer og streaks;
- **Sammenlign**: officielle kampe og deduplikerede fælles grupperunder;
- **Sæsoner**: manuelle samlinger af grupper og turneringer;
- **Identiteter**: forslag, manuel samling, omdøbning og ophævelse.

Nye links bruger `/managers` med `manager`, `opponent` og `season` som query-parametre. `?view=hall-of-fame` er en kompatibilitetsroute, som viderestiller til `/managers` og bevarer den øvrige kontekst.

## Manageridentitet

`ManagerProfile` har et stabilt `manager_id`, visningsnavn, identitetsnøgler og kendte officielle profil-URL'er. Automatisk prioritet er:

1. `owner_user_id`;
2. `account_user_id`;
3. autoritativ kontonøgle;
4. et uløst fallback til visning.

Navne bruges ikke til automatisk at slå to personer sammen. Den deterministiske identitetsgraf forbinder kun autoritative nøgler, som er observeret samtidigt. En manuel profil kan samle hele autoritative komponenter; `manual_identity_keys` registrerer brugerens forbindelser. Merge sker ind i den valgte profils stabile ID, omdøbning ændrer aldrig ID'et, og kun manuelle forbindelser kan ophæves. Den samme identitetsnøgle må kun tilhøre én profil, og et fælles autoritativt Holdet-bruger-ID kan ikke fordeles mellem profiler.

Schema-1 `ManagerAlias` læses i hukommelsen som en profil. Filen forbliver schema 1, indtil brugeren gemmer en profil eller en anden Hub-indstilling eksplicit.

## Ratingperioder og Elo

En ratingperiode er én komplet runde i ét managerspil identificeret ved `(locale, game_slug)`:

- managerens bedste hold vælges efter rundevækst, derefter total og stabilt hold-ID;
- kun managers, som delte mindst én gruppe, sammenlignes; puljer i et flergruppespil behandles hver for sig;
- samme managerpar tæller højst én gang pr. spil/runde;
- selvopgør fjernes;
- alle par sammenlignes på `RoundSummary.change`;
- rating før perioden bruges til alle forventninger i perioden.

Startværdien er 1500 og `K=32`. Managerens periodedelta er `K × (gennemsnitligt faktisk resultat − gennemsnitligt forventet resultat)`. En uafgjort giver 0,5. Perioder sorteres efter rundens officielle sluttid; snapshotets tidspunkt er markeret fallback. Ratingen er foreløbig indtil fem perioder. Ranglisten viser manglende rang og Elo som `–`, og Elo afrundes konsekvent til nærmeste heltal med halve værdier væk fra nul.

## Medaljer, rekorder og awards

Karrierestatistik omfatter guld, sølv, bronze, titler, podier, træskeer, rundesejre, længste sejrsstreak, runder på førstepladsen og længste sammenhængende føringsstreak.

Awardmodtagere er entydige. Konkurrencens sportslige tie-breakers bruges først; stabilt manager-ID er sidste deterministiske fallback. En bronzekamp bestemmer bronze, når templatekonfigurationen bruger den. Uden bronzekamp bruges den bedst rangerede tabende semifinalist.

Runde-awards er:

- **Største comeback**: flest vundne gruppeplaceringer siden forrige komplette runde, derefter højeste vækst og bedste nye placering;
- **Højeste vækst**: højeste `RoundSummary.change`;
- **Tætteste duel**: mindste absolutte forskel i et publiceret fixture, ellers blandt fælles managerpar i gruppen.

`RoundStory` genereres lokalt fra de samme facts. Teksten er deterministisk og kan omtale rundevinder, føringsskifte, comeback, nærmeste duel og streak. Ufuldstændige runder vises som foreløbig preview og fryses ikke. De typed, forklarlige fakta og den delbare HTML-kontrakt er beskrevet i [Rundecenter og daglig arbejdsgang](round-center.md).

## H2H

`ManagerHeadToHead` har to adskilte spor:

| Spor | Enhed |
| --- | --- |
| Officielle kampe | Et publiceret fixture eller ét samlet fler-runders knockoutopgør |
| Fælles grupperunder | Samme deduplikerede managerpar og runder som Elo |

Visningen kan afgrænses til sæson, managerspil og konkurrence. Den viser V-U-T, samlet vækst, største sejr, nærmeste møde og en tidslinje. Officielle og fælles runder blandes aldrig i samme tæller, og ens slugs på forskellige locales holdes adskilt.

## Sæsoner

En `SeasonDefinition` indeholder ID, navn, valgte gruppe-/turnerings-ID'er og valgfrit arkiveringstidspunkt. Sæsoner sammensættes manuelt af eksisterende konkurrencer. `SeasonStore.update` kan omdøbe sæsonen og ændre konkurrencerne uden at ændre ID eller arkivstatus.

`build_season_standings` filtrerer råevents og genbruger den globale `HallOfFameScoreProfile`:

- top-4 gruppepoint;
- turneringsplaceringer;
- bonus for rundesejr.

Ændring af pointprofilen genberegner alle visninger uden at ændre råevents. En gruppe i en aktiv sæson kan arkiveres, men må ikke slettes, før den er fjernet fra sæsonen.

## Eventrevisioner

Eventledger schema 2 er append-only. `ManagerEvent` er den kanoniske in-memory-type, mens `HallOfFameEvent` er det bevarede kompatibilitetsnavn. Hver revision bærer logisk event-ID, revision, eventuelt `supersedes_revision`, konkurrence/revision, runde, eventuelt match-ID, tidspunkt og fulde placeringer med valgfrie identitetsnøgler.

Navigation bygger kun previews. Eksplicit refresh, arkivering eller en eksplicit historikgenopbygning må publicere events. Rettelser gemmes som nye revisioner, og læsning vælger den seneste. Aktive eventrevisioner remappes gennem de aktuelle managerprofiler ved læsning uden at omskrive ledgeren. Legacy Hall of Fame schema 1 læses som revision 1 og bruger sit gamle manager-ID som identitetskandidat.

## Officielle links

Hubben genbruger:

- managerspillets kanoniske URL;
- holdets `source_url`;
- managerens kendte `profile_urls`;
- gruppens manuelt gemte `official_url`.

En gruppe- eller miniliga-URL skal bruge HTTPS, hosten `www.holdet.dk`, ingen credentials og spillets locale-prefix. Hubben gætter aldrig en konkret grupperute.
