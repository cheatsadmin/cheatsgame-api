from django.core.exceptions import ValidationError

from cheatgame.financial_core.models import (
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
    ReconciliationFindingStatus,
    ReconciliationRunStatus,
    ReviewCaseStatus,
)


class InvalidFinancialTransition(ValidationError):
    pass


PAYMENT_TRANSITIONS = {
    PaymentCollectionStatus.OPEN: {
        PaymentCollectionStatus.PROCESSING,
        PaymentCollectionStatus.REVIEW,
        PaymentCollectionStatus.CANCELED,
    },
    PaymentCollectionStatus.PROCESSING: {
        PaymentCollectionStatus.OPEN,
        PaymentCollectionStatus.PARTIALLY_PAID,
        PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        PaymentCollectionStatus.REVIEW,
        PaymentCollectionStatus.CANCELED,
    },
    PaymentCollectionStatus.PARTIALLY_PAID: {
        PaymentCollectionStatus.PROCESSING,
        PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        PaymentCollectionStatus.REVIEW,
    },
    PaymentCollectionStatus.PAID_PENDING_FINALIZATION: {
        PaymentCollectionStatus.PAID,
        PaymentCollectionStatus.REVIEW,
    },
    PaymentCollectionStatus.REVIEW: {
        PaymentCollectionStatus.OPEN,
        PaymentCollectionStatus.PROCESSING,
        PaymentCollectionStatus.PARTIALLY_PAID,
        PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        PaymentCollectionStatus.CANCELED,
    },
    PaymentCollectionStatus.PAID: set(),
    PaymentCollectionStatus.CANCELED: set(),
}


PAYMENT_ATTEMPT_TRANSITIONS = {
    PaymentAttemptStatus.CREATED: {
        PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION,
        PaymentAttemptStatus.PROCESSING,
        PaymentAttemptStatus.DEFINITIVE_FAILED,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
        PaymentAttemptStatus.REVIEW,
    },
    PaymentAttemptStatus.REQUIRES_CUSTOMER_ACTION: {
        PaymentAttemptStatus.PROCESSING,
        PaymentAttemptStatus.DEFINITIVE_FAILED,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
        PaymentAttemptStatus.REVIEW,
    },
    PaymentAttemptStatus.PROCESSING: {
        PaymentAttemptStatus.SUCCEEDED,
        PaymentAttemptStatus.DEFINITIVE_FAILED,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
        PaymentAttemptStatus.REVIEW,
    },
    PaymentAttemptStatus.OUTCOME_UNKNOWN: {
        PaymentAttemptStatus.PROCESSING,
        PaymentAttemptStatus.SUCCEEDED,
        PaymentAttemptStatus.DEFINITIVE_FAILED,
        PaymentAttemptStatus.REVIEW,
    },
    PaymentAttemptStatus.REVIEW: {
        PaymentAttemptStatus.PROCESSING,
        PaymentAttemptStatus.SUCCEEDED,
        PaymentAttemptStatus.DEFINITIVE_FAILED,
        PaymentAttemptStatus.OUTCOME_UNKNOWN,
    },
    PaymentAttemptStatus.SUCCEEDED: set(),
    PaymentAttemptStatus.DEFINITIVE_FAILED: set(),
}


PAYMENT_TRANSACTION_TRANSITIONS = {
    PaymentTransactionStatus.CREATED: {
        PaymentTransactionStatus.REQUESTING,
        PaymentTransactionStatus.CANCELED,
    },
    PaymentTransactionStatus.REQUESTING: {
        PaymentTransactionStatus.PENDING_CUSTOMER,
        PaymentTransactionStatus.PENDING_PROVIDER,
        PaymentTransactionStatus.SUCCEEDED,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.PENDING_CUSTOMER: {
        PaymentTransactionStatus.CALLBACK_RECEIVED,
        PaymentTransactionStatus.VERIFYING,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.PENDING_PROVIDER: {
        PaymentTransactionStatus.CALLBACK_RECEIVED,
        PaymentTransactionStatus.VERIFYING,
        PaymentTransactionStatus.SUCCEEDED,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.CALLBACK_RECEIVED: {
        PaymentTransactionStatus.VERIFYING,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.VERIFYING: {
        PaymentTransactionStatus.SUCCEEDED,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.OUTCOME_UNKNOWN: {
        PaymentTransactionStatus.VERIFYING,
        PaymentTransactionStatus.SUCCEEDED,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
        PaymentTransactionStatus.REVIEW,
    },
    PaymentTransactionStatus.REVIEW: {
        PaymentTransactionStatus.VERIFYING,
        PaymentTransactionStatus.SUCCEEDED,
        PaymentTransactionStatus.DECLINED,
        PaymentTransactionStatus.CANCELED,
        PaymentTransactionStatus.EXPIRED,
        PaymentTransactionStatus.OUTCOME_UNKNOWN,
    },
    PaymentTransactionStatus.SUCCEEDED: set(),
    PaymentTransactionStatus.DECLINED: set(),
    PaymentTransactionStatus.CANCELED: set(),
    PaymentTransactionStatus.EXPIRED: set(),
}


REVIEW_CASE_TRANSITIONS = {
    ReviewCaseStatus.OPEN: {
        ReviewCaseStatus.INVESTIGATING,
        ReviewCaseStatus.APPROVAL_PENDING,
        ReviewCaseStatus.RESOLVED,
        ReviewCaseStatus.CANCELED,
    },
    ReviewCaseStatus.INVESTIGATING: {
        ReviewCaseStatus.APPROVAL_PENDING,
        ReviewCaseStatus.RESOLVED,
        ReviewCaseStatus.CANCELED,
    },
    ReviewCaseStatus.APPROVAL_PENDING: {
        ReviewCaseStatus.INVESTIGATING,
        ReviewCaseStatus.RESOLVED,
        ReviewCaseStatus.CANCELED,
    },
    ReviewCaseStatus.RESOLVED: set(),
    ReviewCaseStatus.CANCELED: set(),
}


RECONCILIATION_RUN_TRANSITIONS = {
    ReconciliationRunStatus.CREATED: {ReconciliationRunStatus.RUNNING, ReconciliationRunStatus.FAILED},
    ReconciliationRunStatus.RUNNING: {ReconciliationRunStatus.COMPLETED, ReconciliationRunStatus.FAILED},
    ReconciliationRunStatus.COMPLETED: set(),
    ReconciliationRunStatus.FAILED: set(),
}


RECONCILIATION_FINDING_TRANSITIONS = {
    ReconciliationFindingStatus.OPEN: {
        ReconciliationFindingStatus.REVIEWING,
        ReconciliationFindingStatus.RESOLVED,
        ReconciliationFindingStatus.ACCEPTED,
    },
    ReconciliationFindingStatus.REVIEWING: {
        ReconciliationFindingStatus.RESOLVED,
        ReconciliationFindingStatus.ACCEPTED,
    },
    ReconciliationFindingStatus.RESOLVED: set(),
    ReconciliationFindingStatus.ACCEPTED: set(),
}


def assert_transition(*, machine, current, target, entity):
    if current == target:
        return
    if target not in machine.get(current, set()):
        raise InvalidFinancialTransition(f"Invalid {entity} transition: {current} -> {target}.")


def assert_payment_transition(current, target):
    assert_transition(machine=PAYMENT_TRANSITIONS, current=current, target=target, entity="Payment")


def assert_payment_attempt_transition(current, target):
    assert_transition(
        machine=PAYMENT_ATTEMPT_TRANSITIONS,
        current=current,
        target=target,
        entity="PaymentAttempt",
    )


def assert_payment_transaction_transition(current, target):
    assert_transition(
        machine=PAYMENT_TRANSACTION_TRANSITIONS,
        current=current,
        target=target,
        entity="PaymentTransaction",
    )


def assert_review_case_transition(current, target):
    assert_transition(machine=REVIEW_CASE_TRANSITIONS, current=current, target=target, entity="ReviewCase")


def assert_reconciliation_run_transition(current, target):
    assert_transition(
        machine=RECONCILIATION_RUN_TRANSITIONS,
        current=current,
        target=target,
        entity="ReconciliationRun",
    )


def assert_reconciliation_finding_transition(current, target):
    assert_transition(
        machine=RECONCILIATION_FINDING_TRANSITIONS,
        current=current,
        target=target,
        entity="ReconciliationFinding",
    )
