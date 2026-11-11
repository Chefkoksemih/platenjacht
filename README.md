# Platenjacht

Je eigen website met een lijst albums. Eens per week zoekt hij per album uitgebreid naar de goedkoopste LP (nieuw of tweedehands, inclusief verzending naar Nederland). Elke maandagochtend krijg je een mail met de beste deals en de laagste prijs per album.

## Hoe het werkt

- **Website** (GitHub Pages, gratis): albums of links toevoegen, wensprijs instellen, alle aanbiedingen en het prijsverloop bekijken.
- **Wekelijkse zoekronde** (GitHub Actions, gratis): elke maandagvroeg zoekt het script via de Discogs API en via Tavily op het web. Gemini leest de gevonden winkelpagina's en haalt er prijs, verzendkosten en voorraad uit.
- **Maandagmail** (Resend, gratis): per album de laagste prijs, waar je hem koopt, de goedkoopste nieuwe en tweedehands versie, en of hij goedkoper of duurder is dan vorige week. Bovenaan in rood de deals:
  - onder je wensprijs,
  - flink goedkoper dan vorige week,
  - laagste prijs tot nu toe, of
  - door de winkel als aanbieding gemarkeerd.
- **Alles gratis**: het Tavily-tegoed wordt over de maandagen verdeeld. Past niet alles in één ronde, dan komen de albums die het langst niet doorzocht zijn als eerste aan de beurt.

Voeg je een album toe, dan wordt alleen dat album meteen opgezocht, zodat je de prijs direct op de site ziet. Dat stuurt geen mail.

## Wat je nodig hebt (eenmalig, ongeveer 30 minuten)

| Dienst | Waarvoor | Kosten |
|---|---|---|
| GitHub | website en wekelijkse zoekronde | gratis |
| Tavily | zoeken op het web | gratis (1.000 credits per maand) |
| Google Gemini API | prijzen uit pagina's halen | gratis |
| Discogs | tweedehands en marketplace prijzen | gratis |
| Resend | mail versturen | gratis |

### Stap 1. Sleutels verzamelen

1. **Tavily**: log in op tavily.com en kopieer je API key.
2. **Gemini**: ga naar aistudio.google.com, klik op Get API key en dan Create API key. Voeg geen betaalgegevens toe.
3. **Discogs**: maak een account, ga naar Settings > Developers en klik op "Generate new token".
4. **Resend**: maak een account op resend.com **met het mailadres waarop je meldingen wilt krijgen** en maak een API key aan. Zonder eigen domein mag Resend alleen naar dat adres mailen, en dat is precies wat we willen.

### Stap 2. Repository aanmaken

1. Maak een account op github.com.
2. Klik op **New repository**, noem hem `platenjacht`, kies **Public** en vink "Add a README file" aan.
3. Klik op **Add file > Upload files** en sleep alle bestanden uit deze zip erin. Alles staat los naast elkaar, zonder mappen. Klik **Commit changes**.
4. Let op: de map `.github` is op een Mac onzichtbaar en wordt vaak niet meegesleept. Controleer of `.github/workflows/check.yml` in de repository staat. Zo niet: **Add file > Create new file**, typ als naam `.github/workflows/check.yml` en plak de inhoud van dat bestand erin.

### Stap 3. Sleutels als geheim opslaan

Ga in je repository naar **Settings > Secrets and variables > Actions > New repository secret** en maak deze aan:

| Naam | Waarde |
|---|---|
| `TAVILY_API_KEY` | je Tavily key |
| `GEMINI_API_KEY` | je Gemini key |
| `DISCOGS_TOKEN` | je Discogs token |
| `RESEND_API_KEY` | je Resend key |
| `NOTIFY_EMAIL` | het mailadres van je Resend-account |

Deze geheimen zijn voor niemand anders zichtbaar, ook niet in een publieke repository.

### Stap 4. Website aanzetten

**Settings > Pages**, kies bij Source "Deploy from a branch", branch `main`, map `/ (root)`, en klik Save. Na een minuut of twee staat je site op `https://JOUWNAAM.github.io/platenjacht/`.

### Stap 5. Website koppelen zodat je vanaf de site kunt toevoegen

1. Ga op GitHub naar je profielfoto > **Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token**.
2. Kies bij Repository access "Only select repositories" en selecteer `platenjacht`.
3. Zet bij Permissions: **Contents** op Read and write, **Actions** op Read and write.
4. Kopieer de token, open je website, klap onderaan **Koppeling met GitHub** open, plak hem daar en klik **Koppeling opslaan**.

De token blijft alleen in jouw browser. Op een andere telefoon of laptop plak je hem opnieuw.

### Stap 6. Testen

Voeg een album toe. Op GitHub zie je onder het tabblad **Actions** de check draaien. Na een paar minuten staan de prijzen op je site.

## Instellingen aanpassen (config.json)

- `tavily_monthly_budget`: hoeveel Tavily-credits per maand gebruikt mogen worden (standaard 900, zo blijft er marge onder de gratis 1.000).
- `zoekdiepte`: `advanced` (grondig, 2 credits per zoekopdracht) of `basic` (1 credit). Met advanced en drie zoekopdrachten kost een album 6 credits per week; zo passen er ruim 35 albums in het gratis tegoed.
- `vaste_winkels`: de Europese winkels die bij elke zoektocht samen in één zoekopdracht worden doorzocht.
- `gemini_model`: welk Gemini-model gebruikt wordt. Werkt dit model niet meer, dan valt het script vanzelf terug op het nieuwste gratis Flash-Lite-model.

- `shipping_estimates_eur`: geschatte verzendkosten als een winkel ze niet vermeldt, per herkomst (NL, EU, other, unknown, discogs). Geschatte bedragen staan op de site met een ± ervoor.
- `web_queries`: de zoekopdrachten per album. Minder zoekopdrachten is minder Tavily-tegoed.
- Het tijdstip van de wekelijkse ronde staat in `.github/workflows/check.yml` bij `cron` (in UTC-tijd). Wil je de mail een keer tussendoor? Ga op GitHub naar Actions, kies Prijzen checken, Run workflow en vink de mail aan.

## Hoe een plaat gevonden wordt

- **Herkennen**: met een Discogs-link wordt het album direct via het nummer in de link opgehaald. Bij "Artiest - Album" zoekt het script het album eerst op Discogs op. Van Discogs komen de hoes, het label, de barcode, de echte naam van de artiest en alle vinylpersingen met hun kleur.
- **Zoeken** (grondig, ongeveer 9 credits per plaat): je vaste winkels in groepjes van vijf (`winkels_per_zoekopdracht`), een brede Nederlandstalige zoekopdracht met voorrang voor Nederlandse sites, een met een andere schrijfwijze van de artiest, en een op barcode. Een snelle check doet alleen de groepjes vaste winkels (ongeveer 5 credits).
- **Gemini te druk?** Dan wacht het script en probeert het opnieuw, zo nodig met een ander gratis model. Lukt het echt niet, dan blijven de prijzen van de vorige keer staan.
- **Uitlezen**: Gemini leest steeds een paar pagina's tegelijk (`paginas_per_keer` in config.json), alleen pagina's waar de albumtitel (of barcode) en een europrijs op staan.
- **Discogs**: bij elke plaat staat een link naar de Discogs-marktplaats, gefilterd op verkopers uit Nederland.
- **Logboek**: open een plaat op de site en klik op "Gevonden, maar niet meegeteld" om te zien welke winkels zijn afgevallen en waarom.
- **Alleen de EU**: bekende Amerikaanse en Britse winkels worden overgeslagen, en daarna valt alles af wat niet aantoonbaar uit de EU verstuurt (ander land, prijs in dollars of ponden, of herkomst onbekend). Het VK, Zwitserland en Noorwegen tellen niet mee.
- **Discogs** levert geen prijzen meer, omdat niet te zien is of een verkoper in de EU zit.

Je vaste winkels staan in `vaste_winkels` in config.json; voeg er gerust een webadres aan toe (zonder www of https).

## Goed om te weten

- **Discogs-prijzen zijn exclusief verzending**; daar rekent de site met de schatting uit config.json. Klik door voor de echte verzendkosten van die verkoper.
- **Webprijzen haalt Gemini uit de tekst van winkelpagina's.** Meestal klopt dat, maar check altijd even op de site van de winkel voordat je bestelt.
- **Tegoed**: de site laat bovenaan zien hoeveel zoektegoed je deze maand gebruikt hebt. Het script blijft er zelf binnen. Ook de checkknoppen op de site gebruiken tegoed; wat je tussendoor gebruikt, gaat van de volgende maandagrondes af. Zonder betaalkaart kan Tavily nooit iets in rekening brengen.
- **Je albumlijst en prijzen zijn openbaar** in een publieke repository (je sleutels niet). Wil je dat niet, dan heb je een betaald GitHub-abonnement nodig voor Pages vanuit een privé-repository.
- GitHub pauzeert geplande checks na 60 dagen zonder activiteit in de repository. Je krijgt daar een mail over; één klik op "Alles snel checken" zet het weer aan.
