from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
)
from cheatgame.digital_products.services.payment_failures import (
    terminate_locked_definitive_unpaid_digital_graph,
)
from cheatgame.financial_core.models import (
    CallbackReceipt,
    CommercialFinalization,
    FinancialActorType,
    FinancialAllocation,
    Payment,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransaction,
    PaymentTransactionStatus,
    ProviderEvent,
    ProviderRequestOutcome,
    ProviderRequestResult,
    ReviewAction,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseStatus,
    Verification,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.locks import (
    LockRank,
    lock_many,
    lock_one,
    ordered_lock_scope,
    register_lock,
)
from cheatgame.financial_core.services.state_machines import (
    assert_payment_attempt_transition,
    assert_payment_transaction_transition,
    assert_payment_transition,
    assert_review_case_transition,
)
from cheatgame.shop.models import Cart, CartLockReason, Checkout, Order
from cheatgame.users.models import BaseUser, UserTypes


RECONCILIATION_NAMESPACE = UUID("48489ba1-cf8d-409d-a1e5-75dca30632d1")
MINIMUM_PROVIDER_HISTORY_AGE = timedelta(minutes=30)


class ProviderRequestReconciliationError(ValidationError):
    pass


@dataclass(frozen=True)
class NoAuthorityReconciliationResult:
    transaction_id: int
    review_case_id: int
    released_reservation_ids: tuple[int, ...]
    replayed: bool


def _staff_actor(actor):
    if not isinstance(actor, BaseUser) or not actor.pk:
        raise PermissionDenied("An authenticated finance reviewer is required.")
    persisted = BaseUser.objects.filter(pk=actor.pk).values("is_active", "user_type").first()
    if not persisted or not persisted["is_active"] or persisted["user_type"] not in (
        UserTypes.ADMIN,
        UserTypes.MANAGER,
    ):
        raise PermissionDenied("An active Admin or Manager finance reviewer is required.")
    return actor.pk


def _validate_evidence(*, evidence_sha256, observed_at):
    digest = str(evidence_sha256).lower().strip()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ProviderRequestReconciliationError("Provider evidence requires a SHA-256 digest.")
    if observed_at is None or timezone.is_naive(observed_at):
        raise ProviderRequestReconciliationError("Provider evidence observation time must be timezone-aware.")
    if observed_at > timezone.now() + timedelta(minutes=5):
        raise ProviderRequestReconciliationError("Provider evidence observation time cannot be in the future.")
    return digest


@transaction.atomic
def reconcile_no_authority_created(
    *,
    transaction_public_id,
    actor,
    evidence_sha256,
    observed_at,
    idempotency_key,
):
    """Close one request-transport ambiguity using authoritative provider-history evidence."""
    actor_id = _staff_actor(actor)
    digest = _validate_evidence(evidence_sha256=evidence_sha256, observed_at=observed_at)
    key = UUID(str(idempotency_key))
    tx_ref = PaymentTransaction.objects.select_related(
        "attempt__payment__order__checkout__cart"
    ).get(public_id=transaction_public_id)
    attempt_ref = tx_ref.attempt
    payment_ref = attempt_ref.payment
    order_ref = payment_ref.order
    checkout_ref = order_ref.checkout
    cart_ref = checkout_ref.cart

    with ordered_lock_scope():
        cart = lock_one(queryset=Cart.objects.all(), rank=LockRank.CART, pk=cart_ref.pk)
        checkout = lock_one(queryset=Checkout.objects.all(), rank=LockRank.CHECKOUT, pk=checkout_ref.pk)
        order = lock_one(queryset=Order.objects.all(), rank=LockRank.PAYABLE, pk=order_ref.pk)
        payment = lock_one(queryset=Payment.objects.all(), rank=LockRank.PAYMENT, pk=payment_ref.pk)
        attempts = lock_many(
            queryset=PaymentAttempt.objects.all(),
            rank=LockRank.PAYMENT_ATTEMPT,
            pks=PaymentAttempt.objects.filter(payment=payment).values_list("pk", flat=True),
        )
        attempt = next(item for item in attempts if item.pk == attempt_ref.pk)
        transactions = lock_many(
            queryset=PaymentTransaction.objects.all(),
            rank=LockRank.PAYMENT_TRANSACTION,
            pks=PaymentTransaction.objects.filter(attempt__payment=payment).values_list("pk", flat=True),
        )
        transaction_obj = next(item for item in transactions if item.pk == tx_ref.pk)
        reservations = lock_many(
            queryset=DigitalInventoryReservation.objects.all(),
            rank=LockRank.RESERVATION,
            pks=DigitalInventoryReservation.objects.filter(order=order).values_list("pk", flat=True),
        )
        reviews = lock_many(
            queryset=ReviewCase.objects.all(),
            rank=LockRank.REVIEW_CASE,
            pks=ReviewCase.objects.filter(transaction=transaction_obj).values_list("pk", flat=True),
        )
        review = next(
            (
                item
                for item in reviews
                if item.reason == ReviewCaseReason.PROVIDER_STATE_UNCLEAR
            ),
            None,
        )
        if review is None:
            raise ProviderRequestReconciliationError("The provider-state ReviewCase is missing.")

        action = ReviewAction.objects.filter(idempotency_key=key).first()
        if action is not None:
            if (
                action.review_case_id != review.pk
                or action.action_type != "transition:resolved"
                or digest not in action.note
            ):
                raise ProviderRequestReconciliationError("Reconciliation idempotency key conflicts.")
            return NoAuthorityReconciliationResult(
                transaction_obj.pk,
                review.pk,
                tuple(
                    sorted(
                        DigitalInventoryReservation.objects.filter(
                            order=order,
                            state=DigitalInventoryReservationState.RELEASED,
                            resolution_reason="provider_no_authority",
                        ).values_list("pk", flat=True)
                    )
                ),
                True,
            )

        if observed_at < transaction_obj.created_at + MINIMUM_PROVIDER_HISTORY_AGE:
            raise ProviderRequestReconciliationError("Provider history was inspected before the safety window elapsed.")
        if (
            transaction_obj.status != PaymentTransactionStatus.OUTCOME_UNKNOWN
            or attempt.status != PaymentAttemptStatus.OUTCOME_UNKNOWN
            or payment.collection_status != PaymentCollectionStatus.REVIEW
            or review.status not in (
                ReviewCaseStatus.OPEN,
                ReviewCaseStatus.INVESTIGATING,
                ReviewCaseStatus.APPROVAL_PENDING,
            )
        ):
            raise ProviderRequestReconciliationError("The payment graph is not an unresolved request ambiguity.")
        if transaction_obj.provider_authority or transaction_obj.provider_reference:
            raise ProviderRequestReconciliationError("Provider identity evidence blocks no-authority reconciliation.")
        request_results = tuple(ProviderRequestResult.objects.filter(transaction=transaction_obj))
        if not request_results or any(
            result.outcome != ProviderRequestOutcome.OUTCOME_UNKNOWN
            or result.reason_code not in ("provider_timeout", "provider_transport_failure")
            or result.safe_metadata.get("result_category") != "transport_uncertain"
            for result in request_results
        ):
            raise ProviderRequestReconciliationError(
                "The request result is not a transport-uncertain provider initiation."
            )
        if (
            CallbackReceipt.objects.filter(correlation_id=transaction_obj.correlation_id).exists()
            or ProviderEvent.objects.filter(transaction=transaction_obj).exists()
            or Verification.objects.filter(transaction=transaction_obj).exists()
            or FinancialAllocation.objects.filter(payment=payment).exists()
            or CommercialFinalization.objects.filter(payment=payment).exists()
        ):
            raise ProviderRequestReconciliationError("Stronger financial evidence blocks no-authority reconciliation.")
        if not reservations or any(
            item.state != DigitalInventoryReservationState.HELD_FOR_REVIEW for item in reservations
        ):
            raise ProviderRequestReconciliationError("The review reservation graph is incoherent.")

        assert_payment_transaction_transition(transaction_obj.status, PaymentTransactionStatus.EXPIRED)
        assert_payment_attempt_transition(attempt.status, PaymentAttemptStatus.DEFINITIVE_FAILED)
        assert_payment_transition(payment.collection_status, PaymentCollectionStatus.OPEN)
        now = timezone.now()
        transaction_obj.status = PaymentTransactionStatus.EXPIRED
        transaction_obj.completed_at = now
        transaction_obj.version += 1
        transaction_obj.save(update_fields=("status", "completed_at", "version", "updated_at"))
        attempt.status = PaymentAttemptStatus.DEFINITIVE_FAILED
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))

        ReviewAction.objects.create(
            review_case=review,
            action_type="transition:resolved",
            actor_type=FinancialActorType.RECONCILIATION,
            actor_id=actor_id,
            reason_code="provider_no_authority",
            note=(
                f"provider={transaction_obj.provider} classification=NO_AUTHORITY_CREATED "
                f"observed_at={observed_at.isoformat()} evidence_sha256={digest}"
            ),
            idempotency_key=key,
        )
        assert_review_case_transition(review.status, ReviewCaseStatus.RESOLVED)
        review.status = ReviewCaseStatus.RESOLVED
        review.resolution_code = "provider_no_authority"
        review.resolved_at = now
        review.version += 1
        review.save(
            update_fields=("status", "resolution_code", "resolved_at", "version", "updated_at")
        )

        payment.collection_status = PaymentCollectionStatus.OPEN
        payment.version += 1
        payment.save(update_fields=("collection_status", "version", "updated_at"))

        termination = terminate_locked_definitive_unpaid_digital_graph(
            cart=cart,
            checkout=checkout,
            order=order,
            payment=payment,
            attempt=attempt,
            transaction_obj=transaction_obj,
            reason_code="provider_no_authority",
            idempotency_identity=key,
            locked_reservations=reservations,
            expected_reservation_state=DigitalInventoryReservationState.HELD_FOR_REVIEW,
            allowed_cart_lock_reasons=(CartLockReason.MANUAL_REVIEW,),
            reservation_resolution_reason="provider_no_authority",
        )

        register_lock(LockRank.EVENT_OUTBOX, f"provider-no-authority:{transaction_obj.pk:020d}")
        append_financial_event(
            aggregate_type=transaction_obj._meta.label_lower,
            aggregate_id=transaction_obj.public_id,
            aggregate_version=transaction_obj.version,
            event_type="provider_request.no_authority_reconciled",
            actor_type=FinancialActorType.RECONCILIATION,
            actor_id=actor_id,
            idempotency_key=f"provider-no-authority:{key}",
            correlation_id=transaction_obj.correlation_id,
            causation_id=review.public_id,
            metadata={
                "previous_status": PaymentTransactionStatus.OUTCOME_UNKNOWN,
                "new_status": PaymentTransactionStatus.EXPIRED,
                "reason_code": "provider_no_authority",
                "evidence_sha256": digest,
            },
        )
        return NoAuthorityReconciliationResult(
            transaction_obj.pk,
            review.pk,
            termination.released_reservation_ids,
            False,
        )
