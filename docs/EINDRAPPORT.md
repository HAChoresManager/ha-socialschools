# Eindrapport: Social Schools voor Home Assistant (0.1.0)

Datum: 2026-11-05. Repository: `ha-socialschools`, integratie
`custom_components/socialschools` (domein `socialschools`).

## Status

- Alles uit de brief is gebouwd: e-mail/wachtwoord-login via de native app-flow
  (PKCE, client `eu.socialschools.ios`), alleen-lezen client met whitelist van
  vier GET-paden, één coordinator per account, per school één apparaat met
  vier entiteiten, opties, reauth, diagnostiek, vertalingen (nl/en), probe,
  README, CHANGELOG, `hacs.json`, CI-workflow (hassfest, HACS, tests).
- Testsuite: 769 tests, allemaal groen (`pytest`), `ruff check` schoon, de
  protocolmodules en de probe compileren onder Python 3.12.
- hassfest en de HACS-validatie draaien in CI (`.github/workflows/validate.yml`).
  In de ontwikkelomgeving kon hassfest niet als geheel worden uitgevoerd; een
  deelcontrole (translations, config_flow, json) gaf 0 fouten. De
  HACS-`brands`-controle is bewust uitgezet (`ignore: brands`) omdat het
  Social Schools-logo niet van ons is om mee te leveren.
- De probe is **niet live gedraaid** (geen inloggegevens in de
  ontwikkelomgeving). Zie "Probe" hieronder voor wat hij afdrukt en wat je ermee doet.

## Entity-id's per school

Per school ontstaat één apparaat met de naam `Social Schools · <schoolnaam>`.
Home Assistant maakt de entity-id uit de apparaatnaam plus de vertaalde
entiteitsnaam in de taal van je HA-installatie (Nederlands geeft Nederlandse
id's). Leestekens en accenten worden door HA omgezet (`·` en spaties worden
`_`, `é` wordt `e`). Voor een school "Voorbeeldschool":

| Entiteit | HA in het Nederlands | HA in het Engels |
|---|---|---|
| Laatste bericht (sensor) | `sensor.social_schools_voorbeeldschool_laatste_bericht` | `sensor.social_schools_voorbeeldschool_latest_post` |
| Berichten (sensor) | `sensor.social_schools_voorbeeldschool_berichten` | `sensor.social_schools_voorbeeldschool_posts` |
| Nieuw bericht (event) | `event.social_schools_voorbeeldschool_nieuw_bericht` | `event.social_schools_voorbeeldschool_new_post` |
| Agenda (calendar) | `calendar.social_schools_voorbeeldschool_agenda` | `calendar.social_schools_voorbeeldschool_calendar` |

Let op:

- De id's worden bij het eerste aanmaken vastgelegd en veranderen daarna niet
  meer, ook niet als je de HA-taal wijzigt. Hernoemen mag altijd; de unieke
  id's (`<profiel-id>_<school-id>_<sleutel>`) blijven gelijk.
- Wijs je het apparaat aan een ruimte toe vóórdat de entiteiten bestaan, dan
  zet HA 2026.10 standaard ook de ruimtenaam vóór de entity-id. Bij een
  gewone installatie (geen ruimte) krijg je precies de id's uit de tabel.
- De exacte schoolnaam komt uit `school.displayName` (anders `school.name`)
  van `useraccounts/current`; de probe toont alleen het aantal scholen, niet
  de naam. Controleer na installatie de apparaatnaam onder *Apparaten*.

## Attributen

`sensor.…_laatste_bericht` (toestand: titel, max 255 tekens, `unknown` zonder berichten):

| Attribuut | Inhoud | In recorder |
|---|---|---|
| `post_id` | id van het bericht (getal) | ja |
| `groep` | communitynamen, komma-gescheiden (max 255 tekens) | ja |
| `auteur` | naam van de plaatser (max 120 tekens) | ja |
| `geplaatst` | ISO 8601, Nederlandse tijd, bv. `2026-11-03T09:00:00+01:00` | ja |
| `tekst` | platte tekst, max 2000 tekens | **nee** |
| `afgekapt` | `true` als `tekst` is ingekort | ja |
| `bijlagen` | lijst bestandsnamen (max 20) | **nee** |
| `reacties` | aantal reacties | ja |

`sensor.…_berichten` (toestand: aantal berichten van de laatste 7 dagen, binnen de feed van 20):

| Attribuut | Inhoud | In recorder |
|---|---|---|
| `berichten` | lijst (standaard 10, optie 1–20) van `{post_id, titel, groep, auteur, geplaatst, tekst_kort}`; `titel`/`groep`/`auteur` max 120 tekens, `tekst_kort` max 300; samen onder ca. 12 kB | **nee** |

`event.…_nieuw_bericht` (eventtype `nieuw_bericht`): attributen `event_type`,
`post_id`, `titel`, `groep`, `auteur`, `geplaatst`, `tekst_kort`
(`tekst_kort` niet in de recorder; wel in `core.restore_state` zodat het
laatste event een herstart overleeft).

`calendar.…_agenda`: standaard agenda-attributen (`message`, `start_time`,
`end_time`, `all_day`, `location`, `description`). Per afspraak:
`summary` = titel, `description` = platte tekst, `location`, uid
`socialschools-<event-id>`. Hele dagen als datums (einde exclusief),
tijden in Europe/Amsterdam. Venster: 7 dagen terug tot de agendahorizon
(optie, standaard 90 dagen), elk uur ververst.

## Eventtype en gebruik in automatiseringen

Eventtype: `nieuw_bericht`. Per nieuw bericht één event, chronologisch.
Niets bij de eerste ophaalronde na installatie of herstart (alleen
"zaaien"); bewerkte berichten zijn niet opnieuw nieuw; nooit dubbel.

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.social_schools_voorbeeldschool_nieuw_bericht
    options:
      event_type:
        - nieuw_bericht
```

Voor een eigen dag- of weekoverzicht: `calendar.get_events`
op `calendar.social_schools_<school>_agenda` (voorbeeld in de README).

## Opties (Instellingen → Integraties → Social Schools → Configureren)

| Optie | Sleutel | Standaard | Bereik |
|---|---|---|---|
| Bijwerkinterval berichten (min) | `posts_interval` | 15 | 10–1440 |
| Agendahorizon (dagen) | `calendar_days` | 90 | 7–365 |
| Berichten in het lijstattribuut | `posts_in_list` | 10 | 1–20 |

Wijzigen herlaadt de integratie. Aanvragen per school per uur bij de
standaardinstelling: 4 feed + 1 agenda (+ ca. 1 tokenverversing per account).

## Wat er bewaard wordt

- `.storage/core.config_entries`: `username`, `refresh_token`, `client_id`,
  `user_profile_id`, titel = weergavenaam. Geen wachtwoord.
- `.storage/socialschools.<entry_id>`: per school `high_water` (tijdstempel)
  en `seen_ids` (max 500 bericht-id's). Wordt verwijderd bij *Integratie
  verwijderen*; het refresh token wordt daarbij niet ingetrokken bij Social
  Schools (de integratie doet geen schrijfaanvragen).

## Probe (`scripts/probe.py`)

Niet gedraaid in de ontwikkelomgeving. Draai hem vóór installatie vanuit de
repo-root op de machine waar de repository staat:

```sh
docker run --rm -it --network host -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -q aiohttp && python scripts/probe.py"
```

Hij vraagt e-mail en wachtwoord (nooit als argument/omgevingsvariabele) en
drukt uitsluitend af:

- `Refresh token ontvangen: ja/nee` en `expires_in: <n>`
- `Refresh werkt: ja/nee` en `Refresh token roteert: ja/nee`
- `Ouderrollen/scholen: <n>`
- `Velden van een bericht: …` (plus velden van `communities[0]`, `author`, `media[0]`)
- `Body is HTML: ja/nee`
- `Feedbody even lang als detail: ja/nee (<lengte feed>/<lengte detail>)`
- `Velden van een event: …` en `Aantal events in venster: <n>`

Wat je ermee doet:

- `Refresh werkt: nee`: niet installeren; eerst uitzoeken (de integratie zou
  na een uur om opnieuw inloggen vragen).
- `Feedbody even lang als detail: nee`: zet `FETCH_POST_DETAILS = True` in
  `custom_components/socialschools/const.py`; dan haalt de integratie per
  nieuw bericht één keer `communityposts/{id}` op (gecachet).
- Ontbreken velden (`title`, `body`, `created`, `communities`, `author`,
  `media`, of bij events `start`, `end`, `allDay`, `location`), dan moet
  `models.py` worden aangepast; de parser slaat onbruikbare items over in
  plaats van te crashen.
- Onverwacht veel scholen: elke ouderrol wordt een apparaat; rollen van
  andere typen (medewerker) worden genegeerd.

## Aannames en open punten

- GitHub-URL in `manifest.json`/README: `https://github.com/HAChoresManager/ha-socialschools`.
  Pas aan als de repo elders komt.
- Naïeve tijdstempels van de API (zonder tijdzone) worden als
  Europe/Amsterdam gelezen; de probe laat niet zien of de API `Z`/offsets
  gebruikt (alleen veldnamen). Klopt een agendatijd niet, dan zit het daar.
- Het scope-set is het live-geverifieerde set uit de referentie
  (`openid profile SocsWebApi email offline_access SocsNotificationApi`);
  inperken is één regel in `const.py` maar kan de login breken, dus niet
  blind gedaan.
- Minimale HA-versie in `hacs.json` is 2026.1.0; ontwikkeld en getest tegen
  2026.10.4. Oudere 2026-versies zijn niet getest.
- Bij een mislukte ophaalronde blijven alle entiteiten beschikbaar met de
  vorige gegevens (bewust, zodat de agenda nooit leegloopt); de status van
  de laatste ronde staat in de diagnostiek.
- Het nieuwe-berichtmechanisme mist een bericht dat exact tijdens een
  herstart van HA verschijnt (zaai-ronde). Dat is de prijs van "nooit een
  stortvloed na een herstart".
