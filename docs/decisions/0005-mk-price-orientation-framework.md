# ADR 0005: MK resale prices as an orientation framework

Status: accepted (owner decisions of 2026-10-16). Date: 2026-10-16. Refines decisions 1 and 2 of
[ADR 0004](0004-owner-estimates-as-mk-proceeds-fallback.md).

## Context

The owner decided on 2026-10-16:

- „не треба да бидат строги правилата треба да бида оквир и правила само за да имаме
  орјентација": the MK resale-price rules must not be strict. They are a framework, only for
  orientation.
- „Цените се извадени од Reklama5": the owner's MK reference prices (the `owner_estimate` rows of
  ADR 0004) were read from Reklama5 asking prices.
- The MK asking prices of the owner's models are recorded **daily** from Reklama5 and Pazar3, for
  all engines, gearboxes and trims of a model (not one variant): by the crawler where it can reach
  the site, otherwise by the owner's assistant dot. The same ad is recorded again every day.

Until now the comparable selection (spec 15) was strict: a different fuel, gearbox, drive,
engine, generation, or a year or mileage outside the widened windows excluded an MK ad. With
whole-model daily recordings most ads differ from a given foreign listing in at least one of
these, so most valuations stayed `insufficient_comparables` with unknown proceeds, and the same
ad recorded on several days would have counted several times.

## Decisions

1. **The strict spec-15 set stays unchanged.** `select_comparables` and the persisted
   `ComparableSetResult` keep their rules, statuses and content hash. No field is added to any
   hashed model (`ComparableSetResult`, its members, `ValuationDependencies`), so every stored set
   and valuation still reloads. Inquiry readiness and the ranking's `comparable_quality` keep
   reading the strict set.

2. **A separate relaxed orientation.** `orient_comparables` (`domain/comparables.py`, version
   `mk-orientation@1.0.0`) looks at one evidence kind: MK asking prices or the owner's estimates.
   - Hard (never relaxed): the same canonical make and model, the MK market, the fixture lineage,
     a convertible price, observed on or before `as_of`, not stale (`comparable_max_age_days`),
     condition (a damaged or non-running ad never orients a running car), and duplicates.
   - Everything else is a **labelled difference** (`orientation_*` codes) with a distance
     penalty. All values are **PROPOSED, not statistically validated**:

     | Difference | Distance |
     |---|---|
     | different fuel | 5 |
     | different generation | 3 |
     | engine outside tolerance | 2 |
     | different gearbox category | 2 |
     | 2WD versus AWD/4WD | 2 |
     | pre/post facelift | 1 |
     | fuel, gearbox or drive unknown; ad year or mileage unknown | 1 each |
     | registration year | 1 per year |
     | mileage | 1 per 30,000 km |

     A different fuel alone is above the closeness threshold. A target-side unknown adds the
     same amount to every ad and does not change the order. Unknowns the strict matcher only
     labels (generation, facelift, engine code) score 0.
   - **Closeness** (`OrientationResult.close`, used by decision 3) is tested on the *verified*
     distance: each unknown fuel, gearbox or drive, on either side, counts as the mismatch it may
     hide (5, 2, 2 instead of 1). So an ad of unknown fuel (it may be a petrol) is never "close"
     to a diesel, and an ad about which only make, model and year are known never looks closer
     than a known near-variant. A listing whose own fuel, gearbox or drive is unknown therefore
     never has a close orientation; this does not move its fallback, because the strict owner
     estimates need those fields known too (ADR 0004, `*_UNVERIFIED`), so step 2 is empty and
     step 3 uses the same orientation.
   - The 5 nearest points are kept (`ORIENTATION_LIMIT`). Order: distance, then the newer
     observation, then the id (exact Decimal and integer microseconds; independent of input
     order).
   - The orientation is never persisted as a comparable set and never shown as one.

3. **Fallback order.** Only when the strict set gives **no** proceeds (no MK asking price or sale
   matches) the valuation pipeline uses, in this order:
   1. the asking-price orientation, when some point is verified within `ORIENTATION_CLOSE` = 4
      (PROPOSED; see "Closeness" above);
   2. else the owner's estimates under the strict rules (ADR 0004 decision 2);
   3. else the asking-price orientation even when no point is verified close
      (`ORIENTATION_DISTANT`, recorded as a valuation warning);
   4. else the relaxed orientation over the owner's estimates.

   Real spec-15 evidence always wins. A close MK ad now beats an exact owner estimate, because
   the estimates were themselves read from Reklama5 asking prices; a distant one does not.

4. **Labelled proceeds.** `proceeds_from_orientation`: base = median of the points, low/high =
   quartiles from 5 points, else min/max. Asking prices keep basis `mk_asking_prices`; the
   negotiation discount is passed through once, as for a comparable set. Owner estimates keep
   basis `owner_estimate` and no discount. Orientation proceeds never carry a
   `comparable_set_id`, always need research and are never an approved basis
   (`proceeds_supported` is false). The valuation stays a `research_candidate` even with
   figures (`assemble_valuation(proceeds_fallback=True)`, for every fallback step including the
   strict owner estimates). The valuation records the warnings `PROCEEDS_FROM_MK_ORIENTATION`
   (asking-price basis), `MK_ORIENTATION:<kind>: ...` (points, distance range, the differences
   counted) and the orientation's own labels (`ORIENTATION_ONLY`, `ORIENTATION_DISTANT`,
   `ORIENTATION_EMPTY`, FX notes). `INSUFFICIENT_COMPARABLES` stays, because the strict
   set is still insufficient. The material unknown "insufficient_comparables for an asking-price
   basis" is not added for an orientation (`assemble_valuation(proceeds_orientation=True)`),
   otherwise every such valuation would be `incomplete` without figures. The points' FX rates are
   valuation dependencies like a comparable's.

5. **Freshness.** Proceeds that do not come from the strict set (orientation points and the
   owner's estimates) expire with their oldest used observation plus `comparable_max_age_days`
   (`assemble_valuation(proceeds_fresh_until=...)`). The owner's estimates of 2026-10-16 therefore
   stop orienting valuations after 90 days (about 2027-01-14) unless they are re-imported with a
   new `observed_at`. Owner estimates have no ad URL and no cluster, so they are **never
   deduplicated**: a re-import with revised values is counted together with the old rows until
   the old rows turn stale (the median mixes old and new values and the sample counts both). A
   refresh should therefore re-import only unchanged values, or wait until the old rows are
   stale; a natural-key supersede rule is an open point.

6. **Duplicates by ad URL.** The same ad recorded on several days counts once, in the strict set
   and in the orientation. `ad_url_key` keeps the host (lowercased, without a leading `www.`),
   the path (without a trailing `/`) and the **query string verbatim** (a Reklama5 ad is
   `/AdDetails?ad=<id>`), and drops scheme, port, user info and fragment. Rows of the same
   evidence kind linked by a shared cluster or a shared ad URL form one group (transitively); the
   newest row that passes the hard checks of the mode is kept (strict set: no hard reason and no
   window failure; orientation: same model and condition, preferring a row the strict set can use,
   so both keep the same row of an ad), and only when no row passes, the
   newest one (equal times: the smallest id). The others are excluded as
   `DUPLICATE_OF_CLUSTER` and/or `DUPLICATE_OF_URL`. A newer row without a price, or a newer
   re-record that lost its mileage or has a model typo, never hides a valid older one. Rows without a URL (owner estimates) are never URL duplicates. The criteria
   version is `mk-comparables@1.3.0`; stored sets keep their own version and reload unchanged.

7. **Read window.** The valuation reads MK candidates within ±10 registration years (the
   repository maximum), so the orientation sees other model years. The strict set records them
   as `YEAR_OUT_OF_WINDOW` exclusions.

## What the orientation does not do

- **No inquiry readiness.** Inquiry readiness still needs a strict set with MK evidence
  (`INSUFFICIENT_COMPARABLES` stays). Orientation and owner-estimate proceeds are not market
  evidence: they never make a candidate `KNOWN_COSTS_EXCEED_PROCEEDS` (not eligible); the
  readiness sees their proceeds as unknown (`CostEvidence.from_scenario_set(proceeds=...)`, keyed
  on the missing `comparable_set_id`).
- **No alerts.** An orientation valuation is never alert eligible.
- **No "supported" economics.** A valuation from any fallback stays a research candidate even
  with figures: the candidate list and detail, the review queue and the MCP typed warnings
  (`RESEARCH_CANDIDATE`) read the stored valuation's own `research_candidate` flag, not only its
  state. The 15-day evaluation's "best supported economics" counts only well-matched
  candidates (an adequate or small-sample strict set), so orientation figures never appear there.
- **No comparable set.** `deals_get_comparables` and the dashboard's comparable view show only the
  strict set. The orientation is visible through the valuation warnings.
- The ranking's `conservative_scenario` points use the valuation figures as before (as for the
  owner's estimates since ADR 0004); `comparable_quality` stays 0 for an insufficient strict set.

## Consequences and open points

- Valuations of the owner's models get a labelled MK orientation as soon as same-model ads are
  recorded, even when no ad matches the exact variant.
- The penalties, the limit and the closeness threshold are proposals. A loose median can mix near
  and far points; a band (best distance + 2) can be considered once real recordings exist.
- Recording new MK rows does not by itself re-value existing listings; they are re-valued on the
  next revision, configuration or FX change, or when their freshness deadline passes.
- MK rows priced in MKD are only usable with a recorded EUR/MKD reference rate.
- The stored proceeds view does not yet say "orientation" next to an asking-price basis; the
  valuation warnings do.
- **Open (owner decision): the same Reklama5 price is treated two ways.** Stored as
  `asking_price`, it gets the configured negotiation discount and the asking-price labels; stored
  as `owner_estimate` (the owner's reference prices, also read from Reklama5), it gets no
  discount and keeps the ADR 0004 label "owner estimate (the owner's assumption; not MK market
  evidence)", because the `owner_estimate` basis forbids a discount. Once the owner sets a
  discount, steps 2 and 4 therefore overstate realized proceeds by that discount compared with
  steps 1 and 3. The `PROCEEDS_FROM_OWNER_ESTIMATE` warning now says that the reference prices
  were read from Reklama5 asking prices and that no discount is applied; whether to apply the
  discount to these estimates (or relabel them) is left to the owner.
