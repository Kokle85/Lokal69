"""``api.routes.resume_warnings``: why a resumed source may still run no new network work.

Pure unit tests (no database): every branch of the documented warning contract (docs/api_contract.md
section 6), so a resume of an ``access_blocked`` or ``parser_unhealthy`` source points the owner at
the technical-status action and never at a configuration revision. SYNTHETIC data only.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from suv_deals.api.routes import resume_warnings
from suv_deals.domain.enums import TechnicalStatus
from suv_deals.views import SourceResumeResult
from suv_deals.views.operations import network_work_allowed

SOURCE_ID = UUID("00000000-0000-4000-8000-0000000000a1")


def _result(*, enabled: bool, status: TechnicalStatus) -> SourceResumeResult:
    return SourceResumeResult(
        source_id=SOURCE_ID,
        source_key="synthetic_source",
        was_paused=True,
        version=5,
        enabled=enabled,
        technical_status=status,
        network_allowed=network_work_allowed(enabled=enabled, paused=False, technical_status=status),
        requeued_jobs=1,
    )


@pytest.mark.parametrize("status", [TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY])
def test_a_technical_block_is_source_blocked_never_activation_blocked(status: TechnicalStatus) -> None:
    # A block disables the source by itself (enabled=false), yet the warning names the block.
    warnings = resume_warnings(_result(enabled=False, status=status))
    assert [w.code.value for w in warnings] == ["SOURCE_BLOCKED"]
    assert status.value in warnings[0].message
    assert "technical-status action" in warnings[0].message


def test_a_source_the_configuration_does_not_enable_is_activation_blocked() -> None:
    warnings = resume_warnings(_result(enabled=False, status=TechnicalStatus.LIVE_SMOKE_PASSED))
    assert [w.code.value for w in warnings] == ["ACTIVATION_BLOCKED"]
    assert "sources sync" in warnings[0].message


def test_an_enabled_unblocked_source_has_no_warning() -> None:
    assert resume_warnings(_result(enabled=True, status=TechnicalStatus.LIVE_SMOKE_PASSED)) == []
