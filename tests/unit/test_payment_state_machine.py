import itertools

import pytest

from app.errors import Conflict
from app.models import Payment, PaymentStatus
from app.services.payments import ALLOWED_TRANSITIONS, FINAL_STATUSES, can_transition, transition

S = PaymentStatus

# The spec's state machine, written out independently of the implementation.
SPEC_TRANSITIONS = {
    (S.CREATED, S.PROCESSING),
    (S.CREATED, S.EXPIRED),
    (S.PROCESSING, S.SUCCEEDED),
    (S.PROCESSING, S.FAILED),
}


@pytest.mark.parametrize(("current", "target"), list(itertools.product(S, S)))
def test_exactly_the_spec_transitions_are_allowed(current, target):
    assert can_transition(current, target) == ((current, target) in SPEC_TRANSITIONS)


def test_every_status_is_covered():
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_final_statuses_have_no_way_out():
    assert {S.SUCCEEDED, S.FAILED, S.EXPIRED} == FINAL_STATUSES


def test_transition_moves_the_payment():
    payment = Payment(status=S.CREATED)
    transition(payment, S.PROCESSING)
    assert payment.status is S.PROCESSING


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.FAILED, S.SUCCEEDED),
        (S.SUCCEEDED, S.FAILED),
        (S.EXPIRED, S.PROCESSING),
        (S.PROCESSING, S.CREATED),
        (S.CREATED, S.SUCCEEDED),  # can't skip the bank
        (S.SUCCEEDED, S.SUCCEEDED),
    ],
)
def test_invalid_transition_raises_conflict_and_leaves_status(current, target):
    payment = Payment(status=current)
    with pytest.raises(Conflict) as exc_info:
        transition(payment, target)
    assert exc_info.value.code == "invalid_state_transition"
    assert payment.status is current
