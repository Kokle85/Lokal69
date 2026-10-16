# ADR 0004: The owner's MK estimates as the resale fallback

Status: accepted (owner decision of 2026-10-16). Date: 2026-10-16.

> Decisions 1 and 2 are refined by [ADR 0005](0005-mk-price-orientation-framework.md) (same
> day): the MK rules are a framework for orientation, a close same-model MK asking-price
> orientation now comes before the strictly matched estimates, and a relaxed orientation over
> the estimates is the last fallback. The estimates were read from Reklama5 asking prices.

## Context

On 2026-10-16 the owner sent the models to search abroad together with MK resale prices
(„Ова се моделите кои ќе ги пребаруваме за да имаме орјентациони цени во мк"). Asked where the
prices come from, the owner answered „Моја проценка" (my own estimate). They are not ads and not
sales.

Until then an `owner_estimate` observation was stored but never used. Without MK asking prices
(both MK sources are still placeholders, and the owner had not imported any ads) every
valuation was `insufficient_comparables`, so expected proceeds and profit stayed `unknown` for
every listing.

## Decisions

1. **Fallback only.** A valuation uses the owner's estimates as proceeds only when its MK
   comparable set has no asking-price or sale evidence at all. Any matching MK asking price or
   verified sale wins, and the estimate is then ignored for that listing.
2. **Same matching rules.** `select_comparables(owner_estimates=True)` selects only
   `owner_estimate` rows with the normal spec 15 rules: same make/model, fuel, gearbox and 2WD
   versus AWD/4WD, engine within tolerance, year and mileage windows. Widening stops at the first
   matching estimate, because each estimate is one reference point per model.
3. **Labelled, never approved.** The proceeds basis is `owner_estimate`. They are labelled "owner
   estimate (the owner's assumption; not MK market evidence)", no negotiation discount applies,
   and research stays needed. The valuation warns `PROCEEDS_FROM_OWNER_ESTIMATE`. An estimate is
   not an approved assumption, so it cannot support a threshold alert (`proceeds_supported`),
   and inquiry readiness still needs real MK comparables.
4. **Canonical names.** The make and model of the listing and of every MK row are compared by
   their taxonomy names (`Skoda`/`Škoda`, `Mokka X`/`Mokka`, `Captur 1.5 dCi`/`Captur`). The
   candidate lookup reads both the listing's own and the canonical names. The comparable
   criteria version is now `mk-comparables@1.2.0`.
5. **The estimate files** are in `data/market/`:
   - `owner_estimates_mk_2026-10-16.json`: the nine confirmed models. A price range is split
     into a low row (older, more km) and a high row (newer, fewer km). A km range uses its
     midpoint. Assumptions the owner did not state are written in each row's provenance (front
     drive where a 4x4 version also exists, and the Qashqai engine power used to separate
     1.5 dCi from 1.6 dCi).
   - `owner_estimates_mk_2026-10-16_to_verify.json`: ix35, Antara, Yeti and S-Cross. These are
     **not to be imported** until the owner confirms them.

   They are imported with `suv-deals market import FILE --evidence-kind owner_estimate
   --owner-user-id <owner> --reason "..." --yes`. Re-importing records nothing new. Estimates
   expire like any MK evidence after `comparable_max_age_days` (90 days). Refreshing one means
   importing a new file with a new `observed_at`.

## Consequences

- Listings of the owner's models get a labelled profit orientation before MK ads are available.
  It is shown as the owner's estimate, never as market evidence.
- The taxonomy (version `2026-10-16.1`) gained Renault Captur, Nissan Juke, Opel Mokka (alias
  Mokka X) and Peugeot 2008 as crossovers, so these models pass the SUV screening.
- A listing whose fuel, gearbox or drive is unknown still matches no estimate (`*_UNVERIFIED`),
  exactly as with MK ads.
