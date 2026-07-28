from threading import Barrier, Lock, Thread
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from cheatgame.financial_core.models import (
    ExceptionalRecognitionAuthorization,
    ExceptionalRecognitionAuthorizationStatus,
    FinancialActorType,
    FinancialAllocation,
    LatePaymentAdjudication,
    LatePaymentAdjudicationDecision,
    LatePaymentAdjudicationStatus,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
    ReviewCase,
    ReviewCaseReason,
    ReviewAction,
    VerificationTriggerSource,
)
from cheatgame.financial_core.services.funds_application import (
    FundsApplicationBlocked,
    recognize_verified_funds,
)
from cheatgame.financial_core.services.late_payment_adjudication import (
    LatePaymentAdjudicationError,
    cancel_terminal_late_payment_adjudication,
    check_terminal_late_payment_decision,
    ensure_terminal_late_payment_adjudication,
    propose_terminal_late_payment_decision,
)
from cheatgame.financial_core.services.verification import apply_verification_result
from cheatgame.financial_core.test_provider_execution_phase1 import (
    ProviderExecutionPhase1Fixture,
)
from cheatgame.users.models import UserTypes


class TerminalLatePaymentAdjudicationTests(ProviderExecutionPhase1Fixture, TransactionTestCase):
    reset_sequences = True

    def reviewer(self, suffix, *, user_type=UserTypes.MANAGER, active=True):
        user = self.make_user()
        user.phone_number = f"09350000{int(suffix):03d}"
        user.user_type = user_type
        user.is_active = active
        user.save(update_fields=("phone_number", "user_type", "is_active", "updated_at"))
        return user

    def terminal_success(self):
        placement, account, attempt, transaction_obj = self.make_pending_graph()
        transaction_obj.status = PaymentTransactionStatus.DECLINED
        transaction_obj.completed_at = timezone.now()
        transaction_obj.version += 1
        transaction_obj.save(update_fields=("status", "completed_at", "version", "updated_at"))
        attempt.status = PaymentAttemptStatus.DEFINITIVE_FAILED
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))
        placement.payment.collection_status = PaymentCollectionStatus.OPEN
        placement.payment.version += 1
        placement.payment.save(update_fields=("collection_status", "version", "updated_at"))
        _, claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        placement.payment.refresh_from_db()
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        adjudication = LatePaymentAdjudication.objects.get(verification=verification)
        return placement, account, attempt, transaction_obj, verification, adjudication

    def approve(self, adjudication):
        maker = self.reviewer("1")
        checker = self.reviewer("2", user_type=UserTypes.ADMIN)
        proposed = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Authenticated settlement evidence matches the exact provider operation.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        return check_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            approve_proposal=True,
            rationale="Independent evidence and ownership checks passed.",
            actor=checker,
            expected_proposal_version=proposed.proposal_version,
            idempotency_key=uuid4(),
        )

    def test_nominal_success_does_not_create_terminal_adjudication(self):
        *_, verification = self.successful_verification()
        self.assertFalse(LatePaymentAdjudication.objects.filter(verification=verification).exists())

    def test_terminal_success_creates_one_review_and_adjudication(self):
        _, _, attempt, transaction_obj, verification, adjudication = self.terminal_success()
        self.assertEqual(adjudication.status, LatePaymentAdjudicationStatus.OPEN)
        self.assertEqual(adjudication.review_case.reason, ReviewCaseReason.LATE_PAYMENT)
        self.assertEqual(
            ReviewCase.objects.filter(
                transaction=transaction_obj,
                reason=ReviewCaseReason.LATE_PAYMENT,
            ).count(),
            1,
        )
        self.assertEqual(attempt.status, PaymentAttemptStatus.DEFINITIVE_FAILED)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.DECLINED)
        self.assertEqual(verification.transaction_id, transaction_obj.pk)

    def test_duplicate_detection_reuses_exact_case_and_adjudication(self):
        _, _, _, transaction_obj, verification, adjudication = self.terminal_success()
        reused, created = ensure_terminal_late_payment_adjudication(
            verification=verification,
            review_case=adjudication.review_case,
        )
        self.assertFalse(created)
        self.assertEqual(reused.pk, adjudication.pk)
        self.assertEqual(
            LatePaymentAdjudication.objects.filter(verification=verification).count(),
            1,
        )
        self.assertEqual(
            ReviewCase.objects.filter(
                transaction=transaction_obj,
                reason=ReviewCaseReason.LATE_PAYMENT,
            ).count(),
            1,
        )

    def test_maker_checker_acceptance_creates_exact_authorization(self):
        _, _, _, transaction_obj, verification, adjudication = self.terminal_success()
        approved = self.approve(adjudication)
        authorization = ExceptionalRecognitionAuthorization.objects.get(adjudication=approved)
        self.assertEqual(approved.status, LatePaymentAdjudicationStatus.APPROVED)
        self.assertNotEqual(approved.maker_id, approved.checker_id)
        self.assertEqual(authorization.verification_id, verification.pk)
        self.assertEqual(authorization.transaction_id, transaction_obj.pk)
        self.assertEqual(authorization.amount, verification.canonical_allocation_amount)
        self.assertEqual(authorization.provider_reference, verification.provider_reference)
        self.assertEqual(
            authorization.status,
            ExceptionalRecognitionAuthorizationStatus.AUTHORIZED,
        )

    def test_unauthorized_or_same_actor_cannot_approve(self):
        *_, adjudication = self.terminal_success()
        customer = self.reviewer("3", user_type=UserTypes.CUSTOMER)
        with self.assertRaises(PermissionDenied):
            propose_terminal_late_payment_decision(
                adjudication_public_id=adjudication.public_id,
                decision=LatePaymentAdjudicationDecision.ACCEPT,
                rationale="Not authorized.",
                actor=customer,
                idempotency_key=uuid4(),
            )
        maker = self.reviewer("4")
        proposed = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Maker proposal.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        with self.assertRaises(PermissionDenied):
            check_terminal_late_payment_decision(
                adjudication_public_id=adjudication.public_id,
                approve_proposal=True,
                rationale="Self approval.",
                actor=maker,
                expected_proposal_version=proposed.proposal_version,
                idempotency_key=uuid4(),
            )

    def test_changed_proposal_creates_new_append_only_maker_action(self):
        *_, adjudication = self.terminal_success()
        first_maker = self.reviewer("13")
        second_maker = self.reviewer("14", user_type=UserTypes.ADMIN)
        first = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Initial exact-evidence acceptance proposal.",
            actor=first_maker,
            idempotency_key=uuid4(),
        )
        second = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.REJECT,
            rationale="New evidence requires a replacement proposal.",
            actor=second_maker,
            idempotency_key=uuid4(),
        )
        self.assertEqual(second.proposal_version, first.proposal_version + 1)
        self.assertEqual(second.maker_id, second_maker.pk)
        self.assertEqual(
            ReviewAction.objects.filter(
                review_case=adjudication.review_case,
                action_type__startswith="late_payment.maker_proposal:",
            ).count(),
            2,
        )

    def test_checker_can_reject_acceptance_proposal_and_decision_is_terminal(self):
        *_, adjudication = self.terminal_success()
        maker = self.reviewer("15")
        checker = self.reviewer("16", user_type=UserTypes.ADMIN)
        proposal = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Maker accepts the evidence.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        rejected = check_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            approve_proposal=False,
            rationale="Checker rejects the proposal.",
            actor=checker,
            expected_proposal_version=proposal.proposal_version,
            idempotency_key=uuid4(),
        )
        self.assertEqual(rejected.status, LatePaymentAdjudicationStatus.REJECTED)
        self.assertFalse(ExceptionalRecognitionAuthorization.objects.exists())
        with self.assertRaises(LatePaymentAdjudicationError):
            propose_terminal_late_payment_decision(
                adjudication_public_id=adjudication.public_id,
                decision=LatePaymentAdjudicationDecision.ACCEPT,
                rationale="Attempted terminal reversal.",
                actor=maker,
                idempotency_key=uuid4(),
            )

    def test_concurrent_identical_checker_approval_converges_once(self):
        *_, adjudication = self.terminal_success()
        maker = self.reviewer("18")
        checker = self.reviewer("19", user_type=UserTypes.ADMIN)
        proposal = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Concurrent acceptance proposal.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        key = uuid4()
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = check_terminal_late_payment_decision(
                    adjudication_public_id=adjudication.public_id,
                    approve_proposal=True,
                    rationale="Concurrent independent approval.",
                    actor=checker,
                    expected_proposal_version=proposal.proposal_version,
                    idempotency_key=key,
                )
                outcomes.append(("ok", result.pk))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(timeout=30)
        self.assertTrue(all(not item.is_alive() for item in threads))
        self.assertEqual(outcomes, [("ok", adjudication.pk), ("ok", adjudication.pk)])
        self.assertEqual(ExceptionalRecognitionAuthorization.objects.count(), 1)
        self.assertEqual(
            ReviewAction.objects.filter(idempotency_key=key).count(),
            1,
        )

    def test_open_case_can_be_canceled_but_approved_case_cannot(self):
        *_, open_case = self.terminal_success()
        reviewer = self.reviewer("17")
        canceled = cancel_terminal_late_payment_adjudication(
            adjudication_public_id=open_case.public_id,
            reason="Provider confirms that no settlement exists.",
            actor=reviewer,
            idempotency_key=uuid4(),
        )
        self.assertEqual(canceled.status, LatePaymentAdjudicationStatus.CANCELED)

        *_, another = self.terminal_success()
        approved = self.approve(another)
        with self.assertRaises(LatePaymentAdjudicationError):
            cancel_terminal_late_payment_adjudication(
                adjudication_public_id=approved.public_id,
                reason="Attempted cancellation after approval.",
                actor=reviewer,
                idempotency_key=uuid4(),
            )

    def test_rejection_creates_no_recognition_authority(self):
        *_, adjudication = self.terminal_success()
        maker = self.reviewer("5")
        checker = self.reviewer("6", user_type=UserTypes.ADMIN)
        proposed = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.REJECT,
            rationale="Evidence is insufficient.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        rejected = check_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            approve_proposal=True,
            rationale="Rejection independently confirmed.",
            actor=checker,
            expected_proposal_version=proposed.proposal_version,
            idempotency_key=uuid4(),
        )
        self.assertEqual(rejected.status, LatePaymentAdjudicationStatus.REJECTED)
        self.assertFalse(ExceptionalRecognitionAuthorization.objects.exists())
        self.assertFalse(FinancialAllocation.objects.exists())

    def test_terminal_recognition_requires_and_consumes_exact_authorization(self):
        placement, account, attempt, transaction_obj, verification, adjudication = self.terminal_success()
        self.accounting_policy(account)
        with self.assertRaises(FundsApplicationBlocked):
            recognize_verified_funds(
                verification_id=verification.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )
        approved = self.approve(adjudication)
        placement.payment.refresh_from_db()
        result = recognize_verified_funds(
            verification_id=verification.pk,
            idempotency_key=uuid4(),
            expected_payment_version=placement.payment.version,
            correlation_id=uuid4(),
            actor_type=FinancialActorType.RECONCILIATION,
            actor_id=approved.checker_id,
        )
        attempt.refresh_from_db()
        transaction_obj.refresh_from_db()
        placement.payment.refresh_from_db()
        authorization = ExceptionalRecognitionAuthorization.objects.get(adjudication=approved)
        self.assertEqual(result.allocation.verification_id, verification.pk)
        self.assertEqual(attempt.status, PaymentAttemptStatus.DEFINITIVE_FAILED)
        self.assertEqual(transaction_obj.status, PaymentTransactionStatus.DECLINED)
        self.assertEqual(
            placement.payment.collection_status,
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        )
        self.assertEqual(authorization.status, ExceptionalRecognitionAuthorizationStatus.APPLIED)
        self.assertEqual(authorization.allocation_id, result.allocation.pk)
        self.assertEqual(FinancialAllocation.objects.count(), 1)

    def test_approved_authorization_cannot_be_rebound_to_different_evidence(self):
        *_, adjudication = self.terminal_success()
        approved = self.approve(adjudication)
        authorization = ExceptionalRecognitionAuthorization.objects.get(adjudication=approved)
        authorization.amount += 1
        with self.assertRaises(ValidationError):
            authorization.save()


from cheatgame.digital_products.models import (  # noqa: E402
    DigitalCartFulfillmentMethod,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart  # noqa: E402
from cheatgame.digital_products.services.checkout_preparation import prepare_digital_checkout  # noqa: E402
from cheatgame.financial_core.models import (  # noqa: E402
    CommercialAccountingPolicyVersion,
    DigitalFulfillmentObligation,
    FinancialAccount,
    FinancialAccountType,
    MoneyUnit,
    PaymentTenderType,
    PaymentTransactionOperation,
    ProviderRequestOutcome,
)
from cheatgame.financial_core.services.late_payment_adjudication import (  # noqa: E402
    apply_approved_terminal_late_payment,
    recover_terminal_late_payment_inventory,
)
from cheatgame.financial_core.services.placement import place_order_and_create_payment_obligation  # noqa: E402
from cheatgame.financial_core.services.provider_requests import (  # noqa: E402
    apply_provider_request_result,
    claim_provider_request,
    create_or_replay_payment_attempt,
    create_or_replay_request_transaction,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture  # noqa: E402
from cheatgame.product.models import DeliveredVersion, NativeConsole, ProductCommerceAuthority  # noqa: E402
from cheatgame.shop.models import Cart  # noqa: E402


class TerminalLatePaymentInventoryRecoveryTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def reviewer(self, suffix, *, user_type=UserTypes.MANAGER):
        user = self.make_user()
        user.phone_number = f"09360000{int(suffix):03d}"
        user.user_type = user_type
        user.save(update_fields=("phone_number", "user_type", "updated_at"))
        return user

    def digital_terminal_success(self, *, pool_quantity=2):
        user = self.make_user()
        product = self.make_product(authority=ProductCommerceAuthority.DIGITAL_PRODUCTS, price=9000)
        version = DeliveredVersion.objects.create(product=product, native_console=NativeConsole.PS4)
        pool = InventoryPool.objects.create(
            sellable_quantity=pool_quantity,
            status=InventoryPoolStatus.ENABLED,
        )
        offer = DigitalOffer.objects.create(
            delivered_version=version,
            customer_console=NativeConsole.PS4,
            capacity=DigitalOfferCapacity.CAPACITY_1,
            price=9000,
            inventory_pool=pool,
            sale_state=DigitalOfferSaleState.ACTIVE,
        )
        cart = Cart.objects.create(user=user)
        add_digital_offer_to_cart(
            cart=cart,
            offer=offer,
            fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=user,
        )
        checkout, _ = prepare_digital_checkout(actor=user, client_checkout_uuid=uuid4())
        placement = place_order_and_create_payment_obligation(
            checkout_id=checkout.pk,
            expected_user_id=user.pk,
            expected_checkout_version=checkout.version,
            source_unit=MoneyUnit.IRR,
            idempotency_key=uuid4(),
        )
        _, _, account = self.make_account()
        attempt = create_or_replay_payment_attempt(
            payment_id=placement.payment.pk,
            merchant_account_version_id=account.pk,
            tender_type=PaymentTenderType.EXTERNAL_PROVIDER,
            requested_amount=placement.payment.amount_due,
            idempotency_key=uuid4(),
        ).attempt
        transaction_obj = create_or_replay_request_transaction(
            attempt_id=attempt.pk,
            operation_type=PaymentTransactionOperation.SALE,
            idempotency_key=uuid4(),
        ).transaction
        claim = claim_provider_request(
            transaction_id=transaction_obj.pk,
            claim_idempotency_key=uuid4(),
        )
        apply_provider_request_result(
            transaction_id=transaction_obj.pk,
            claim_token=claim.claim.claim_token,
            outcome=ProviderRequestOutcome.ACCEPTED_PENDING,
            evidence_hash="1" * 64,
            result_idempotency_key=uuid4(),
        )
        transaction_obj.refresh_from_db()
        transaction_obj.status = PaymentTransactionStatus.DECLINED
        transaction_obj.completed_at = timezone.now()
        transaction_obj.version += 1
        transaction_obj.save(update_fields=("status", "completed_at", "version", "updated_at"))
        attempt.status = PaymentAttemptStatus.DEFINITIVE_FAILED
        attempt.version += 1
        attempt.save(update_fields=("status", "version", "updated_at"))
        placement.payment.collection_status = PaymentCollectionStatus.OPEN
        placement.payment.version += 1
        placement.payment.save(update_fields=("collection_status", "version", "updated_at"))
        _, verification_claim = self.verification_claim(transaction_obj, account)
        verification = apply_verification_result(
            claim_token=verification_claim.claim.claim_token,
            result=self.normalized_result(transaction_obj, account),
            result_idempotency_key=uuid4(),
            trigger_source=VerificationTriggerSource.CALLBACK,
        )
        _, _, liability = self.accounting_policy(account)
        merchandise = FinancialAccount.objects.create(
            key=f"late-digital-revenue:{uuid4()}",
            name="Late digital revenue",
            account_type=FinancialAccountType.REVENUE,
        )
        shipping = FinancialAccount.objects.create(
            key=f"late-digital-shipping:{uuid4()}",
            name="Unused late digital shipping",
            account_type=FinancialAccountType.REVENUE,
        )
        CommercialAccountingPolicyVersion.objects.create(
            policy_key="late-commercial-digital-v1",
            version=1,
            commerce_authority="digital_products",
            customer_unapplied_funds_account=liability,
            merchandise_revenue_account=merchandise,
            shipping_revenue_account=shipping,
            active_for_new_finalizations=True,
        )
        adjudication = LatePaymentAdjudication.objects.get(verification=verification)
        maker = self.reviewer("11")
        checker = self.reviewer("12", user_type=UserTypes.ADMIN)
        proposal = propose_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            decision=LatePaymentAdjudicationDecision.ACCEPT,
            rationale="Exact settlement evidence accepted.",
            actor=maker,
            idempotency_key=uuid4(),
        )
        adjudication = check_terminal_late_payment_decision(
            adjudication_public_id=adjudication.public_id,
            approve_proposal=True,
            rationale="Independent approval.",
            actor=checker,
            expected_proposal_version=proposal.proposal_version,
            idempotency_key=uuid4(),
        )
        return placement, pool, adjudication, checker

    def apply(self, adjudication, checker, *, key=None):
        return apply_approved_terminal_late_payment(
            adjudication_public_id=adjudication.public_id,
            idempotency_key=key or uuid4(),
            correlation_id=uuid4(),
            actor=checker,
        )

    def _concurrent_recovery(self, *, barrier, authorization_id, results, result_lock):
        close_old_connections()
        try:
            barrier.wait()
            recovered = recover_terminal_late_payment_inventory(
                authorization_id=authorization_id
            )
            outcome = ("ok", recovered)
        except Exception as exc:  # pragma: no cover - asserted by the parent thread
            outcome = ("error", type(exc).__name__, str(exc))
        finally:
            close_old_connections()
        with result_lock:
            results.append(outcome)

    def test_existing_hold_is_retained_without_inventory_decrement(self):
        placement, pool, adjudication, checker = self.digital_terminal_success()
        before = pool.sellable_quantity
        result = self.apply(adjudication, checker)
        reservation = DigitalInventoryReservation.objects.get(order=placement.order)
        pool.refresh_from_db()
        self.assertTrue(result.inventory_recovered)
        self.assertEqual(reservation.state, DigitalInventoryReservationState.PAYMENT_HOLD)
        self.assertEqual(pool.sellable_quantity, before)
        self.assertEqual(result.inventory_review.status, "resolved")

    def test_released_hold_creates_one_linked_replacement_and_replays(self):
        placement, pool, adjudication, checker = self.digital_terminal_success()
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        key = uuid4()
        first = self.apply(adjudication, checker, key=key)
        second = self.apply(adjudication, checker, key=key)
        reservations = list(DigitalInventoryReservation.objects.filter(order=placement.order).order_by("pk"))
        pool.refresh_from_db()
        self.assertTrue(first.inventory_recovered)
        self.assertTrue(second.inventory_recovered)
        self.assertEqual(len(reservations), 2)
        self.assertEqual(reservations[0].state, DigitalInventoryReservationState.RELEASED)
        self.assertEqual(reservations[1].state, DigitalInventoryReservationState.PAYMENT_HOLD)
        self.assertEqual(
            reservations[1].recovery_authorization_id,
            adjudication.recognition_authorization.pk,
        )
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(pool.sellable_quantity, 2)
        self.finalize(placement)
        reservations[1].refresh_from_db()
        pool.refresh_from_db()
        self.assertEqual(reservations[1].state, DigitalInventoryReservationState.CONSUMED)
        self.assertEqual(pool.sellable_quantity, 1)
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 1)

    def test_concurrent_recovery_converges_on_one_replacement(self):
        placement, _, adjudication, checker = self.digital_terminal_success()
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        barrier = Barrier(2)
        result_lock = Lock()
        results = []
        threads = [
            Thread(
                target=self._concurrent_recovery,
                kwargs={
                    "barrier": barrier,
                    "authorization_id": adjudication.recognition_authorization.pk,
                    "results": results,
                    "result_lock": result_lock,
                },
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(results, [("ok", True), ("ok", True)])
        replacements = DigitalInventoryReservation.objects.filter(
            order=placement.order,
            recovery_authorization__isnull=False,
        )
        self.assertEqual(replacements.count(), 1)
        self.assertEqual(
            replacements.get().state,
            DigitalInventoryReservationState.PAYMENT_HOLD,
        )

    def test_customer_checkout_projection_accepts_replacement_history_before_and_after_consumption(self):
        placement, _, adjudication, checker = self.digital_terminal_success()
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        self.apply(adjudication, checker)

        client = APIClient()
        client.force_authenticate(placement.order.user)
        detail_url = f"/api/digital-products/customer/checkout/{placement.order.checkout.public_id}/"
        active_url = "/api/digital-products/customer/checkout/active/"
        detail = client.get(detail_url)
        active = client.get(active_url)
        self.assertEqual((detail.status_code, active.status_code), (200, 200))
        self.assertEqual(len(detail.data["lines"]), 1)
        self.assertNotIn("reservation", str(detail.data).lower())

        self.finalize(placement)
        consumed_detail = client.get(detail_url)
        self.assertEqual(consumed_detail.status_code, 200)
        self.assertEqual(consumed_detail.data["status"], "paid")
        self.assertEqual(len(consumed_detail.data["lines"]), 1)

    def test_current_reservation_constraint_rejects_every_live_state_beside_replacement(self):
        placement, _, adjudication, checker = self.digital_terminal_success()
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        self.apply(adjudication, checker)

        for conflicting_state in (
            DigitalInventoryReservationState.ACTIVE,
            DigitalInventoryReservationState.PAYMENT_HOLD,
            DigitalInventoryReservationState.HELD_FOR_REVIEW,
        ):
            with self.subTest(state=conflicting_state):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    DigitalInventoryReservation.objects.filter(pk=original.pk).update(
                        state=conflicting_state
                    )

    def test_no_current_reservation_remains_projectable_during_paid_unfulfillable_review(self):
        placement, pool, adjudication, checker = self.digital_terminal_success(pool_quantity=1)
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        pool.sellable_quantity = 0
        pool.save(update_fields=("sellable_quantity", "updated_at"))
        result = self.apply(adjudication, checker)
        self.assertFalse(result.inventory_recovered)

        client = APIClient()
        client.force_authenticate(placement.order.user)
        response = client.get(
            f"/api/digital-products/customer/checkout/{placement.order.checkout.public_id}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["is_payment_ready"])
        self.assertEqual(len(response.data["lines"]), 1)

    def test_unavailable_inventory_keeps_recognized_liability_in_review(self):
        placement, pool, adjudication, checker = self.digital_terminal_success(pool_quantity=1)
        original = DigitalInventoryReservation.objects.get(order=placement.order)
        original.state = DigitalInventoryReservationState.RELEASED
        original.state_changed_at = timezone.now()
        original.resolution_reason = "definitive_unpaid"
        original.save(update_fields=("state", "state_changed_at", "resolution_reason", "updated_at"))
        pool.sellable_quantity = 0
        pool.save(update_fields=("sellable_quantity", "updated_at"))
        result = self.apply(adjudication, checker)
        placement.payment.refresh_from_db()
        self.assertFalse(result.inventory_recovered)
        self.assertEqual(result.inventory_review.status, "open")
        self.assertEqual(
            placement.payment.collection_status,
            PaymentCollectionStatus.PAID_PENDING_FINALIZATION,
        )
        self.assertEqual(FinancialAllocation.objects.count(), 1)
        self.assertEqual(DigitalInventoryReservation.objects.filter(order=placement.order).count(), 1)
