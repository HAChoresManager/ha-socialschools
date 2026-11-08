# Changelog

Alle noemenswaardige wijzigingen in dit project worden in dit bestand bijgehouden.

Het formaat is gebaseerd op [Keep a Changelog](https://keepachangelog.com/nl/1.1.0/)
en dit project volgt [Semantic Versioning](https://semver.org/lang/nl/).

## [Unreleased]

## [0.2.1] - 2026-11-08

### Opgelost

- Hele-dag-activiteiten in de agenda duurden een dag te lang. Social Schools geeft een hele dag
  door als middernacht UTC (`2026-11-26T00:00:00Z`) met een einde dat al exclusief is; de
  integratie rekende dat eerst om naar Nederlandse tijd, zag om 01:00 geen middernacht en telde er
  een dag bij. Een hele dag is nu een kalenderdatum: de datum zoals die in de tekst staat, zonder
  omrekening naar een tijdzone (met `Z`, met een offset als `+01:00` of zonder tijdzone). Een
  einde om 00:00 telt niet mee, een ander einde wel, en een activiteit duurt altijd minstens één
  dag. De studiedag van 26 november staat nu alleen op 26 november; de kerstvakantie vanaf
  21 december 2026 loopt tot en met zondag 3 januari. Activiteiten met een tijd zijn ongewijzigd.

### Gewijzigd

- `socialschools.open_bericht` toont een bericht dat in de feed staat met de tekst uit de feed en
  doet daarvoor geen verzoek meer (de probe liet zien dat de feed de volledige tekst bevat). Alleen
  een bericht dat inmiddels uit de feed is gevallen, of waarvan de feedtekst ingekort lijkt (de
  tekst eindigt op `…` of `...`, of `FETCH_POST_DETAILS` staat aan), wordt één keer opgehaald met
  `GET communityposts/{id}` en daarna uit het geheugen getoond. Zo gaan er zo min mogelijk
  detailverzoeken naar Social Schools; of zo'n verzoek daar als "bekeken" telt, is niet bekend.
- `open_bericht` opent nu ook een bericht dat uit de feed is gevallen, als de integratie het
  eerder in de feed zag (de laatste 500 bericht-id's per school, ook na een herstart). Een onbekend
  id geeft zoals voorheen de melding dat het bericht niet in de huidige feed staat, zonder verzoek.
- CI: de validatie compileert ook `scripts/release.py` met Python 3.12.

### Toegevoegd

- `scripts/probe.py --kinderen-vorm` beschrijft ook de groepen van de kinderen: het type en de
  velden van de eerste groep, of een `groupId` (of `id`) uit de communities van de feed in de
  groepen van een kind voorkomt en bij hoeveel berichten, of een groepsnaam gelijk is aan een
  communitynaam, en of `yearClass` gelijk is aan een communitynaam (exact, en zonder verschil in
  hoofdletters en spaties). Alleen typen, veldnamen, ja/nee en aantallen.
- Releases: bij elke push naar `main` maakt `.github/workflows/release.yml` voor een nieuwe versie
  in `manifest.json` de tag `v<versie>` en een GitHub-release met de sectie uit deze changelog.
  `v0.2.0` wordt eenmalig achteraf aangemaakt op de merge-commit van 0.2.0. HACS toont daardoor
  versienummers in plaats van een commit.

## [0.2.0] - 2026-11-07

### Toegevoegd

- Gelezen per persoon: optie *Lezers* (personen). Een bericht blijft nieuw tot alle lezers het
  gelezen hebben. Dit is uitsluitend administratie in Home Assistant; er gaat niets naar Social
  Schools. Bestaande berichten tellen bij de eerste run (en voor een later toegevoegde lezer) als
  gelezen.
- Acties `socialschools.markeer_gelezen` (met `gelezen: false` om een markering weg te halen),
  `socialschools.markeer_alles_gelezen` en `socialschools.open_bericht`. Zonder `persoon` geldt
  de persoon van de ingelogde gebruiker; een kiosk-gebruiker zonder persoon krijgt de melding
  "Kies wie het bericht gelezen heeft".
- Sensor *Ongelezen* (aantal, `eerste`, `per_persoon`) en sensor *Geopend bericht* (volledige
  tekst tot 8000 tekens, binnen het attribuutbudget; standaard het nieuwste bericht).
- De lijst `berichten` en het event `nieuw_bericht` krijgen `kinderen`, `schoolbreed` en `url`
  (deeplink naar het bericht in de webapp); de lijst ook `gelezen_door`, `nog_lezen` en `nieuw`.
- Optie *Kinderen en groepen* (`Kind=6A`, meerdere groepen met komma's) om berichten aan kinderen
  te koppelen; exacte vergelijking per groepsnaam, zonder hoofdlettergevoeligheid.
- `scripts/probe.py --agenda-vorm` en `--kinderen-vorm`: tonen de vorm van agenda-tijden en van
  de kinderen in het account, zonder inhoud.
- MIT-licentie (`LICENSE`).
- `SECURITY.md`: kwetsbaarheden melden via private vulnerability reporting van GitHub.
- Dependabot werkt de GitHub Actions wekelijks bij (`.github/dependabot.yml`).

### Gewijzigd

- CI: alle GitHub Actions zijn vastgepind op een commit-SHA, met de versie als commentaar.
- JSON-antwoorden worden gedecodeerd met één gedeelde helper (`payload.py`) die de grootte
  begrenst en de nestdiepte (maximaal 64 niveaus) controleert met een lineaire scan vóór
  `json.loads`. De uitkomst hangt daardoor niet meer af van de recursiegrens of de stackgrootte
  van de Python-interpreter. Alleen UTF-8 wordt geaccepteerd.
- Een geheel getal in een JSON-antwoord mag hoogstens 640 cijfers hebben; de decoder controleert
  dat zelf, net als de lengte van een `expires_in` als tekst en van `Content-Length`. Ook deze
  uitkomst (en de rekentijd) hangt daardoor niet meer af van de interpreterinstelling
  `int_max_str_digits`.
- De API-client controleert per endpoint de vorm van het antwoord: account, berichtenfeed en
  bericht moeten een JSON-object zijn, de agenda een lijst of object. Een ander antwoord geeft
  een fout ("unexpected JSON shape") in plaats van dat het wordt doorgegeven.
- CI: de HACS-validatie wordt overgeslagen zolang de repository privé is.
- De opslag (`.storage/socialschools.<entry_id>`) gaat naar versie 2 met de gelezen-markeringen;
  een opslag van 0.1 wordt automatisch omgezet.
- Diagnostische gegevens tonen van lezers en kinderen alleen het aantal.

### Opgelost

- Een half emoji-teken uit de API (een losse UTF-16-surrogaat, in het JSON-antwoord bijvoorbeeld
  `\ud83d`) in een bericht, agenda-item, schoolnaam of weergavenaam wordt vervangen door het
  vervangingsteken U+FFFD (`�`). Voorheen konden de sensoren dan niet worden bijgewerkt en kon
  Home Assistant de toestand niet als JSON opslaan.

## [0.1.0] - 2026-11-04

### Toegevoegd

- Eerste uitgave van de alleen-lezen Social Schools-integratie voor Home Assistant
  (`socialschools`, HACS-compatibel, `integration_type: hub`, `iot_class: cloud_polling`).
- Instellen via de UI met e-mailadres en wachtwoord: inloggen zoals de officiële iOS-app
  (client-id `eu.socialschools.ios`, PKCE-codewisseling). Het wachtwoord wordt nooit bewaard;
  alleen het refresh token staat in de configuratie-entry en wordt bij rotatie automatisch
  bijgewerkt.
- Opnieuw verifiëren (reauth) met alleen het wachtwoord wanneer Social Schools het refresh
  token afwijst; tijdelijke storingen starten nooit een reauth.
- Opties met automatische herlaad: interval van de berichten (10–1440 minuten, standaard 15),
  agendahorizon (7–365 dagen, standaard 90) en aantal berichten in de lijst (1–20, standaard 10).
- Per school (ouderrol) één apparaat "Social Schools · <school>" met vier entiteiten:
  sensor *Laatste bericht* (titel als toestand; attributen `post_id`, `groep`, `auteur`,
  `geplaatst`, `tekst`, `afgekapt`, `bijlagen`, `reacties`), sensor *Berichten* (aantal van de
  afgelopen 7 dagen; attribuut `berichten` met `post_id`, `titel`, `groep`, `auteur`, `geplaatst`,
  `tekst_kort`), event *Nieuw bericht* (eventtype `nieuw_bericht`) en agenda *Agenda*.
- Detectie van nieuwe berichten op basis van bericht-id's en een tijdstempel in
  `.storage/socialschools.<entry_id>`; na installatie of een herstart worden bestaande berichten
  stilletjes als gezien geregistreerd, zodat er geen oude meldingen afgaan.
- Schoolagenda van 7 dagen terug tot de ingestelde horizon vooruit, elk uur ververst, met echte
  hele-dag-activiteiten en tijden in Europe/Amsterdam; `calendar.get_events` werkt ook tijdens
  een storing op de laatst opgehaalde agenda.
- Read-only API-client met een whitelist van vier GET-paden, bearer-token-beheer met één
  hernieuwingspoging bij HTTP 401 en foutafhandeling die nooit tokens, cookies, namen of
  antwoordteksten logt.
- HTML van berichten en agendabeschrijvingen wordt omgezet naar platte tekst (links blijven
  als `tekst (url)` staan, lijsten krijgen een `- `-prefix).
- De attributen `tekst`, `berichten`, `bijlagen` en `tekst_kort` (event) worden niet in de
  recorder-database opgeslagen; diagnostische gegevens bevatten alleen aantallen, tijdstippen en
  school-id's.
- Nederlandse en Engelse vertalingen (entiteitsnamen, instelformulieren, foutmeldingen).
- `scripts/probe.py`: live protocolcontrole zonder Home Assistant die alleen ja/nee-antwoorden,
  aantallen, tekstlengtes en veldnamen afdrukt.
- Testsuite met `pytest-homeassistant-custom-component` (uitsluitend synthetische gegevens) en
  CI met hassfest, HACS-validatie, ruff en pytest.
