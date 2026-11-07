# Social Schools voor Home Assistant

[![Validate](https://github.com/HAChoresManager/ha-socialschools/actions/workflows/validate.yml/badge.svg)](https://github.com/HAChoresManager/ha-socialschools/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)

Onofficiële, **alleen-lezen** integratie die de schoolberichten en de schoolagenda van
[Social Schools](https://www.socialschools.eu/) in Home Assistant brengt. Voor ouders die een
melding willen bij een nieuw bericht van school en de schoolagenda in hun dashboard en
automatiseringen willen gebruiken.

## Wat doet deze integratie?

- **Schoolberichten** – per school een sensor met het laatste bericht, een sensor met het aantal
  berichten van de afgelopen zeven dagen (met een lijst van de nieuwste berichten) en een
  *event*-entiteit die afgaat bij elk nieuw bericht.
- **Schoolagenda** – per school een agenda-entiteit (`calendar`) met de activiteiten van zeven
  dagen terug tot een instelbaar aantal dagen vooruit, inclusief hele-dag-activiteiten.
- **Alleen lezen** – de integratie doet uitsluitend vier soorten GET-aanvragen (account,
  berichtenfeed, één bericht, agenda). Ze verstuurt niets, markeert niets als gelezen en raakt
  geen gesprekken, inbox, leesbevestigingen of statistieken aan.

Niet inbegrepen: gesprekken en de inbox, leesbevestigingen, reacties plaatsen, absentie melden,
oudergesprekken inplannen, foto's downloaden. Alleen rollen van het type *ouder* worden gebruikt;
een school waar je bijvoorbeeld als medewerker bent aangemeld, verschijnt niet.

## Onofficieel – lees dit eerst

Social Schools heeft geen openbare API. Deze integratie meldt zich aan zoals de officiële
iOS-app dat doet (client-id `eu.socialschools.ios`, inloggen via het gewone inlogformulier en een
PKCE-codewisseling) en gebruikt daarna dezelfde API als de app. Dat betekent:

- Social Schools kan dit op elk moment breken door iets aan het inloggen of aan de API te
  veranderen. Verwacht dan een fout in het logboek of een verzoek om opnieuw in te loggen; met
  [`scripts/probe.py`](#de-probe-scriptsprobepy) kun je nagaan wat er nog werkt.
- Deze integratie is niet gemaakt door en niet verbonden aan Social Schools. Gebruik op eigen
  risico.
- Een extra inlogstap (bijvoorbeeld tweestapsverificatie of een andere identity provider) wordt
  niet ondersteund; het instellen stopt dan met de melding dat het inloggen niet wordt ondersteund
  (een verkeerd wachtwoord geeft daarentegen *Ongeldige aanmelding*).

## Wat wordt er opgeslagen?

| Waar | Wat | Niet |
|---|---|---|
| `.storage/core.config_entries` | de titel van de configuratie (je weergavenaam bij Social Schools), e-mailadres, profiel-id, de client-id en het **refresh token** waarmee nieuwe toegangstokens worden gehaald | je **wachtwoord**: dat wordt alleen tijdens het instellen gebruikt en nooit bewaard of gelogd |
| `.storage/socialschools.<entry_id>` | per school de ids van de al geziene berichten en één tijdstempel (voor de detectie van nieuwe berichten) | geen berichten, titels of namen |
| `.storage/core.restore_state` | zoals bij elke event-entiteit: het laatste event (met titel en korte tekst), zodat het een herstart overleeft | |
| recorder-database | de toestanden en de meeste attributen van de entiteiten | de attributen `tekst`, `berichten`, `bijlagen` en `tekst_kort` (zie [Privacy](#privacy)) |

Het refresh token geeft toegang tot je Social Schools-account. Bescherm de map `.storage` (en
back-ups daarvan) zoals je dat met andere wachtwoorden zou doen. Bij *Integratie verwijderen*
verdwijnen de entry met het token en het bestand `.storage/socialschools.<entry_id>`, maar het
token zelf wordt **niet ingetrokken**: de integratie doet geen schrijfaanvragen, dus bij Social
Schools blijft het (net als een kopie in een back-up) geldig tot het verloopt of wordt
ingetrokken. Of een wachtwoordwijziging het token ongeldig maakt, is niet geverifieerd; zeker
weten doe je dat alleen bij Social Schools zelf.

## Installatie

### Via HACS (aanbevolen)

1. HACS → *Integrations* → menu rechtsboven → **Custom repositories**.
2. Repository `https://github.com/HAChoresManager/ha-socialschools`, categorie **Integration**,
   *Add*.
3. Zoek "Social Schools" in HACS, *Download*, en herstart Home Assistant.

### Handmatig

Kopieer de map `custom_components/socialschools` uit deze repository naar de map
`custom_components` van je Home Assistant-configuratie en herstart Home Assistant.

Draait Home Assistant in Docker en is de configuratiemap eigendom van een andere gebruiker
(bijvoorbeeld uid 911), geef de map dan die eigenaar:

```sh
sudo cp -r custom_components/socialschools /pad/naar/config/custom_components/
sudo chown -R 911:911 /pad/naar/config/custom_components/socialschools
```

Herstart daarna de Home Assistant-container (bijvoorbeeld `docker restart homeassistant`).

## Configuratie

*Instellingen → Apparaten & diensten → Integratie toevoegen → Social Schools*. Vul het
e-mailadres en het wachtwoord van je Social Schools-account in. Het wachtwoord wordt alleen
gebruikt om in te loggen en daarna weggegooid; de integratie bewaart het refresh token dat Social
Schools teruggeeft.

Eén configuratie per account: alle scholen waar je een ouderrol hebt, komen onder die ene
configuratie. Heeft het account geen ouderrol, dan stopt het instellen met de melding dat er geen
ouderrol is gevonden.

### Opties

*Instellingen → Apparaten & diensten → Social Schools → Configureren*:

| Optie | Standaard | Bereik | Betekenis |
|---|---|---|---|
| Bijwerkinterval (minuten) | 15 | 10 – 1440 | hoe vaak de berichtenfeed wordt opgehaald |
| Agendahorizon (dagen) | 90 | 7 – 365 | hoe ver vooruit de agenda wordt opgehaald |
| Berichten in het lijstattribuut | 10 | 1 – 20 | lengte van het attribuut `berichten` |

Een wijziging wordt direct toegepast (de integratie herlaadt). De agenda wordt los van het
berichteninterval elk uur ververst.

## Entiteiten

Per school maakt de integratie één apparaat **Social Schools · \<schoolnaam\>** met vier
entiteiten. Home Assistant leidt de entity-id's af uit de apparaatnaam en de vertaalde
entiteitsnaam; voor een school "Voorbeeldschool":

| Entiteit | Home Assistant in het Nederlands | Home Assistant in het Engels |
|---|---|---|
| Laatste bericht (sensor) | `sensor.social_schools_voorbeeldschool_laatste_bericht` | `sensor.social_schools_voorbeeldschool_latest_post` |
| Berichten (sensor) | `sensor.social_schools_voorbeeldschool_berichten` | `sensor.social_schools_voorbeeldschool_posts` |
| Nieuw bericht (event) | `event.social_schools_voorbeeldschool_nieuw_bericht` | `event.social_schools_voorbeeldschool_new_post` |
| Agenda (calendar) | `calendar.social_schools_voorbeeldschool_agenda` | `calendar.social_schools_voorbeeldschool_calendar` |

Leestekens en accenten in de schoolnaam worden door Home Assistant omgezet
(`Basisschool 't Voorbeeld` → `basisschool_t_voorbeeld`, `De Voorbeeldschool (Noord)` →
`de_voorbeeldschool_noord`). De unieke id's
(`<profiel-id>_<school-id>_<sleutel>`) veranderen niet, dus je kunt de entity-id's gerust
hernoemen. Attribuutnamen zijn Nederlands en vast; ze worden niet vertaald.

### `sensor.…_laatste_bericht`

Toestand: de titel van het nieuwste bericht (of de eerste regel van de tekst als er geen titel
is), ingekort tot 255 tekens; `unknown` als er geen berichten zijn.

| Attribuut | Inhoud |
|---|---|
| `post_id` | id van het bericht (getal) |
| `groep` | groep(en) waarin het bericht is geplaatst, gescheiden door `, ` (max. 255 tekens) |
| `auteur` | naam van degene die het bericht plaatste (max. 120 tekens) |
| `geplaatst` | tijdstip van plaatsing (ISO 8601, Nederlandse tijd) |
| `tekst` | de tekst van het bericht als platte tekst, maximaal 2000 tekens |
| `afgekapt` | `true` als `tekst` is ingekort |
| `bijlagen` | lijst met de bestandsnamen van de bijlagen (max. 20 namen van elk max. 120 tekens; worden alle attributen samen groter dan ongeveer 12 kB JSON, dan vallen de laatste namen weg) |
| `reacties` | aantal reacties |

### `sensor.…_berichten`

Toestand: het aantal berichten van de afgelopen zeven dagen (binnen de opgehaalde feed van
20 berichten).

Attribuut `berichten`: een lijst van de nieuwste berichten (aantal instelbaar, standaard 10),
elk met `post_id`, `titel` (max. 120 tekens), `groep` (max. 120 tekens), `auteur` (max. 120
tekens), `geplaatst` en `tekst_kort` (max. 300 tekens). Zouden alle attributen samen groter
worden dan ongeveer 12 kB JSON, dan vallen de oudste berichten uit de lijst.

### `event.…_nieuw_bericht`

Eventtype: `nieuw_bericht`. Gaat af voor elk bericht dat sinds de vorige ophaalronde nieuw in de
feed is verschenen, in chronologische volgorde, met de attributen `post_id`, `titel`, `groep`,
`auteur`, `geplaatst` en `tekst_kort`.

Een bericht is nieuw als zijn id nog niet eerder is gezien én het is geplaatst na het hoogste
bekende tijdstip min zeven dagen. Bewerkte berichten houden hun id en zijn dus niet opnieuw
nieuw. Bij de eerste ophaalronde na het instellen of na een herstart van Home Assistant worden
alle berichten in de feed stilletjes als gezien geregistreerd: je krijgt nooit een stortvloed aan
oude meldingen, maar een bericht dat precies tijdens een herstart wordt geplaatst, kan worden
gemist.

### `calendar.…_agenda`

Een gewone agenda-entiteit: `on` zolang er een activiteit bezig is, anders `off`, met de
standaardattributen (`message`, `start_time`, `end_time`, `all_day`, `location`, `description`)
van de lopende of eerstvolgende activiteit. Hele-dag-activiteiten zijn echte hele-dag-items;
activiteiten met een tijd staan in Nederlandse tijd (Europe/Amsterdam). De agenda beslaat zeven
dagen terug tot de ingestelde horizon vooruit en wordt elk uur ververst. Ze is te gebruiken in de
agendakaart van het dashboard en met de actie `calendar.get_events`.

## Automatiseringen

### Melding bij een nieuw bericht

Met de trigger *Event received* van de event-entiteit:

```yaml
alias: Nieuw schoolbericht
triggers:
  - trigger: event.received
    target:
      entity_id: event.social_schools_voorbeeldschool_nieuw_bericht
    options:
      event_type:
        - nieuw_bericht
actions:
  - action: notify.mobile_app_telefoon
    data:
      title: "{{ trigger.to_state.attributes.groep }} – {{ trigger.to_state.attributes.titel }}"
      message: "{{ trigger.to_state.attributes.tekst_kort }}"
```

Of met een gewone *state*-trigger: de toestand van een event-entiteit is het tijdstip van het
laatste event, dus elk nieuw bericht verandert de toestand.

```yaml
triggers:
  - trigger: state
    entity_id: event.social_schools_voorbeeldschool_nieuw_bericht
    not_to:
      - unknown
      - unavailable
```

Komen er in één ophaalronde meerdere nieuwe berichten binnen, dan gaat het event voor elk bericht
apart af (oudste eerst) en start je automatisering dus meerdere keren.

### Schoolagenda van morgen

```yaml
alias: Schoolagenda van morgen
triggers:
  - trigger: time
    at: "19:00:00"
actions:
  - action: calendar.get_events
    target:
      entity_id: calendar.social_schools_voorbeeldschool_agenda
    data:
      start_date_time: "{{ (now() + timedelta(days=1)).strftime('%Y-%m-%d 00:00:00') }}"
      duration:
        days: 1
    response_variable: agenda
  - action: notify.mobile_app_telefoon
    data:
      title: Schoolagenda morgen
      message: >-
        {% set items = agenda['calendar.social_schools_voorbeeldschool_agenda']['events'] %}
        {% if items %}
          {% for item in items %}- {{ item.summary }}
          {% endfor %}
        {% else %}
          Morgen staat er niets in de schoolagenda.
        {% endif %}
```

## Gedrag bij storingen

- Lukt het ophalen tijdelijk niet (geen verbinding, time-out, een 5xx-fout of een onverwacht
  antwoord), dan houdt de integratie de **vorige gegevens** vast en blijven alle entiteiten
  beschikbaar; `calendar.get_events` blijft antwoorden uit de laatst opgehaalde agenda. De
  volgende ronde probeert het gewoon opnieuw. Home Assistant logt de eerste mislukking en het
  herstel, niet elke ronde opnieuw.
- Alleen wanneer Social Schools het **refresh token afwijst** (bijvoorbeeld na een
  wachtwoordwijziging of als het token is ingetrokken) vraagt Home Assistant om opnieuw in te
  loggen (*Opnieuw verifiëren*). Je vult dan alleen je wachtwoord opnieuw in; het moet hetzelfde
  account zijn.
- Mislukt alleen het ophalen van de agenda terwijl de berichten wel lukken, dan blijft de vorige
  agenda staan en gaat de rest gewoon door.
- Is Social Schools niet bereikbaar bij het opstarten, dan probeert Home Assistant het instellen
  later automatisch opnieuw.
- Een herlaad (bijvoorbeeld na het opslaan van opties) wacht op een tokenverversing die op dat
  moment bezig is, zodat een zojuist geroteerd refresh token niet verloren gaat.
- Diagnostische gegevens (*apparaat → Diagnostische gegevens downloaden*) bevatten tijdstippen,
  aantallen, school-id's en de status van de laatste ophaalronde, maar geen berichten, namen,
  e-mailadres of tokens.

## De probe: `scripts/probe.py`

`scripts/probe.py` controleert live of het inlog- en API-protocol nog klopt, zonder Home
Assistant. Handig voordat je de integratie installeert en wanneer er iets kapot lijkt na een
wijziging bij Social Schools. Vanuit de root van deze repository, in een wegwerpcontainer met
alleen `aiohttp`:

```sh
docker run --rm -it --network host -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -q aiohttp && python scripts/probe.py"
```

Het script vraagt je e-mailadres en wachtwoord (het wachtwoord wordt niet getoond en nergens
bewaard; argumenten of omgevingsvariabelen worden bewust niet geaccepteerd), logt in zoals de
app, wisselt de code in, ververst de tokens één keer en doet daarna dezelfde vier leesaanvragen
als de integratie, voor de eerste school met een ouderrol. Het drukt uitsluitend regels als deze af
(de waarden zijn een voorbeeld; de veldnamen zijn wat de API op dat moment teruggeeft):

```text
Refresh token ontvangen: ja
expires_in: 3600
Refresh werkt: ja
Refresh token roteert: nee
Ouderrollen/scholen: 1
Velden van een bericht: author, body, commentsCount, communities, created, id, media, title
Velden van communities[0]: id, name
Velden van author: displayName, id
Velden van media[0]: fileName, fullPath
Body is HTML: ja
Feedbody even lang als detail: ja (812/812)
Velden van een event: allDay, communities, description, end, id, location, start, title
Aantal events in venster: 14
```

Geen tokens, namen, titels, teksten of URL's: alleen ja/nee, aantallen, tekstlengtes en
veldnamen. Bij een fout drukt het script alleen de naam van de fout en een opgeschoonde melding
af en stopt het met exitcode 1. `python scripts/probe.py --check` controleert alleen of de
protocolmodules zonder Home Assistant laden (dat gebruikt de testsuite).

Wat de uitkomst betekent:

- **Refresh werkt: nee** – de integratie zou na ongeveer een uur om opnieuw inloggen vragen;
  eerst uitzoeken voordat je installeert.
- **Refresh token roteert: ja** – normaal; de integratie slaat het nieuwe token automatisch op.
- **Body is HTML: ja** – verwacht; de integratie zet HTML om naar platte tekst.
- **Feedbody even lang als detail: nee** – de feed levert ingekorte teksten. De constante
  `FETCH_POST_DETAILS` in `custom_components/socialschools/const.py` kan dan op `True`, zodat de
  volledige tekst per nieuw bericht wordt opgehaald (één extra aanvraag per nieuw bericht).

## Privacy

- De integratie logt zelf nooit wachtwoorden, tokens, cookies, antwoorden van de API, namen of
  berichtteksten, ook niet op debugniveau; in haar eigen logregels wordt de configuratie bij het
  entry-id genoemd. Foutmeldingen bevatten alleen de soort fout en de HTTP-status. Home Assistant
  zelf noemt in zijn meldingen over het instellen van een integratie de titel van de configuratie
  (je weergavenaam), zoals bij elke integratie.
- De attributen `tekst` (laatste bericht), `berichten` (de lijst), `bijlagen` en `tekst_kort`
  (van het event) worden **niet in de recorder-database** opgeslagen; ze bestaan alleen in de
  actuele toestand (en het laatste event, inclusief `tekst_kort`, in `core.restore_state`). De
  toestand zelf (de titel), `auteur`, `groep`, `geplaatst`, `post_id`, `reacties` en de overige
  attributen van het event (`titel`, `groep`, `auteur`, `geplaatst`, `post_id`) worden wel in de
  geschiedenis vastgelegd, zoals bij elke entiteit.
  Wil je ook die niet bewaren, sluit de entiteiten dan uit in de
  [recorder-configuratie](https://www.home-assistant.io/integrations/recorder/#configure-filter).
- Diagnostische gegevens bevatten geen inhoud en geen namen; e-mailadres, profiel-id, titel van
  de configuratie en tokens zijn daarin weggelakt.
- De integratie praat uitsluitend met `login.socialschools.eu` en `api.socialschools.eu` over
  HTTPS en volgt geen redirects naar andere hosts.

## Beperkingen

- Per school wordt alleen de eerste pagina van de feed opgehaald: de **20 nieuwste berichten**.
  Oudere berichten tellen niet mee en komen niet in `berichten`.
- De agenda beslaat **7 dagen terug tot N dagen vooruit** (optie *agendahorizon*, standaard 90);
  buiten dat venster geeft `calendar.get_events` niets terug.
- Het aantal aanvragen blijft ruim onder **20 per uur per school**: bij het standaardinterval
  4 feed-aanvragen en 1 agenda-aanvraag per uur per school, plus ongeveer één tokenverversing per
  uur per account en één accountaanvraag bij het opstarten. Bij het minimum van 10 minuten zijn
  het 7 aanvragen per uur per school. Berichtdetails worden standaard niet opgehaald.
- Alleen **ouderrollen**; andere rollen worden overgeslagen.
- Eén Social Schools-account per configuratie; meerdere accounts zijn meerdere configuraties.
- Nederlandse en Engelse vertalingen; de attribuutnamen zijn altijd Nederlands.
- Vereist Home Assistant 2026.1 of nieuwer; ontwikkeld en getest tegen 2026.10.

## Ontwikkeling

```sh
python3.14 -m venv venv && . venv/bin/activate
pip install -r requirements_test.txt      # pytest-homeassistant-custom-component + ruff
pytest                                    # alle tests (asyncio_mode=auto, zie pyproject.toml)
pytest tests/test_probe.py -q             # of één bestand
ruff check .
```

- `const.py`, `errors.py`, `payload.py`, `login.py`, `tokens.py`, `client.py`, `models.py` en
  `textutil.py` in `custom_components/socialschools` zijn "pure" modules zonder Home
  Assistant-afhankelijkheid (alleen de standaardbibliotheek, `aiohttp`, `yarl` en `multidict`) en
  draaien ook op Python 3.12; `scripts/probe.py` laadt ze via een stand-in pakket.
- De tests gebruiken uitsluitend synthetische gegevens; alles wat op een geheim lijkt draagt de
  marker `private-`, zodat lekken in foutmeldingen opvallen. HTTP wordt afgevangen met
  `aioclient_mock` of een lokale `aiohttp`-testserver; er gaat niets naar buiten.
- CI (`.github/workflows/validate.yml`): hassfest, de HACS-validatie (zonder de `brands`-controle
  zolang het domein niet in home-assistant/brands staat), een compileercontrole van de pure
  modules en de probe met Python 3.12, en `ruff check` + `pytest` op Python 3.14, bij elke push,
  pull request en wekelijks.
- `docs/DESIGN.md` is het ontwerpcontract (namen, gedrag, grenzen); gedragswijzigingen horen
  daar ook in.
