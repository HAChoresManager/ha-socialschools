# Changelog

Alle noemenswaardige wijzigingen in dit project worden in dit bestand bijgehouden.

Het formaat is gebaseerd op [Keep a Changelog](https://keepachangelog.com/nl/1.1.0/)
en dit project volgt [Semantic Versioning](https://semver.org/lang/nl/).

## [Unreleased]

### Gewijzigd

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
