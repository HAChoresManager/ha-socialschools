# Eindrapport: Social Schools voor Home Assistant 0.2.1

Datum: 2026-11-08. Branch `claude/bold-franklin-ok3whj`, pull request naar `main`.
Het rapport van 0.1.0 staat in [`EINDRAPPORT.md`](EINDRAPPORT.md). Wat er na de merge van
0.2.1 gebeurde (de live probe, de koppeling in 0.2.2 en `v0.2.0`), staat onder
[Vervolg: 0.2.2](#vervolg-022).

## Samenvatting

| Onderdeel | Uitkomst |
|---|---|
| 1. Hele-dag-items één dag te lang | Opgelost en getest op de zes live vormen plus varianten. |
| 2. `open_bericht` zonder extra verzoek | Een bericht uit de feed kost nul verzoeken; alleen een bericht buiten de feed (of met een teken van inkorten) wordt één keer opgehaald. |
| 3. Kinderen automatisch koppelen | Probe uitgebreid in 0.2.1. De live probe vond een koppeling op groepsnaam; de automatische koppeling is gebouwd in 0.2.2 (zie Vervolg). |
| 4. Releases | Workflow en script klaar. `v0.2.1` is na de merge aangemaakt; `v0.2.0` bleek niet te kunnen en is losgelaten (zie Vervolg). |
| 5. Afronding | Versie 0.2.1, CHANGELOG, README, DESIGN; 1090 tests groen op Python 3.14, controles op Python 3.12 groen. |

## 1. Agenda: hele dagen

**Oorzaak.** Social Schools geeft een hele dag door als middernacht UTC met `Z`, met een einde
dat al exclusief is (`2026-11-26T00:00:00Z` tot `2026-11-27T00:00:00Z`). `parse_event` zette
start en einde eerst om naar Europe/Amsterdam. Het einde werd daardoor 01:00, `_all_day_end` zag
geen middernacht, vatte het op als inclusief einde en telde er een dag bij: einde 28 november in
plaats van 27.

**Fix** (`custom_components/socialschools/models.py`, `_all_day_dates`). Bij `allDay` worden
datum en tijd gelezen zoals ze in de tekst staan, zonder omrekening naar welke tijdzone dan ook.
Dat werkt hetzelfde voor `Z`, voor een offset zoals `+01:00` en voor een waarde zonder tijdzone.

- start = de datum uit de tekst van `start`;
- einde = de datum uit de tekst van `end` als de tijd daar 00:00 is, anders die datum + 1;
- het einde is altijd minstens start + 1 (ook als `end` ontbreekt, ongeldig is of gelijk is aan
  de start).

Items met een tijd zijn ongewijzigd: een tijd met zone wordt omgezet naar Europe/Amsterdam, een
tijd zonder zone geldt als Europe/Amsterdam. In het probevenster stonden geen items met een tijd,
dus die aanname is niet opnieuw bevestigd; de terugval staat er nog.

**Testgevallen** (`tests/test_models.py`, `tests/test_calendar.py`):

| `start` | `end` | Wordt (einde exclusief) |
|---|---|---|
| `2026-11-26T00:00:00Z` | `2026-11-27T00:00:00Z` | 26 → 27 november: de studiedag duurt één dag |
| `2026-12-03T00:00:00Z` | `2026-12-04T00:00:00Z` | 3 → 4 december |
| `2026-12-04T00:00:00Z` | `2026-12-05T00:00:00Z` | 4 → 5 december |
| `2026-12-17T00:00:00Z` | `2026-12-18T00:00:00Z` | 17 → 18 december |
| `2026-12-18T00:00:00Z` | `2026-12-19T00:00:00Z` | 18 → 19 december |
| `2026-12-21T00:00:00Z` | `2027-01-04T00:00:00Z` | 21 december → 4 januari: de kerstvakantie loopt tot en met zondag 3 januari |

Varianten, die allemaal 26 → 27 november opleveren, elk onder drie tijdzones (Amsterdam, UTC,
Los Angeles):

- zonder `Z` (`2026-11-26T00:00:00`);
- met `+01:00` (`2026-11-26T00:00:00+01:00` wordt 26 november);
- met `-05:00` en met milliseconden (`.000Z`);
- een einde dat ontbreekt, en een einde dat gelijk is aan de start (met `Z` en met `+01:00`).

Daarnaast door de agenda-entiteit heen: `calendar.get_events` geeft de studiedag als `2026-11-26`
→ `2026-11-27` en de kerstvakantie als `2026-12-21` → `2027-01-04`. Op 27 november staat er voor
die dag niets meer, zondag 3 januari valt nog in de vakantie en maandag 4 januari niet. De agenda
staat `on` tijdens 26 november en is om 01:30 op 27 november `off`, met de kerstvakantie als
volgende item. De tests met de live vormen (`Z`) falen op de code van 0.2.0 en slagen nu.

Drie bestaande testgevallen beschreven het oude omrekengedrag en volgen nu de nieuwe regel. Zo
werd `2026-11-04T23:00:00Z` → `2026-11-05T23:00:00Z` vroeger 5 → 6 november, en nu 4 → 6
november (datums zoals geschreven, het einde om 23:00 telt mee). Zulke tijden stuurt de API voor
hele dagen niet: de probe zag alleen middernacht.

**Template-sensor `sensor.school_agenda`.** Die trekt er een dag af zolang álle hele-dag-items
minstens twee dagen beslaan. Na de update beslaat een eendaags item één dag, dus stopt die
correctie vanzelf. Er hoeft niets aan te veranderen.

## 2. `open_bericht`: geen extra verzoek als de feed het bericht al heeft

`async_open_post` haalde bij een cache-miss altijd `GET communityposts/{id}` op. Nu:

- **Een bericht in de feed** wordt getoond zoals de feed het heeft: **nul verzoeken**. De probe
  liet zien dat de feed-body even lang is als de detail-body (494/494).
- **Het detail wordt alleen opgehaald** voor:
  - een bericht dat niet meer in de feed staat. De integratie herkent het aan de bericht-id's die
    ze eerder in de feed zag (de laatste 500 per school, ook na een herstart);
  - een aanwijzing dat de feed-body is ingekort: de tekst eindigt op `…` of `...`, of
    `FETCH_POST_DETAILS` staat aan (behalve als de poll de detailtekst al heeft opgehaald).
- Een opgehaald bericht komt in de cache (20 per entry). Daarna kost opnieuw openen geen verzoek.
- Een id dat de integratie nooit in een feed zag, geeft zoals voorheen de melding dat het bericht
  niet in de huidige feed staat. Dan gaat er niets naar Social Schools.
- Een fout bij het ophalen houdt het vorige bericht vast (melding `open_failed`), en er wordt niets
  gecachet.

Waarom: het scheelt verzoeken, en we weten niet of een detailverzoek bij Social Schools als
"bekeken" telt (berichten hebben `readStatistics`). De regel blijft: de integratie stuurt nooit
iets dat als gelezen kan tellen.

Er is één bewuste afweging. Het teken "eindigt op een beletselteken" kan ook afgaan bij een tekst
die de schrijver zelf met `...` eindigt. Dat kost dan één verzoek, één keer.

Tests (`tests/test_open_post.py`):

- openen uit de feed doet nul verzoeken (ook herhaald, en de feedtekst wint van een afwijkend
  detail);
- een bericht buiten de feed doet één verzoek en komt daarna uit de cache;
- een bericht van vóór een herstart (id in de opslag) is te openen;
- een fout houdt het vorige bericht vast, en een volgende poging vraagt opnieuw;
- een onbekend id wordt geweigerd zonder verzoek;
- een feedtekst die op `…` of `...` eindigt, haalt het detail één keer op;
- met `FETCH_POST_DETAILS` wordt de al opgehaalde detailtekst hergebruikt;
- de cache houdt de 20 laatst geopende berichten vast;
- lange teksten blijven binnen het attribuutbudget.

De whitelist-test (`tests/test_read_tracking.py`) dekt nu ook het detailverzoek van een bericht
buiten de feed. Daarbij gaan alleen GET-verzoeken naar de vier bestaande paden.

## 3. Kinderen automatisch koppelen

**Wat de probe nu doet.** `scripts/probe.py --kinderen-vorm` drukt na de bestaande regels af:

- het aantal groepen per connection (kind);
- het type van `roles[0].connections[0].groups[0]` (van het eerste kind met groepen), en bij een
  object de veldnamen met hun typen;
- of een `groupId` uit de communities van de feed voorkomt in de groepen van een kind. Het
  antwoord is `ja, bij N van M berichten (via groups[].<veld>)`, `nee` of `onbekend`. Dezelfde
  vraag wordt los gesteld voor community-`id`;
- of een groepsnaam van een kind gelijk is aan de `name` van een community in de feed;
- of `yearClass` exact gelijk is aan de `name` van een community in de feed, en of dat zo is na
  het negeren van hoofdletters en extra spaties.

De probe drukt alleen veldnamen, typen, ja/nee en aantallen af, nooit namen van kinderen of
groepen en nooit id's. Voorbeeld met verzonnen gegevens:

```text
Groepen per connection: 1
Vorm van roles[0].connections[0].groups[0]: object; velden: groupId (int), id (int), name (str)
groupId uit de feed in de groepen van een kind: ja, bij 7 van 20 berichten (via groups[].groupId)
id uit de feed in de groepen van een kind: nee
Groepsnaam van een kind gelijk aan een communitynaam in de feed: ja, bij 7 van 20 berichten (via groups[].name)
yearClass exact gelijk aan een communitynaam in de feed: nee
yearClass gelijk zonder verschil in hoofdletters en spaties: nee
```

De oude regels "Overlap met …" heten nu "Overlap van die id's met …". Ze vergelijken alleen de
id's onder de paden bij "Gevonden onder" en zijn dus niet te verwarren met het nieuwe
groupId-antwoord.

**Uitkomst bij het opleveren van 0.2.1: nog niet bekend.** De probe heeft echte inloggegevens
nodig, en die waren er in de ontwikkelomgeving niet. Na de merge is hij live gedraaid; de
uitkomst en de koppeling die daarop volgde, staan onder [Vervolg: 0.2.2](#vervolg-022).

**Zo lees je de uitkomst** (de handleiding zoals die bij 0.2.1 hoorde):

- `groupId … ja, bij N van M berichten (via groups[].X)`, waarbij N ongeveer gelijk is aan het
  aantal berichten voor de groep van het kind: de koppeling werkt via veld `X`. Dan is de
  vervolgstap klein. Per bericht worden de kinderen bepaald via de groepen van hun connection
  (`groups[].X` tegen `communities[].groupId`), met als naam `firstName` (of `displayName`). De
  optie `kinderen` blijft als aanvulling en overschrijving, en `schoolbreed` blijft zoals het is.
  Dan hoeft er in augustus niets bijgewerkt te worden, en komt een kind dat naar
  school gaat er vanzelf bij.
- Alleen `Groepsnaam … ja`: dan kan een koppeling op naam, met dezelfde exacte vergelijking als de
  optie. Ook dat volgt de groepen automatisch.
- Alleen `yearClass … ja`: dat is bruikbaar, maar zwakker, omdat de jaargroep niet altijd de naam
  van de community is.
- Overal `nee`: dan blijft het bij de handmatige optie.

## 4. Releases automatisch

**Hoe het werkt.** `.github/workflows/release.yml` start bij elke push naar `main`. Handmatig
starten kan ook, maar alleen op `main`. De workflow draait `scripts/release.py`:

1. Hij leest de versie uit `custom_components/socialschools/manifest.json`.
2. Voor die versie kijkt hij of tag `v<versie>` bestaat. Bestaat de tag al, dan gebeurt er
   niets. (In 0.2.1 deed hij dat ook eenmalig voor 0.2.0; dat is in 0.2.2 vervallen, zie Vervolg.)
3. Bestaat de tag nog niet, dan zoekt hij de commit waarmee de versie op `main` kwam. Dat is de
   oudste commit in de nieuwste reeks eerste-oudercommits die die versie dragen; bij een
   gemergede pull request is dat de merge-commit.
4. Hij neemt de sectie `## [<versie>] - …` uit `CHANGELOG.md` als tekst.
5. Hij maakt met `gh release create v<versie> --target <commit>` de tag en de release, als
   *latest*.
6. Ontbreekt de changelog-sectie, of wordt de tag geweigerd, dan faalt de run met een duidelijke
   melding (`::error::`) en komt er geen lege release.

De workflow heeft alleen `contents: write`, gebruikt alleen `actions/checkout` (vastgepind, met
volledige geschiedenis en zonder bewaarde inloggegevens) en de `gh`-CLI met de `GITHUB_TOKEN`. Hij
draait nooit op pull requests en zit dus niet in de verplichte checks. De validatieworkflow
compileert het script wel met Python 3.12, de Python van de runner.

**Na de merge van 0.2.1** maakte de eerste run `v0.2.1` op de merge-commit (`9c8202f`), als
latest. HACS toont sindsdien versies in plaats van een commit van `main`. De eenmalige poging
voor `v0.2.0` op `e81245d` mislukte; zie Vervolg. `python3 scripts/release.py --dry-run` laat
vooraf zien wat een run zou doen.

`v0.1.0` krijgt bewust geen tag. Die versie bewaart haar gegevens in opslagversie 1 en kan de
opslag van 0.2 (versie 2) niet lezen, dus terugzetten via HACS zou de integratie breken.

**Rulesets.** De rulesets zelf kon ik vanuit deze omgeving niet inzien. De run na de merge geeft
nu wel het antwoord: **de rulesets houden het aanmaken van tags door de workflow niet tegen**,
want `v0.2.1` is in die run gewoon aangemaakt. Wat de rulesets in het algemeen wel en niet
tegenhouden:

- De regels op `main` (PR verplicht, checks hassfest/HACS/tests) houden niets tegen. De workflow
  pusht niets naar `main` en maakt alleen via de API een tag.
- Een tag wordt alleen geweigerd door een ruleset met als doel **Tags** en de regel **Restrict
  creations** die `v*` dekt. De run faalt dan met "Repository rule violations found … Cannot
  create ref due to creations being restricted", en het script wijst naar de rulesets.
- De `GITHUB_TOKEN` (github-actions[bot]) kan niet op de bypass-lijst van een ruleset. Daar
  kunnen alleen rollen, teams, GitHub Apps en Dependabot op.
- **Oplossing als zo'n ruleset er is:** zet in die tag-ruleset *Restrict creations* uit, of
  haal `v*` uit het doel. *Restrict updates* en *Restrict deletions* kunnen blijven staan: die
  houden geen nieuwe tag tegen en beschermen de bestaande tags. Start daarna de mislukte run
  opnieuw (Actions → Release → Re-run jobs), of start de workflow handmatig op `main`.
- Controleren: Settings → Rules → Rulesets. Staat er geen ruleset met doel *Tags*, dan houdt
  niets het tegen.
- Moet het aanmaken van tags beperkt blijven, dan kan het met een eigen GitHub App op de
  bypass-lijst en een token via `actions/create-github-app-token` (een actie van GitHub zelf).
  Dat vraagt een App en secrets en is niet gedaan.

## 5. Afronding

- Versie 0.2.1 in `manifest.json` en `const.py`. CHANGELOG, README en `docs/DESIGN.md` zijn
  bijgewerkt: §7 (regel voor hele dagen), §13 (probe), §16 (`open_bericht`) en de nieuwe §17. In
  de README zijn bijgewerkt: de agendaregel, *Geopend bericht*, de probe-opties, de opdracht
  zonder git, en releases.
- `diagnostics` is ongewijzigd en bevat alleen aantallen.
- Er zijn geen nieuwe paden in de whitelist en geen niet-GET-verzoeken. De test die de vier paden
  vastlegt (`test_allowed_paths_are_exactly_the_read_endpoints`) is ongewijzigd, en de
  whitelist-test met acties is uitgebreid.
- Lokaal is getest op dezelfde Python als de CI:
  - Python 3.14.6 met Home Assistant 2026.10.4 en pytest-homeassistant-custom-component
    0.13.375: 1090 tests geslaagd (was 994). `ruff check` en `ruff format --check` zijn schoon.
  - Python 3.12.3: `py_compile` van de pure modules, de probe en `scripts/release.py`, en
    `scripts/probe.py --check` met alleen aiohttp, slagen.
  - actionlint geeft geen meldingen op beide workflows.
- hassfest en de HACS-validatie draaien in CI op de pull request. Als geheel kunnen ze hier niet
  draaien.

## Probe-opdracht

Na de merge, op de NUC (zonder git; de repository wordt in een wegwerpcontainer opgehaald):

```sh
mkdir -p ~/ss-probe
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v ~/ss-probe:/w alpine/git clone --depth 1 https://github.com/HAChoresManager/ha-socialschools.git /w
cd ~/ss-probe && docker run --rm -it --network host -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -q --root-user-action=ignore --disable-pip-version-check aiohttp && python scripts/probe.py --kinderen-vorm"
cd ~ && rm -rf ~/ss-probe
```

Wil je de nieuwe probe al vóór de merge draaien, zet dan in de tweede regel
`--branch claude/bold-franklin-ok3whj` direct na `--depth 1`. De rest blijft gelijk.

De probe vraagt e-mail en wachtwoord en drukt alleen veldnamen, typen, ja/nee en aantallen af.
Voor de koppeling zijn de regels vanaf `Kinderen-vorm:` nodig; die bevatten geen namen en geen
id's.

## Vervolg: 0.2.2

**De live probe** (8 november 2026, `--kinderen-vorm`) gaf:

```text
Groepen per connection: 1
Vorm van roles[0].connections[0].groups[0]: str
groupId uit de feed in de groepen van een kind: nee
id uit de feed in de groepen van een kind: nee
Groepsnaam van een kind gelijk aan een communitynaam in de feed: ja, bij 16 van 20 berichten (via groups[])
yearClass exact gelijk aan een communitynaam in de feed: nee
yearClass gelijk zonder verschil in hoofdletters en spaties: nee
```

De groepen van een kind zijn dus namen, en die komen bij 16 van de 20 berichten overeen met een
community van het bericht. `groupId` en `yearClass` zijn niet bruikbaar.

**De automatische koppeling is gebouwd in 0.2.2.**

- Per school leest de integratie uit het account de kinderen bij de ouderrol: de voornaam
  (`firstName`, anders `displayName`) en de groepsnamen (`groups`).
- Een bericht hoort bij een kind als een van zijn communitynamen gelijk is aan een groep van het
  kind, met dezelfde vergelijking als de optie (hoofdletters en extra spaties tellen niet).
  `Neo 4/5/6` telt dus nog steeds niet als `6A`.
- De optie `kinderen` blijft als aanvulling en overschrijving. Een regel voor een kind uit het
  account vervangt diens groepen, een regel voor een ander kind voegt het toe. Klopt de koppeling,
  dan kan de optie leeg. Staat er nog een regel voor een kind dat in het account staat, dan wint
  die regel, en moet je hem in augustus zelf bijwerken.
- Het account wordt elke dag opnieuw gelezen (één extra GET op het bestaande pad
  `useraccounts/current`). Een nieuw schooljaar of een kind dat naar school gaat, komt er zo
  vanzelf bij, zonder herstart. Mislukt dat lezen, dan blijven de vorige kinderen staan en volgt
  na een uur een nieuwe poging.
- `schoolbreed` is ongewijzigd. Namen staan alleen in de attributen `kinderen`, niet in de opslag,
  de diagnostiek (alleen `account_child_count`) of het log.

**`v0.2.0` is losgelaten.** De run na de merge van 0.2.1 kreeg voor `v0.2.0` "HTTP 403: Resource
not accessible by integration". GitHub weigert de `GITHUB_TOKEN` een tag op een commit waarvan
`.github/workflows` afwijkt van `main`, en dat recht kan het token niet krijgen. De commit
`e81245d` heeft geen `release.yml` en een oudere `validate.yml`. Met de hand aanmaken lukte ook
niet: de doelkeuze in de webinterface gaat niet verder terug dan de merge van PR 3. Omdat 0.2.0
verder niet nodig is, maakt de workflow alleen nog de tag van de versie in het manifest, en is
elke push naar `main` weer groen. Een geweigerde tag noemt nu beide bekende oorzaken.
