# Seller inquiry e-mail templates

Status: implemented as pure domain code (`src/suv_deals/domain/seller_templates.py`), version set
`seller_templates@1`, scope version 1. No e-mail has been sent by creating these templates.
Contract: specification v1.1 sections 37.1, 37.3 and 37.4.

## What these templates are for

Vasko's bounded **standing authorization** (recorded in
`config/seller_inquiry_authorization.yaml`, effective 2026-10-06) lets the system send **one**
initial inquiry per actual vehicle/seller pair, across all sites and sending accounts, to the
verified seller of the exact listing. The inquiry asks only:

1. whether the vehicle is still available,
2. for the available vehicle documents (registration documents and the CoC), with personal data
   redacted,
3. the seller's lowest/final selling price,

and states that the message is a non-binding information request.

There is **no per-message approval, no first-message approval and no first-template approval**.
The original-language message and the Macedonian preview are stored as audit artifacts so Vasko
can see what was sent; they never pause a qualifying send. The only switch that could add a
message approval is the explicit owner setting `SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL`
(default `false`), read by `inquiries.requires_message_approval`.

Not covered (and rejected by the validator): follow-ups, replies, offers, price acceptance,
negotiation beyond asking the lowest price, reservations, viewing appointments, travel or
collection, deposits/payments/cash, purchases, resale promises, attachments, CC/BCC or a second
recipient, and any personal data beyond the verified sender display name/e-mail.

## Language selection

`domain/language.py` decides the template language from evidence only:

1. a **verified** seller language preference (stated by the seller or established by the seller's
   own correspondence), then
2. the language of the **seller-written** advertisement text (title/description/seller note),
   detected deterministically with a minimum number of exclusive evidence words and a clear margin.

Website navigation language and the listing country are recorded but never decide. Switzerland is
German, French or Italian according to the text evidence. English is used only for an English
advertisement or a positively established English preference, never as the fallback. Mixed or
insufficient evidence is `language_unresolved`; a detected language without a template (e.g.
Dutch, Polish) is `unsupported_language` and is held for template work, never replaced by English.
That hold is technical language/template QA, not an approval request to Vasko.

## Templates (verbatim)

The first line of each block is the subject line as documented in the specification; only the
text after the label is the actual e-mail subject. `–` (EN DASH) and `’` (RIGHT SINGLE
QUOTATION MARK) are part of the texts.

German `seller_initial_de_v1`:

```text
Betreff: Anfrage zu {{vehicle_label}} – {{listing_reference}}

Guten Tag,

ich schreibe wegen dieses Fahrzeugs: {{listing_url}}

Ist das Fahrzeug noch verfügbar?
Könnten Sie mir die vorhandenen Fahrzeugunterlagen zusenden, insbesondere die Zulassungsunterlagen und das CoC, falls vorhanden? Bitte schwärzen Sie persönliche Daten.
Was ist Ihr niedrigster Verkaufspreis für das Fahrzeug?

Es handelt sich zunächst um eine unverbindliche Anfrage.

Freundliche Grüße
{{verified_sender_display_name}}
```

Italian `seller_initial_it_v1`:

```text
Oggetto: Richiesta su {{vehicle_label}} – {{listing_reference}}

Buongiorno,

scrivo per questo veicolo: {{listing_url}}

Il veicolo è ancora disponibile?
Potrebbe inviarmi i documenti disponibili del veicolo, in particolare la carta di circolazione e il certificato di conformità CoC, se presente? La prego di oscurare i dati personali.
Qual è il prezzo minimo finale a cui sarebbe disposto a venderlo?

Si tratta soltanto di una richiesta di informazioni.

Cordiali saluti,
{{verified_sender_display_name}}
```

French `seller_initial_fr_v1`:

```text
Objet : Renseignements sur {{vehicle_label}} – {{listing_reference}}

Bonjour,

je vous contacte au sujet de ce véhicule : {{listing_url}}

Le véhicule est-il toujours disponible ?
Pourriez-vous m’envoyer les documents disponibles du véhicule, notamment le certificat d’immatriculation et le certificat de conformité CoC, si vous en disposez ? Merci de masquer les données personnelles.
Quel est votre dernier prix, le plus bas auquel vous accepteriez de le vendre ?

Il s’agit uniquement d’une demande de renseignements.

Cordialement,
{{verified_sender_display_name}}
```

English `seller_initial_en_v1` (only with verified English-language evidence):

```text
Subject: Enquiry about {{vehicle_label}} – {{listing_reference}}

Hello,

I’m writing about this vehicle: {{listing_url}}

Is the vehicle still available?
Could you send the available vehicle documents, particularly the registration documents and the CoC if available? Please redact personal details.
What is your lowest final selling price for the vehicle?

This is an information enquiry only.

Kind regards,
{{verified_sender_display_name}}
```

Macedonian informational preview for Vasko `seller_initial_mk_preview_v1` (never sent to a
seller; rendered with exactly the same placeholder values as the message it mirrors, and only from
an exact rendering of a registered seller template, so it always translates what can be sent):

```text
Предмет: Прашање за {{vehicle_label}} – {{listing_reference}}

Здраво,

Ви пишувам за ова возило: {{listing_url}}

Дали возилото е сè уште достапно?
Може ли да ми ги испратите достапните документи за возилото, особено сообраќајната документација и CoC ако е достапен? Ве молам скријте ги личните податоци.
Која е вашата последна, најниска продажна цена за возилото?

Ова е само барање за информации.

Поздрав,
{{verified_sender_display_name}}
```

A unit test compares every block above with the constants in code and with spec 37.4.

## Placeholders and safe rendering

`render(template_id, vehicle_label, listing_reference, listing_url, sender_display_name, *,
verified_listing_url)` replaces only these bounded placeholders. Seller text is never inserted.

| Placeholder | Source | Rules |
|---|---|---|
| `vehicle_label` | `build_vehicle_label(make, model, generation)` / `vehicle_label_from_taxonomy(match)` from verified make/model (taxonomy match on structured fields, not a title guess) | letters, digits, space and `. - / + ( )`; at most 60 characters. **Shortening rule:** if `make model generation` is too long the optional generation is dropped; nothing else is truncated or invented; a duplicated make prefix in the model is removed. Missing make/model blocks rendering. |
| `listing_reference` | the exact listing reference (ad number/source id) | required; a missing reference **blocks** rendering; `A-Z a-z 0-9 . _ / # -` and single spaces; at most 64 characters |
| `listing_url` | the listing URL bound in the recipient evidence | `http`/`https` only, host with a dot, no credentials, no port, no fragment, no secret-looking query parameters, no encoded CR/LF/control characters, at most 512 characters, and **byte-identical** to `verified_listing_url` |
| `verified_sender_display_name` | the verified sender binding | letters, spaces, `. - '` (no digits); at most 64 characters |

Every placeholder value is rejected (never silently cleaned) when it contains CR/LF or Unicode
line separators (header injection), other control/format characters (including bidi overrides
and zero-width characters), raw HTML/markup (`<`, `>`, `[`, `]`, `{`, `}`, `|`, backtick,
backslash, HTML entities), template
syntax `{{ }}`, an `@`, or a URL outside its own slot. Errors carry problem codes only
(`TemplateRenderError.problems`); offending values are never echoed.

## Hashes and versioning

- `template_hash`: SHA-256 over template id, version, kind, language, subject and body.
- `body_hash`: SHA-256 over the canonical rendered message (`{"subject", "body"}`, NFC, LF line
  endings). It is stored in the inquiry binding at reservation and must match at dispatch.
- `scope_hash`: SHA-256 over the bounded scope contract (`SCOPE_CONTRACT`: purpose, the three
  questions, requested documents, non-binding statement, allowed/excluded data categories, one
  recipient, no CC/BCC, no attachments, no follow-ups, no commitments). Identical for all
  templates of the same scope version.
- Template ids are `seller_initial_<lang>_v<N>`. A wording fix creates a new id/version (old
  versions stay in code for audit); the inquiry binding records template id, version and hashes.

**Ordinary safe wording fixes within the same scope need no approval.** A new template version
only has to pass `validate_scope` on its rendered output. Changes that add commitments, extra
personal data, unrelated questions, numbers/prices/currencies, other URLs, markup, attachments,
CC/BCC or extra recipients fail validation and cannot be rendered or dispatched. Changing the
scope itself (questions, data categories) is a new scope version and a new owner authorization
record, not a template edit.

## Scope validator

`validate_scope(message, envelope=None)` is semantic so that wording fixes keep passing:

- exactly three questions, classified per language as availability, vehicle documents (must name
  the registration documents and the CoC) and lowest/final price, each exactly once; any other
  question fails (`EXTRA_QUESTION`, `UNRECOGNISED_QUESTION`, `QUESTION_MISSING:*`);
- the non-binding statement is present (`NON_BINDING_STATEMENT_MISSING`);
- a multilingual forbidden lexicon (applied to every message whatever its language, fail closed)
  for offers, purchase, price acceptance/agreement, reservation, deposit/payment/cash,
  appointment/viewing/test drive, travel/collection, negotiation/discount, finances/bank/IBAN,
  contact channels (phone/WhatsApp), address, identity documents, budget/profit/resale and
  unrelated business (export, destination market);
- no digits outside the bounded label/reference values (`PRICE_OR_NUMBER`), no currencies, no
  phone numbers/e-mail addresses/IBANs/secrets, exactly one URL equal to the verified listing URL,
  no markup or header lines, the signature equals the verified sender display name;
- with an envelope: exactly one valid `To` address, no CC/BCC, at most one valid Reply-To, no
  attachments and only the allow-listed extra headers (`Message-ID`, `Date`, `MIME-Version`,
  `Content-Type`, `Content-Transfer-Encoding`), each at most once, ASCII-printable and without
  CR/LF or Unicode line separators. `Content-Type` must be a single `text/plain` part (charset
  UTF-8 or US-ASCII): `text/html`, any `multipart/*` container (the carrier of attachments) or extra
  parameters fail with `MIME_NOT_PLAIN_TEXT`; `MIME-Version` must be `1.0`,
  `Content-Transfer-Encoding` one of `7bit`, `8bit`, `quoted-printable`, `base64`, and
  `Message-ID` a single `<id@domain>`.

The validator runs on every rendering, on the Macedonian preview, again when the binding is
created (`inquiries.bind_inquiry`) and again with the envelope immediately before transmission
(`inquiries.dispatch_preflight`).

## Exact rendering

`validate_scope` is semantic so that a reviewed wording fix can become a new template version.
What is actually bound and sent must, however, be the exact deterministic rendering of a
registered template: `rendering_problems(message)` re-validates every placeholder value with the
same rules as `render`, re-renders the registered template and requires byte-identical subject and
body, the registered template id/version/hash/kind/language, the current template set and the
scope hash. `bind_inquiry` refuses (`NOT_TEMPLATE_RENDERING`, `TEMPLATE_NOT_REGISTERED`, ...) and
`dispatch_preflight` cancels (`MESSAGE_NOT_TEMPLATE_RENDERING`) any hand-built or edited message,
even one whose wording would stay inside the scope. A `VehicleLabel` text is always exactly
`make model [generation]` from its verified parts.
