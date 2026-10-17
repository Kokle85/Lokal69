"""The seller-reply opportunity decision (spec 37.7 "notify Vasko when the evidence supports a good
opportunity"; ``workers.reply_handlers.opportunity_decision``), pure and without a database.

The end-to-end tests cannot reach this branch: under the current, unapproved production tax rule
no stored valuation may notify. These cases build SYNTHETIC valuations with the unit-test
builders of ``domain.valuation``:

- a valuation that may not notify (unapproved threshold / incomplete economics, stale) never
  raises an opportunity alert;
- the first notifiable valuation of an inquiry is a ``first_alert``;
- the same evidence as the previously alerted valuation is not material (no routine re-alert).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tests.unit.test_valuation import _alert_ready, active_calc, assemble

from suv_deals.domain.enums import Availability, ValuationState
from suv_deals.domain.notifications import MaterialityReason
from suv_deals.domain.valuation import InvalidationReason, Valuation, mark_stale
from suv_deals.persistence.valuation_repo import StoredValuation
from suv_deals.workers.reply_handlers import alert_state, opportunity_decision


def _stored(valuation: Valuation) -> StoredValuation:
    return StoredValuation.model_construct(
        id=uuid4(),
        listing_id=uuid4(),
        listing_revision_id=uuid4(),
        listing_revision=1,
        valuation=valuation,
        created_at=datetime.now(UTC),
    )


def _notifiable() -> Valuation:
    calc = active_calc()
    valuation = assemble(tax=calc, scenario_set=_alert_ready(calc))
    assert valuation.can_notify
    return valuation


def test_a_valuation_that_may_not_notify_raises_nothing() -> None:
    incomplete = assemble()
    assert not incomplete.can_notify
    assert opportunity_decision(_stored(incomplete), None, Availability.AVAILABLE) is None
    stale = mark_stale(_notifiable(), [InvalidationReason.FRESHNESS_DEADLINE], datetime.now(UTC))
    assert stale.state == ValuationState.STALE
    assert opportunity_decision(_stored(stale), None, Availability.AVAILABLE) is None


def test_the_first_notifiable_valuation_is_a_first_alert() -> None:
    decision = opportunity_decision(_stored(_notifiable()), None, Availability.AVAILABLE)
    assert decision is not None and decision.realert_allowed
    assert decision.reasons == (MaterialityReason.FIRST_ALERT,)


def test_unchanged_evidence_is_not_realerted() -> None:
    valuation = _notifiable()
    previous, current = _stored(valuation), _stored(valuation)
    decision = opportunity_decision(current, previous, Availability.AVAILABLE)
    assert decision is not None and not decision.material and not decision.realert_allowed
    state = alert_state(current, Availability.AVAILABLE)
    assert state.price_eur is not None and state.evidence_valid is True
