from threading import Barrier, Thread
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalCheckoutLineSnapshot,
    DigitalFulfillmentItem,
    Entitlement,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    CommercialFinalization,
    CommercialFinalizationWorkItem,
    DigitalInventoryCommitment,
    FinancialActorType,
    FinancialOutboxMessage,
    FinalizationWorkStatus,
    JournalEntry,
    ReviewAction,
    ReviewCase,
    ReviewCaseReason,
    ReviewCaseStatus,
    StandardInventoryCommitment,
)
from cheatgame.financial_core.services.commercial_finalization import (
    CommercialFinalizationBlocked,
    FINALIZER_CONTRACT_VERSION,
    FULFILLMENT_OUTBOX_TOPIC,
    finalize_commercial_work_item,
    finalize_paid_commerce,
)
from cheatgame.financial_core.services.idempotency import IdempotencyConflict
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.shop.models import CartState, CheckoutStatus, OrderStatus, StockReservationState


class CommercialFinalizerApi08Tests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def run_work(self, placement, *, key=None, work_version=None, payment_version=None, **kwargs):
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        return finalize_commercial_work_item(
            work_item_public_id=work.public_id,
            idempotency_key=key or uuid4(),
            expected_work_item_version=work.version if work_version is None else work_version,
            expected_payment_version=(
                placement.payment.version if payment_version is None else payment_version
            ),
            correlation_id=uuid4(),
            **kwargs,
        )

    def test_work_root_creates_complete_standard_graph_and_resolves_only_system_marker(self):
        placement, _ = self.ready_standard()
        unrelated = ReviewCase.objects.create(
            reason=ReviewCaseReason.FRAUD_RISK,
            severity="low",
            status=ReviewCaseStatus.RESOLVED,
            payment=placement.payment,
            opened_by_type=FinancialActorType.SYSTEM,
            summary="Already resolved unrelated evidence.",
            resolution_code="historical",
            resolved_at=placement.payment.updated_at,
            idempotency_key=uuid4(),
        )
        result = self.run_work(placement)
        work = result.work_item
        placement.payment.refresh_from_db()
        placement.order.refresh_from_db()
        placement.order.checkout.refresh_from_db()
        placement.order.checkout.cart.refresh_from_db()
        reservation = placement.order.stock_reservations.get()
        marker = ReviewCase.objects.get(
            payment=placement.payment,
            reason=ReviewCaseReason.PAID_PENDING_FINALIZATION,
        )
        self.assertEqual(work.status, FinalizationWorkStatus.COMPLETED)
        self.assertEqual(placement.payment.collection_status, "paid")
        self.assertEqual(placement.order.payment_status, OrderStatus.PAID)
        self.assertEqual(placement.order.checkout.status, CheckoutStatus.PAID)
        self.assertEqual(placement.order.checkout.cart.state, CartState.OPEN)
        self.assertEqual(reservation.state, StockReservationState.CONSUMED)
        self.assertEqual(StandardInventoryCommitment.objects.count(), 1)
        self.assertEqual(marker.status, ReviewCaseStatus.RESOLVED)
        self.assertTrue(
            ReviewAction.objects.filter(
                review_case=marker,
                actor_type=FinancialActorType.SYSTEM,
                actor__isnull=True,
                reason_code="commercial_finalization_completed",
            ).exists()
        )
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.resolution_code, "historical")

    def test_digital_work_creates_commitment_obligation_and_dormant_handoff_only(self):
        placement, pool = self.ready_digital()
        result = self.run_work(placement)
        pool.refresh_from_db()
        commitment = DigitalInventoryCommitment.objects.get(finalization=result.finalization)
        outbox = FinancialOutboxMessage.objects.get(
            topic=FULFILLMENT_OUTBOX_TOPIC,
            aggregate_id=str(result.finalization.public_id),
        )
        self.assertEqual(commitment.inventory_pool_id, pool.pk)
        self.assertEqual(commitment.pre_quantity - commitment.committed_quantity, commitment.post_quantity)
        self.assertEqual(outbox.safe_payload["finalizer_contract_version"], FINALIZER_CONTRACT_VERSION)
        self.assertEqual(outbox.safe_payload["commerce_authority"], "digital_products")
        self.assertFalse(DigitalFulfillmentItem.objects.exists())
        self.assertFalse(Entitlement.objects.exists())

    def test_completed_response_loss_replays_without_duplicate_effect(self):
        placement, _ = self.ready_standard()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        placement.payment.refresh_from_db()
        payment_version = placement.payment.version
        key = uuid4()
        first = self.run_work(
            placement,
            key=key,
            work_version=work.version,
            payment_version=payment_version,
        )
        second = self.run_work(
            placement,
            key=key,
            work_version=work.version,
            payment_version=payment_version,
        )
        third = self.run_work(
            placement,
            key=uuid4(),
            work_version=work.version,
            payment_version=payment_version,
        )
        self.assertTrue(second.replayed)
        self.assertTrue(third.replayed)
        self.assertEqual(first.finalization.pk, second.finalization.pk)
        self.assertEqual(first.finalization.pk, third.finalization.pk)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(StandardInventoryCommitment.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="commercial_reclassification").count(), 1)
        self.assertEqual(FinancialOutboxMessage.objects.filter(topic=FULFILLMENT_OUTBOX_TOPIC).count(), 1)

    def test_actor_and_work_version_fail_closed(self):
        placement, _ = self.ready_standard()
        with self.assertRaises(CommercialFinalizationBlocked):
            self.run_work(placement, actor_type=FinancialActorType.CUSTOMER)
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        with self.assertRaises(CommercialFinalizationBlocked):
            self.run_work(placement, work_version=work.version + 1)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_journal_failure_rolls_back_full_graph_and_releases_claim_for_retry(self):
        placement, _ = self.ready_standard()
        product = placement.order.order_items.get().product
        before = product.quantity
        with patch(
            "cheatgame.financial_core.services.commercial_finalization.post_balanced_journal_entry_under_lock",
            side_effect=ValidationError("synthetic accounting failure"),
        ):
            with self.assertRaises(ValidationError):
                self.run_work(placement)
        placement.payment.refresh_from_db()
        placement.order.refresh_from_db()
        product.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        reservation = placement.order.stock_reservations.get()
        self.assertEqual(work.status, FinalizationWorkStatus.PENDING)
        self.assertIsNone(work.claim_token)
        self.assertEqual(placement.payment.collection_status, "paid_pending_finalization")
        self.assertEqual(placement.order.payment_status, OrderStatus.PENDDING)
        self.assertEqual(product.quantity, before)
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)
        self.assertFalse(CommercialFinalization.objects.exists())
        self.assertFalse(StandardInventoryCommitment.objects.exists())
        self.assertFalse(FinancialOutboxMessage.objects.filter(topic=FULFILLMENT_OUTBOX_TOPIC).exists())

    def test_expired_claim_is_reclaimed_without_stale_owner_completion(self):
        placement, _ = self.ready_standard()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        now = timezone.now()
        work.status = FinalizationWorkStatus.CLAIMED
        work.claim_token = uuid4()
        work.claimed_at = now - timedelta(minutes=10)
        work.claim_expires_at = now - timedelta(minutes=5)
        work.attempt_count = 1
        work.version += 1
        work.save(
            update_fields=(
                "status",
                "claim_token",
                "claimed_at",
                "claim_expires_at",
                "attempt_count",
                "version",
                "updated_at",
            )
        )
        placement.payment.refresh_from_db()
        result = self.run_work(
            placement,
            work_version=work.version,
            payment_version=placement.payment.version,
        )
        result.work_item.refresh_from_db()
        self.assertEqual(result.work_item.status, FinalizationWorkStatus.COMPLETED)
        self.assertEqual(result.work_item.attempt_count, 2)
        self.assertIsNone(result.work_item.claim_token)

    def test_expired_reservation_fails_terminal_without_commercial_effect(self):
        placement, _ = self.ready_standard()
        reservation = placement.order.stock_reservations.get()
        reservation.expires_at = timezone.now() - timedelta(microseconds=1)
        reservation.save(update_fields=("expires_at", "updated_at"))
        with self.assertRaises(CommercialFinalizationBlocked):
            self.run_work(placement)
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        placement.payment.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(work.status, FinalizationWorkStatus.CANCELED)
        self.assertEqual(placement.payment.collection_status, "paid_pending_finalization")
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_unresolved_financial_review_rolls_back_and_blocks_finalization(self):
        placement, _ = self.ready_standard()
        ReviewCase.objects.create(
            reason=ReviewCaseReason.FRAUD_RISK,
            severity="high",
            payment=placement.payment,
            opened_by_type=FinancialActorType.SYSTEM,
            summary="Synthetic unresolved financial review.",
            idempotency_key=uuid4(),
        )
        product = placement.order.order_items.get().product
        before = product.quantity
        with self.assertRaises(CommercialFinalizationBlocked):
            self.run_work(placement)
        placement.payment.refresh_from_db()
        product.refresh_from_db()
        self.assertEqual(placement.payment.collection_status, "paid_pending_finalization")
        self.assertEqual(product.quantity, before)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_same_key_changed_frozen_identity_conflicts(self):
        placement, _ = self.ready_standard()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        placement.payment.refresh_from_db()
        payment_version = placement.payment.version
        key = uuid4()
        self.run_work(
            placement,
            key=key,
            work_version=work.version,
            payment_version=payment_version,
        )
        with self.assertRaises(IdempotencyConflict):
            self.run_work(
                placement,
                key=key,
                work_version=work.version,
                payment_version=payment_version + 1,
            )

    def test_postgresql_append_only_guards_protect_new_evidence(self):
        placement, _ = self.ready_standard()
        finalization = self.run_work(placement).finalization
        commitment = StandardInventoryCommitment.objects.get(finalization=finalization)
        outbox = FinancialOutboxMessage.objects.get(topic=FULFILLMENT_OUTBOX_TOPIC)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE financial_core_standardinventorycommitment "
                    "SET committed_quantity = committed_quantity + 1 WHERE id = %s",
                    [commitment.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM financial_core_financialoutboxmessage WHERE id = %s",
                    [outbox.pk],
                )

    def test_postgresql_deferred_graph_guard_rejects_foreign_authority_commitment(self):
        placement, _ = self.ready_standard()
        finalization = self.run_work(placement).finalization
        foreign_pool = InventoryPool.objects.create(
            sellable_quantity=2,
            status=InventoryPoolStatus.ENABLED,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO financial_core_digitalinventorycommitment "
                    "(public_id, reservation_set_digest, pre_quantity, committed_quantity, "
                    "post_quantity, correlation_id, causation_id, created_at, finalization_id, "
                    "inventory_pool_id, order_id) "
                    "VALUES (%s, %s, 2, 1, 1, %s, NULL, NOW(), %s, %s, %s)",
                    [
                        str(uuid4()),
                        "f" * 64,
                        str(uuid4()),
                        finalization.pk,
                        foreign_pool.pk,
                        placement.order.pk,
                    ],
                )

    def test_postgresql_deferred_guard_rejects_forged_reservation_consumption(self):
        placement, _ = self.ready_standard()
        reservation = placement.order.stock_reservations.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE shop_stockreservation SET state = 'consumed' WHERE id = %s",
                    [reservation.pk],
                )
        reservation.refresh_from_db()
        self.assertEqual(reservation.state, StockReservationState.PAYMENT_HOLD)

    def test_realistic_work_query_budget_is_bounded(self):
        placement, _ = self.ready_standard()
        with CaptureQueriesContext(connection) as queries:
            self.run_work(placement)
        self.assertLessEqual(len(queries), 160)


class CommercialFinalizerApi08ConcurrencyTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def test_two_work_commands_with_different_keys_commit_one_complete_graph(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        work_version = work.version
        payment_version = placement.payment.version
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = finalize_commercial_work_item(
                    work_item_public_id=work.public_id,
                    idempotency_key=uuid4(),
                    expected_work_item_version=work_version,
                    expected_payment_version=payment_version,
                    correlation_id=uuid4(),
                )
                outcomes.append(("ok", result.finalization.pk, result.replayed))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__, False))
            finally:
                close_old_connections()

        threads = [Thread(target=runner) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual([item[0] for item in outcomes].count("ok"), 2, outcomes)
        self.assertEqual(CommercialFinalization.objects.count(), 1)
        self.assertEqual(StandardInventoryCommitment.objects.count(), 1)
        self.assertEqual(JournalEntry.objects.filter(source_type="commercial_reclassification").count(), 1)
        self.assertEqual(FinancialOutboxMessage.objects.filter(topic=FULFILLMENT_OUTBOX_TOPIC).count(), 1)


class CommercialFinalizerApi08BlockerHardeningTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def run_work(self, placement, *, key=None, work_version=None, payment_version=None, **kwargs):
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        return finalize_commercial_work_item(
            work_item_public_id=work.public_id,
            idempotency_key=key or uuid4(),
            expected_work_item_version=work.version if work_version is None else work_version,
            expected_payment_version=(
                placement.payment.version if payment_version is None else payment_version
            ),
            correlation_id=uuid4(),
            **kwargs,
        )

    def test_direct_engine_rejects_missing_claimed_work_root(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        with self.assertRaises(CommercialFinalizationBlocked):
            finalize_paid_commerce(
                payment_id=placement.payment.pk,
                idempotency_key=uuid4(),
                expected_payment_version=placement.payment.version,
                correlation_id=uuid4(),
            )
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        self.assertEqual(work.status, FinalizationWorkStatus.PENDING)
        self.assertFalse(CommercialFinalization.objects.exists())

    def test_completed_replay_uses_frozen_policy_after_policy_rotation(self):
        placement, _ = self.ready_standard()
        placement.payment.refresh_from_db()
        work = CommercialFinalizationWorkItem.objects.get(payment=placement.payment)
        work_version = work.version
        payment_version = placement.payment.version
        first = self.run_work(
            placement,
            work_version=work_version,
            payment_version=payment_version,
        )
        frozen = first.finalization.accounting_policy_version
        frozen.active_for_new_finalizations = False
        frozen.save(update_fields=("active_for_new_finalizations", "updated_at"))
        CommercialAccountingPolicyVersion.objects.create(
            policy_key=f"{frozen.policy_key}-rotated",
            version=frozen.version + 1,
            commerce_authority=frozen.commerce_authority,
            customer_unapplied_funds_account=frozen.customer_unapplied_funds_account,
            merchandise_revenue_account=frozen.merchandise_revenue_account,
            shipping_revenue_account=frozen.shipping_revenue_account,
            active_for_new_finalizations=True,
        )
        replay = self.run_work(
            placement,
            key=uuid4(),
            work_version=work_version,
            payment_version=payment_version,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.finalization.pk, first.finalization.pk)
        self.assertEqual(replay.finalization.accounting_policy_version_id, frozen.pk)

    def test_digital_commercial_snapshot_and_revision_are_database_immutable(self):
        placement, _ = self.ready_digital()
        checkout = placement.order.checkout
        snapshot = DigitalCheckoutLineSnapshot.objects.get(checkout_line__checkout=checkout)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_digitalcheckoutlinesnapshot "
                    "SET capacity = 'capacity_1' WHERE id = %s",
                    [snapshot.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE shop_checkoutline SET snapshot = "
                    "jsonb_set(snapshot, '{commercial_revision}', '2'::jsonb) WHERE id = %s",
                    [snapshot.checkout_line_id],
                )

    def test_terminal_projections_and_inventory_deltas_require_exact_finalization(self):
        placement, _ = self.ready_standard()
        checkout = placement.order.checkout
        cart = checkout.cart
        product = placement.order.order_items.get().product
        for sql, params in (
            ("UPDATE shop_order SET payment_status = 3, fulfillment_status = 'processing' WHERE id = %s", [placement.order.pk]),
            ("UPDATE shop_checkout SET status = 'paid' WHERE id = %s", [checkout.pk]),
            ("UPDATE shop_cart SET state = 'open', active_checkout_id = NULL WHERE id = %s", [cart.pk]),
            ("UPDATE product_product SET quantity = quantity - 1 WHERE id = %s", [product.pk]),
        ):
            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)

        standard = self.run_work(placement).finalization
        commitment = StandardInventoryCommitment.objects.get(finalization=standard)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE product_product SET quantity = quantity - 1 WHERE id = %s",
                    [commitment.product_id],
                )

        digital_placement, pool = self.ready_digital()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_inventorypool "
                    "SET sellable_quantity = sellable_quantity - 1 WHERE id = %s",
                    [pool.pk],
                )
        self.run_work(digital_placement)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_inventorypool "
                    "SET sellable_quantity = sellable_quantity - 1 WHERE id = %s",
                    [pool.pk],
                )

    def test_commitment_cannot_replace_the_observed_inventory_decrement(self):
        placement, _ = self.ready_standard()
        product = placement.order.order_items.get().product
        original_quantity = product.quantity
        with patch.object(type(product), "save", autospec=True, return_value=None):
            with self.assertRaises(DatabaseError):
                self.run_work(placement)
        product.refresh_from_db()
        self.assertEqual(product.quantity, original_quantity)
        self.assertFalse(CommercialFinalization.objects.exists())
        self.assertFalse(StandardInventoryCommitment.objects.exists())

        digital_placement, pool = self.ready_digital()
        original_quantity = pool.sellable_quantity
        with patch.object(type(pool), "save", autospec=True, return_value=None):
            with self.assertRaises(DatabaseError):
                self.run_work(digital_placement)
        pool.refresh_from_db()
        self.assertEqual(pool.sellable_quantity, original_quantity)
        self.assertFalse(
            CommercialFinalization.objects.filter(payment=digital_placement.payment).exists()
        )
        self.assertFalse(DigitalInventoryCommitment.objects.exists())

    def test_extra_commercial_postings_and_forged_outbox_are_rejected(self):
        placement, _ = self.ready_standard()
        finalization = self.run_work(placement).finalization
        policy = finalization.accounting_policy_version
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO financial_core_journalposting "
                    "(line_number, direction, amount, currency, memo, created_at, account_id, entry_id) "
                    "VALUES (90, 'debit', 1, 'IRR', '', NOW(), %s, %s), "
                    "(91, 'credit', 1, 'IRR', '', NOW(), %s, %s)",
                    [
                        policy.merchandise_revenue_account_id,
                        finalization.journal_entry_id,
                        policy.shipping_revenue_account_id,
                        finalization.journal_entry_id,
                    ],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            FinancialOutboxMessage.objects.create(
                topic=FULFILLMENT_OUTBOX_TOPIC,
                aggregate_type=finalization._meta.label_lower,
                aggregate_id=str(finalization.public_id),
                idempotency_key=f"forged:{uuid4()}",
                correlation_id=uuid4(),
                safe_payload={"event_type": FULFILLMENT_OUTBOX_TOPIC, "credential": "forged"},
            )
