from threading import Barrier, Thread
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cheatgame.digital_products.fulfillment_serializers import (
    admin_fulfillment_projection,
    customer_fulfillment_projection,
)
from cheatgame.digital_products.models import (
    DigitalEntitlementStatus,
    DigitalFulfillmentItem,
    DigitalFulfillmentStatus,
    DigitalCartFulfillmentMethod,
    DigitalOffer,
    DigitalOfferCapacity,
    DigitalOfferSaleState,
    Entitlement,
    FulfillmentActivity,
    FulfillmentActivityActorType,
    FulfillmentActorAuthority,
    FulfillmentActivityType,
    InstalledGameClassification,
    InstalledGameCompletionSource,
    InstalledGameRecord,
    InstalledGameRecordState,
    InventoryPool,
    InventoryPoolStatus,
)
from cheatgame.digital_products.services.cart import add_digital_offer_to_cart
from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentConflict,
    DigitalFulfillmentValidationError,
    add_fulfillment_note,
    assign_fulfillment_operator,
    change_fulfillment_method,
    correct_purchased_game_installation,
    customer_confirm_remote_completion,
    open_fulfillment_exception,
    record_bonus_game,
    record_console_received,
    record_customer_contact,
    record_purchased_game_installation,
    record_remote_handling,
    remove_purchased_game_installation,
    provision_digital_fulfillment_obligation,
    retry_fulfillment,
    staff_verify_fulfillment_completion,
    start_fulfillment_work,
)
from cheatgame.financial_core.models import DigitalFulfillmentObligation, IdempotencyRecord
from cheatgame.financial_core.test_commercial_finalizer_phase1 import CommercialFinalizerFixture
from cheatgame.shop.models import FulfillmentStatus
from cheatgame.users.models import BaseUser, UserTypes


class DigitalFulfillmentObligationIntegrationTests(CommercialFinalizerFixture, TransactionTestCase):
    reset_sequences = True

    def finalized_obligation(self):
        placement, pool = self.ready_digital()
        self.finalize(placement)
        return placement, pool, DigitalFulfillmentObligation.objects.get(order=placement.order)

    def manager(self, suffix="1", user_type=UserTypes.MANAGER):
        return BaseUser.objects.create_user(
            phone_number=f"0912999900{suffix}", firstname="Synthetic", lastname="Operator",
            user_type=user_type,
        )

    def provision(self):
        placement, pool, obligation = self.finalized_obligation()
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
        )
        return placement, pool, obligation, item

    def provision_remote(self):
        def add_remote_line(**kwargs):
            remote_pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
            remote_offer = DigitalOffer.objects.create(
                delivered_version=kwargs["offer"].delivered_version,
                customer_console=kwargs["offer"].customer_console,
                capacity=DigitalOfferCapacity.CAPACITY_2,
                price=kwargs["offer"].price,
                inventory_pool=remote_pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
            return add_digital_offer_to_cart(
                cart=kwargs["cart"], offer=remote_offer,
                fulfillment_method=DigitalCartFulfillmentMethod.REMOTE, actor=kwargs["actor"],
            )

        with patch(
            "cheatgame.financial_core.test_commercial_finalizer_phase1.add_digital_offer_to_cart",
            side_effect=add_remote_line,
        ):
            placement, pool = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(order=placement.order)
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
        )
        return placement, pool, obligation, item

    def test_intake_creates_exactly_one_pending_graph_and_replays(self):
        placement, pool, obligation = self.finalized_obligation()
        key = uuid4()
        first = provision_digital_fulfillment_obligation(obligation_public_id=obligation.public_id, idempotency_key=key)
        second = provision_digital_fulfillment_obligation(obligation_public_id=obligation.public_id, idempotency_key=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(DigitalFulfillmentItem.objects.filter(obligation=obligation).count(), 1)
        self.assertEqual(Entitlement.objects.filter(obligation=obligation, status=DigitalEntitlementStatus.PENDING_FULFILLMENT).count(), 1)
        self.assertEqual(FulfillmentActivity.objects.filter(fulfillment_item=first, activity_type=FulfillmentActivityType.PROVISIONED).count(), 1)
        pool.refresh_from_db()
        self.assertEqual(pool.sellable_quantity, 1)  # finalizer consumed it; intake did not.

    def test_conflicting_intake_key_is_rejected(self):
        _, _, obligation = self.finalized_obligation()
        key = uuid4()
        IdempotencyRecord.objects.create(
            scope="digital_fulfillment.provision", key=str(key), request_hash="f" * 64,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            provision_digital_fulfillment_obligation(obligation_public_id=obligation.public_id, idempotency_key=key)

    def test_payment_and_obligation_do_not_activate_entitlement(self):
        _, _, _, item = self.provision()
        self.assertEqual(item.entitlement.status, DigitalEntitlementStatus.PENDING_FULFILLMENT)
        self.assertNotEqual(item.status, DigitalFulfillmentStatus.COMPLETED)

    def test_in_store_completion_activates_once_and_preserves_commercial_truth(self):
        placement, pool, obligation, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        with self.assertRaises(DigitalFulfillmentConflict):
            record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        completion_key = uuid4()
        staff_verify_fulfillment_completion(fulfillment_id=item.public_id, actor=operator, idempotency_key=completion_key)
        staff_verify_fulfillment_completion(fulfillment_id=item.public_id, actor=operator, idempotency_key=completion_key)
        item.refresh_from_db(); item.entitlement.refresh_from_db(); placement.order.refresh_from_db(); pool.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.COMPLETED)
        self.assertEqual(item.entitlement.status, DigitalEntitlementStatus.ACTIVE)
        self.assertEqual(placement.order.fulfillment_status, FulfillmentStatus.DELIVERED)
        self.assertEqual(pool.sellable_quantity, 1)
        self.assertEqual(InstalledGameRecord.objects.filter(fulfillment_item=item, classification=InstalledGameClassification.PURCHASED).count(), 1)

    def test_purchased_identity_must_match_snapshot(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        other_product = self.make_product()
        with self.assertRaises(ValidationError):
            InstalledGameRecord.objects.create(
                fulfillment_item=item, game=other_product,
                delivered_version=item.obligation.checkout_line.digital_snapshot.delivered_version,
                classification=InstalledGameClassification.PURCHASED,
                completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
                operator=operator, actor_authority=FulfillmentActorAuthority.UNASSIGNED_STAFF,
                idempotency_key=uuid4(), request_fingerprint="f" * 64,
            )

    def test_bonus_has_no_commercial_or_entitlement_effect(self):
        _, _, obligation, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        before = Entitlement.objects.count()
        bonus = record_bonus_game(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(), fallback_title="Synthetic Bonus")
        self.assertEqual(bonus.classification, InstalledGameClassification.BONUS)
        self.assertEqual(Entitlement.objects.count(), before)
        self.assertEqual(DigitalFulfillmentObligation.objects.count(), 1)

    def test_customer_and_internal_projections_exclude_financial_internals(self):
        _, _, _, item = self.provision()
        admin = admin_fulfillment_projection(item)
        customer = customer_fulfillment_projection(item)
        forbidden = ("journal", "allocation", "provider", "reservation", "pool", "fingerprint")
        rendered = f"{admin!r}{customer!r}".lower()
        self.assertFalse(any(fragment in rendered for fragment in forbidden))

    def test_unassigned_operator_cannot_bypass_assignment(self):
        _, _, _, item = self.provision()
        assigned = self.manager("1")
        other = self.manager("2")
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=assigned, actor=assigned, idempotency_key=uuid4())
        with self.assertRaises(PermissionDenied):
            record_customer_contact(fulfillment_id=item.public_id, actor=other, idempotency_key=uuid4())

    def test_credential_like_notes_are_rejected(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        with self.assertRaises(DigitalFulfillmentValidationError):
            add_fulfillment_note(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
                note="customer password is synthetic",
            )

    def test_postgresql_raw_sql_cannot_reassign_ownership_or_mutate_activity(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, obligation, item = self.provision()
        activity = item.activities.get(activity_type=FulfillmentActivityType.PROVISIONED)
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_digitalfulfillmentitem SET obligation_id = %s WHERE id = %s",
                    [obligation.pk + 999999, item.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_fulfillmentactivity SET note = 'forged' WHERE id = %s",
                    [activity.pk],
                )

    def test_multi_game_order_provisions_and_completes_each_obligation_independently(self):
        def add_two_lines(**kwargs):
            first = add_digital_offer_to_cart(**kwargs)
            second_pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
            second_offer = DigitalOffer.objects.create(
                delivered_version=kwargs["offer"].delivered_version,
                customer_console=kwargs["offer"].customer_console,
                capacity=DigitalOfferCapacity.CAPACITY_2,
                price=kwargs["offer"].price + 1000,
                inventory_pool=second_pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
            add_digital_offer_to_cart(
                cart=kwargs["cart"], offer=second_offer,
                fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE, actor=kwargs["actor"],
            )
            return first

        with patch(
            "cheatgame.financial_core.test_commercial_finalizer_phase1.add_digital_offer_to_cart",
            side_effect=add_two_lines,
        ):
            placement, _ = self.ready_digital()
        self.finalize(placement)
        obligations = list(DigitalFulfillmentObligation.objects.filter(order=placement.order).order_by("pk"))
        self.assertEqual(len(obligations), 2)
        items = [provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
        ) for obligation in obligations]
        self.assertEqual({item.obligation_id for item in items}, {obligation.pk for obligation in obligations})
        operator = self.manager()

        def complete(item):
            assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
            record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
            record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
            start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
            record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
            staff_verify_fulfillment_completion(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())

        complete(items[0])
        placement.order.refresh_from_db(); items[1].refresh_from_db()
        self.assertEqual(placement.order.fulfillment_status, FulfillmentStatus.PROCESSING)
        self.assertEqual(items[1].status, DigitalFulfillmentStatus.QUEUED)
        self.assertEqual(items[1].entitlement.status, DigitalEntitlementStatus.PENDING_FULFILLMENT)
        complete(items[1])
        placement.order.refresh_from_db()
        self.assertEqual(placement.order.fulfillment_status, FulfillmentStatus.DELIVERED)

    def test_contradictory_partial_intake_is_rejected(self):
        _, _, obligation = self.finalized_obligation()
        with self.assertRaises(DigitalFulfillmentConflict), transaction.atomic():
            DigitalFulfillmentItem.objects.create(
                obligation=obligation,
                current_fulfillment_method=obligation.fulfillment_method,
                status=DigitalFulfillmentStatus.QUEUED,
            )
            provision_digital_fulfillment_obligation(
                obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
            )

    def test_remote_customer_confirmation_is_owner_bound_and_activates(self):
        def add_remote_line(**kwargs):
            remote_pool = InventoryPool.objects.create(sellable_quantity=2, status=InventoryPoolStatus.ENABLED)
            remote_offer = DigitalOffer.objects.create(
                delivered_version=kwargs["offer"].delivered_version,
                customer_console=kwargs["offer"].customer_console,
                capacity=DigitalOfferCapacity.CAPACITY_2,
                price=kwargs["offer"].price,
                inventory_pool=remote_pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
            return add_digital_offer_to_cart(
                cart=kwargs["cart"], offer=remote_offer,
                fulfillment_method=DigitalCartFulfillmentMethod.REMOTE, actor=kwargs["actor"],
            )

        with patch(
            "cheatgame.financial_core.test_commercial_finalizer_phase1.add_digital_offer_to_cart",
            side_effect=add_remote_line,
        ):
            placement, _ = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(order=placement.order)
        item = provision_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
        )
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        from cheatgame.digital_products.services.fulfillment import record_remote_handling
        record_remote_handling(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        stranger = BaseUser.objects.create_user(
            phone_number="09129999009", firstname="Other", lastname="Customer", user_type=UserTypes.CUSTOMER,
        )
        with self.assertRaises(DigitalFulfillmentValidationError):
            customer_confirm_remote_completion(fulfillment_id=item.public_id, actor=stranger, idempotency_key=uuid4())
        customer_confirm_remote_completion(
            fulfillment_id=item.public_id, actor=placement.order.user, idempotency_key=uuid4(),
        )
        item.refresh_from_db(); item.entitlement.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.COMPLETED)
        self.assertEqual(item.entitlement.status, DigitalEntitlementStatus.ACTIVE)

    def test_completion_failure_rolls_back_execution_and_entitlement(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        with patch("cheatgame.digital_products.services.fulfillment._append_activity", side_effect=RuntimeError("synthetic rollback")):
            with self.assertRaises(RuntimeError):
                staff_verify_fulfillment_completion(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        item.refresh_from_db(); item.entitlement.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.IN_PROGRESS)
        self.assertEqual(item.entitlement.status, DigitalEntitlementStatus.PENDING_FULFILLMENT)

    def test_concurrent_completion_has_one_activation(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock proof")
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        key = uuid4(); barrier = Barrier(2); outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                result = staff_verify_fulfillment_completion(
                    fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
                )
                outcomes.append(("ok", result.pk))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()

        threads = [Thread(target=runner), Thread(target=runner)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({kind for kind, _ in outcomes}, {"ok"})
        item.refresh_from_db(); item.entitlement.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.COMPLETED)
        self.assertEqual(item.entitlement.status, DigitalEntitlementStatus.ACTIVE)
        self.assertEqual(FulfillmentActivity.objects.filter(
            fulfillment_item=item, activity_type=FulfillmentActivityType.STAFF_VERIFIED,
        ).count(), 1)

    def test_concurrent_intake_produces_one_result(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock proof")
        _, _, obligation = self.finalized_obligation()
        barrier = Barrier(2)
        outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                item = provision_digital_fulfillment_obligation(obligation_public_id=obligation.public_id, idempotency_key=uuid4())
                outcomes.append(("ok", item.pk))
            except Exception as exc:  # captured for deterministic assertion
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()

        threads = [Thread(target=runner), Thread(target=runner)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({outcome[0] for outcome in outcomes}, {"ok"})
        self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
        self.assertEqual(DigitalFulfillmentItem.objects.filter(obligation=obligation).count(), 1)

    def test_transition_graph_rejects_backward_and_bypass_commands(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        with self.assertRaises(DigitalFulfillmentValidationError):
            record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        with self.assertRaises(DigitalFulfillmentValidationError):
            record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        open_fulfillment_exception(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(), note="Synthetic failure",
        )
        with self.assertRaises(DigitalFulfillmentValidationError):
            record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        with self.assertRaises(DigitalFulfillmentValidationError):
            start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        retry_fulfillment(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(), reason="Synthetic retry",
        )
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())

    def test_capacity_one_method_crossing_is_rejected(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        with self.assertRaises(DigitalFulfillmentValidationError):
            change_fulfillment_method(
                fulfillment_id=item.public_id, fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
                actor=operator, idempotency_key=uuid4(),
            )

    def test_command_replays_and_conflicts_are_fingerprinted(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        other = self.manager("2")
        assign_key = uuid4()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=assign_key,
        )
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=assign_key,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            assign_fulfillment_operator(
                fulfillment_id=item.public_id, operator=other, actor=operator, idempotency_key=assign_key,
            )
        contact_key = uuid4()
        record_customer_contact(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=contact_key, contacted=True,
        )
        record_customer_contact(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=contact_key, contacted=True,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            record_customer_contact(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=contact_key, contacted=False,
            )
        console_key = uuid4()
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=console_key)
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=console_key)
        work_key = uuid4()
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=work_key)
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=work_key)
        evidence_key = uuid4()
        evidence = record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
        )
        self.assertEqual(
            record_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
            ).pk,
            evidence.pk,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            record_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
                completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
            )
        note_key = uuid4()
        add_fulfillment_note(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=note_key, note="Synthetic note",
        )
        add_fulfillment_note(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=note_key, note="Synthetic note",
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            add_fulfillment_note(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=note_key, note="Different note",
            )
        completion_key = uuid4()
        staff_verify_fulfillment_completion(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=completion_key,
        )
        staff_verify_fulfillment_completion(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=completion_key,
        )
        self.assertEqual(
            record_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
            ).pk,
            evidence.pk,
        )
        bonus_key = uuid4()
        bonus = record_bonus_game(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=bonus_key,
            fallback_title="Synthetic Bonus",
        )
        self.assertEqual(record_bonus_game(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=bonus_key,
            fallback_title="Synthetic Bonus",
        ).pk, bonus.pk)
        with self.assertRaises(DigitalFulfillmentConflict):
            record_bonus_game(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=bonus_key,
                fallback_title="Different Bonus",
            )

    def test_remote_replays_survive_state_advancement(self):
        placement, _, _, item = self.provision_remote()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        evidence_key = uuid4()
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        remote_key = uuid4()
        record_remote_handling(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=remote_key,
        )
        record_remote_handling(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=remote_key,
        )
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=evidence_key,
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        confirm_key = uuid4()
        customer_confirm_remote_completion(
            fulfillment_id=item.public_id, actor=placement.order.user, idempotency_key=confirm_key,
        )
        customer_confirm_remote_completion(
            fulfillment_id=item.public_id, actor=placement.order.user, idempotency_key=confirm_key,
        )
        item.refresh_from_db()
        self.assertEqual(item.status, DigitalFulfillmentStatus.COMPLETED)

    def test_exception_and_retry_replays_are_fingerprinted(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        exception_key = uuid4()
        open_fulfillment_exception(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=exception_key,
            note="Synthetic exception",
        )
        open_fulfillment_exception(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=exception_key,
            note="Synthetic exception",
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            open_fulfillment_exception(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=exception_key,
                note="Different exception",
            )
        retry_key = uuid4()
        retry_fulfillment(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=retry_key,
            reason="Synthetic retry",
        )
        retry_fulfillment(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=retry_key,
            reason="Synthetic retry",
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            retry_fulfillment(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=retry_key,
                reason="Different retry",
            )

    def test_purchased_correction_and_removal_change_current_evidence(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        original = record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        replacement = correct_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="Corrected synthetic evidence",
        )
        self.assertEqual(replacement.corrects_id, original.pk)
        self.assertEqual(item.installed_games.filter(
            classification=InstalledGameClassification.PURCHASED,
            state=InstalledGameRecordState.RECORDED,
            superseded_by__isnull=True,
        ).get().pk, replacement.pk)
        removed = remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            reason="Removed synthetic evidence",
        )
        self.assertEqual(removed.corrects_id, replacement.pk)
        self.assertFalse(item.installed_games.filter(
            classification=InstalledGameClassification.PURCHASED,
            state=InstalledGameRecordState.RECORDED,
            superseded_by__isnull=True,
        ).exists())
        with self.assertRaises(DigitalFulfillmentValidationError):
            staff_verify_fulfillment_completion(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            )

    def test_correction_and_method_replays_reject_changed_payloads(self):
        _, _, _, item = self.provision_remote()
        operator = self.manager()
        method_key = uuid4()
        change_fulfillment_method(
            fulfillment_id=item.public_id, fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=operator, idempotency_key=method_key,
        )
        change_fulfillment_method(
            fulfillment_id=item.public_id, fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
            actor=operator, idempotency_key=method_key,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            change_fulfillment_method(
                fulfillment_id=item.public_id, fulfillment_method=DigitalCartFulfillmentMethod.REMOTE,
                actor=operator, idempotency_key=method_key,
            )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        correction_key = uuid4()
        first = correct_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=correction_key,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="Synthetic correction",
        )
        second = correct_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=correction_key,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="Synthetic correction",
        )
        self.assertEqual(first.pk, second.pk)
        with self.assertRaises(DigitalFulfillmentConflict):
            correct_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=correction_key,
                completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
                reason="Different correction",
            )

    def test_concurrent_identical_note_has_one_effect(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock proof")
        _, _, _, item = self.provision()
        operator = self.manager()
        key = uuid4(); barrier = Barrier(2); outcomes = []

        def runner():
            close_old_connections()
            try:
                barrier.wait()
                activity = add_fulfillment_note(
                    fulfillment_id=item.public_id, actor=operator,
                    idempotency_key=key, note="Concurrent synthetic note",
                )
                outcomes.append(("ok", activity.pk))
            except Exception as exc:
                outcomes.append(("error", type(exc).__name__))
            finally:
                close_old_connections()

        threads = [Thread(target=runner), Thread(target=runner)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual({kind for kind, _ in outcomes}, {"ok"})
        self.assertEqual(len({pk for _, pk in outcomes}), 1)

    def test_completed_and_active_are_irreversible_in_orm_and_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4())
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        staff_verify_fulfillment_completion(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        item.refresh_from_db(); entitlement = item.entitlement; completed_at = item.completed_at
        item.status = DigitalFulfillmentStatus.IN_PROGRESS; item.completed_at = None
        with self.assertRaises(ValidationError):
            item.save()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_digitalfulfillmentitem SET status='in_progress', completed_at=NULL WHERE id=%s",
                    [item.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_entitlement SET status='pending_fulfillment', activated_at=NULL WHERE id=%s",
                    [entitlement.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "WITH reversed_execution AS ("
                    "UPDATE digital_products_digitalfulfillmentitem "
                    "SET status='in_progress', completed_at=NULL WHERE id=%s RETURNING id"
                    ") UPDATE digital_products_entitlement "
                    "SET status='pending_fulfillment', activated_at=NULL "
                    "WHERE fulfillment_item_id IN (SELECT id FROM reversed_execution)",
                    [item.pk],
                )
        item.refresh_from_db(); entitlement.refresh_from_db()
        self.assertEqual(item.completed_at, completed_at)
        self.assertEqual(entitlement.status, DigitalEntitlementStatus.ACTIVE)

    def test_unsupported_entitlement_states_and_forged_actor_are_rejected(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        placement, _, _, item = self.provision()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_entitlement SET status='under_review' WHERE fulfillment_item_id=%s",
                    [item.pk],
                )
        with self.assertRaises(DatabaseError), transaction.atomic():
            FulfillmentActivity.objects.bulk_create([FulfillmentActivity(
                fulfillment_item=item,
                activity_type=FulfillmentActivityType.WORK_STARTED,
                actor_type=FulfillmentActivityActorType.STAFF,
                actor=placement.order.user,
                actor_authority=FulfillmentActorAuthority.UNASSIGNED_STAFF,
                visibility="customer_safe",
                idempotency_key=uuid4(),
                request_fingerprint="a" * 64,
            )])

    def test_duplicate_initial_activity_and_stale_intake_idempotency_fail_closed(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL guard proof")
        _, _, obligation, item = self.provision()
        with self.assertRaises(DatabaseError), transaction.atomic():
            FulfillmentActivity.objects.bulk_create([FulfillmentActivity(
                fulfillment_item=item,
                activity_type=FulfillmentActivityType.PROVISIONED,
                actor_type=FulfillmentActivityActorType.SYSTEM,
                actor_authority=FulfillmentActorAuthority.SYSTEM,
                visibility="customer_safe",
                idempotency_key=uuid4(),
                request_fingerprint="b" * 64,
            )])
        stale_key = uuid4()
        IdempotencyRecord.objects.create(
            scope="digital_fulfillment.provision", key=str(stale_key),
            request_hash=__import__(
                "cheatgame.financial_core.services.idempotency", fromlist=["canonical_request_hash"]
            ).canonical_request_hash({"operation": "provision-v2", "obligation": str(obligation.public_id)}),
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            provision_digital_fulfillment_obligation(
                obligation_public_id=obligation.public_id, idempotency_key=stale_key,
            )

    def test_all_operational_free_text_rejects_credential_material(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        for value in ("password=synthetic", "OTP 1234", "secret token", "browser cookie"):
            with self.assertRaises(DigitalFulfillmentValidationError):
                add_fulfillment_note(
                    fulfillment_id=item.public_id, actor=operator,
                    idempotency_key=uuid4(), note=value,
                )
        item.internal_reference = "credential URL"
        with self.assertRaises(ValidationError):
            item.save()

    def test_manager_assignment_replay_precedes_changed_assignment_authority(self):
        _, _, _, item = self.provision()
        actor = self.manager("1")
        target = self.manager("2")
        other = self.manager("3")
        key = uuid4()
        first = assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=target, actor=actor, idempotency_key=key,
        )
        second = assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=target, actor=actor, idempotency_key=key,
        )
        self.assertEqual(first.pk, second.pk)
        record_customer_contact(
            fulfillment_id=item.public_id, actor=target, idempotency_key=uuid4(),
        )
        self.assertEqual(
            assign_fulfillment_operator(
                fulfillment_id=item.public_id, operator=target, actor=actor, idempotency_key=key,
            ).pk,
            item.pk,
        )
        with self.assertRaises(DigitalFulfillmentConflict):
            assign_fulfillment_operator(
                fulfillment_id=item.public_id, operator=other, actor=actor, idempotency_key=key,
            )
        with self.assertRaises(DigitalFulfillmentConflict):
            assign_fulfillment_operator(
                fulfillment_id=item.public_id, operator=target, actor=other, idempotency_key=key,
            )
        self.assertEqual(item.activities.filter(
            activity_type=FulfillmentActivityType.OPERATOR_ASSIGNED,
        ).count(), 1)

    def test_correction_replay_survives_completion_without_duplicate_successor(self):
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        original = record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        key = uuid4()
        first = correct_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="Corrected immutable evidence",
        )
        staff_verify_fulfillment_completion(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        second = correct_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="Corrected immutable evidence",
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(InstalledGameRecord.objects.filter(corrects=original).count(), 1)
        with self.assertRaises(DigitalFulfillmentConflict):
            correct_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
                completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
                reason="Conflicting correction",
            )

    def test_remote_removal_replay_is_stable_before_and_after_completion(self):
        _, _, _, item = self.provision_remote()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        original = record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        key = uuid4()
        first = remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
            reason="Removed remote evidence",
        )
        self.assertEqual(remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
            reason="Removed remote evidence",
        ).pk, first.pk)
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_VERIFIED_REMOTE,
        )
        record_remote_handling(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            await_confirmation=False,
        )
        staff_verify_fulfillment_completion(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        self.assertEqual(remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
            reason="Removed remote evidence",
        ).pk, first.pk)
        self.assertEqual(InstalledGameRecord.objects.filter(corrects=original).count(), 1)
        with self.assertRaises(DigitalFulfillmentConflict):
            remove_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=key,
                reason="Conflicting removal",
            )

    def test_removed_evidence_is_terminal_in_model_service_and_postgresql(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL removed-chain guard proof")
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        original = record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        removed = remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            reason="Terminal removal",
        )
        with self.assertRaises(DigitalFulfillmentValidationError):
            correct_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
                completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
                reason="Cannot correct removed evidence",
            )
        with self.assertRaises(DigitalFulfillmentValidationError):
            remove_purchased_game_installation(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
                reason="Cannot remove twice",
            )
        snapshot = item.obligation.checkout_line.digital_snapshot
        successor = InstalledGameRecord(
            fulfillment_item=item, game_id=snapshot.product_id,
            delivered_version_id=snapshot.delivered_version_id,
            classification=InstalledGameClassification.PURCHASED,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            operator=operator, actor_authority=FulfillmentActorAuthority.ASSIGNED_OPERATOR,
            state=InstalledGameRecordState.RECORDED, corrects=removed,
            correction_reason="Forbidden revival", idempotency_key=uuid4(),
            request_fingerprint="r" * 64,
        )
        with self.assertRaises(ValidationError):
            successor.full_clean()
        with self.assertRaises(DatabaseError), transaction.atomic():
            InstalledGameRecord.objects.bulk_create([successor])
        with self.assertRaises(DigitalFulfillmentValidationError):
            staff_verify_fulfillment_completion(
                fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            )
        self.assertEqual(InstalledGameRecord.objects.filter(corrects=original).count(), 1)

    def test_evidence_successor_rejects_cross_execution_fork_and_self_cycle(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL evidence-chain guard proof")
        def add_two_lines(**kwargs):
            first = add_digital_offer_to_cart(**kwargs)
            second_pool = InventoryPool.objects.create(
                sellable_quantity=2, status=InventoryPoolStatus.ENABLED,
            )
            second_offer = DigitalOffer.objects.create(
                delivered_version=kwargs["offer"].delivered_version,
                customer_console=kwargs["offer"].customer_console,
                capacity=DigitalOfferCapacity.CAPACITY_2,
                price=kwargs["offer"].price + 1000,
                inventory_pool=second_pool,
                sale_state=DigitalOfferSaleState.ACTIVE,
            )
            add_digital_offer_to_cart(
                cart=kwargs["cart"], offer=second_offer,
                fulfillment_method=DigitalCartFulfillmentMethod.IN_STORE,
                actor=kwargs["actor"],
            )
            return first

        with patch(
            "cheatgame.financial_core.test_commercial_finalizer_phase1.add_digital_offer_to_cart",
            side_effect=add_two_lines,
        ):
            placement, _ = self.ready_digital()
        self.finalize(placement)
        obligations = list(DigitalFulfillmentObligation.objects.filter(
            order=placement.order,
        ).order_by("pk"))
        first_item, second_item = [
            provision_digital_fulfillment_obligation(
                obligation_public_id=obligation.public_id, idempotency_key=uuid4(),
            )
            for obligation in obligations
        ]
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=first_item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=first_item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=first_item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=first_item.public_id, actor=operator, idempotency_key=uuid4())
        original = record_purchased_game_installation(
            fulfillment_id=first_item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        correct_purchased_game_installation(
            fulfillment_id=first_item.public_id, actor=operator, idempotency_key=uuid4(),
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            reason="First immutable successor",
        )
        snapshot = first_item.obligation.checkout_line.digital_snapshot
        fork = InstalledGameRecord(
            fulfillment_item=first_item, game_id=snapshot.product_id,
            delivered_version_id=snapshot.delivered_version_id,
            classification=InstalledGameClassification.PURCHASED,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            operator=operator, actor_authority=FulfillmentActorAuthority.ASSIGNED_OPERATOR,
            state=InstalledGameRecordState.RECORDED, corrects=original,
            correction_reason="Fork", idempotency_key=uuid4(), request_fingerprint="f" * 64,
        )
        with self.assertRaises(DatabaseError), transaction.atomic():
            InstalledGameRecord.objects.bulk_create([fork])

        second_snapshot = second_item.obligation.checkout_line.digital_snapshot
        cross = InstalledGameRecord(
            fulfillment_item=second_item, game_id=second_snapshot.product_id,
            delivered_version_id=second_snapshot.delivered_version_id,
            classification=InstalledGameClassification.PURCHASED,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            operator=operator, actor_authority=FulfillmentActorAuthority.UNASSIGNED_STAFF,
            state=InstalledGameRecordState.RECORDED, corrects=original,
            correction_reason="Cross execution", idempotency_key=uuid4(),
            request_fingerprint="x" * 64,
        )
        with self.assertRaises(ValidationError):
            cross.full_clean()
        with self.assertRaises(DatabaseError), transaction.atomic():
            InstalledGameRecord.objects.bulk_create([cross])
        self_cycle = InstalledGameRecord(
            pk=999999, fulfillment_item=first_item, game_id=snapshot.product_id,
            delivered_version_id=snapshot.delivered_version_id,
            classification=InstalledGameClassification.PURCHASED,
            completion_source=InstalledGameCompletionSource.STAFF_INSTALLED,
            operator=operator, actor_authority=FulfillmentActorAuthority.ASSIGNED_OPERATOR,
            state=InstalledGameRecordState.RECORDED, corrects_id=999999,
            correction_reason="Self cycle", idempotency_key=uuid4(), request_fingerprint="c" * 64,
        )
        with self.assertRaises(ValidationError):
            self_cycle.clean()
        with self.assertRaises(DatabaseError), transaction.atomic():
            InstalledGameRecord.objects.bulk_create([self_cycle])

    def test_raw_sql_stale_removed_evidence_cannot_complete(self):
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL completion-graph proof")
        _, _, _, item = self.provision()
        operator = self.manager()
        assign_fulfillment_operator(
            fulfillment_id=item.public_id, operator=operator, actor=operator, idempotency_key=uuid4(),
        )
        record_customer_contact(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_console_received(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        start_fulfillment_work(fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4())
        record_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
        )
        remove_purchased_game_installation(
            fulfillment_id=item.public_id, actor=operator, idempotency_key=uuid4(),
            reason="Stale completion attack fixture",
        )
        now = timezone.now()
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE digital_products_digitalfulfillmentitem "
                    "SET status='completed', completed_at=%s WHERE id=%s",
                    [now, item.pk],
                )
                cursor.execute(
                    "UPDATE digital_products_entitlement "
                    "SET status='active', activated_at=%s WHERE fulfillment_item_id=%s",
                    [now, item.pk],
                )
            FulfillmentActivity.objects.bulk_create([
                FulfillmentActivity(
                    fulfillment_item=item, activity_type=FulfillmentActivityType.STAFF_VERIFIED,
                    actor_type=FulfillmentActivityActorType.STAFF, actor=operator,
                    actor_authority=FulfillmentActorAuthority.ASSIGNED_OPERATOR,
                    visibility="customer_safe", idempotency_key=uuid4(),
                    request_fingerprint="v" * 64,
                ),
                FulfillmentActivity(
                    fulfillment_item=item, activity_type=FulfillmentActivityType.STATUS_CHANGED,
                    actor_type=FulfillmentActivityActorType.STAFF, actor=operator,
                    actor_authority=FulfillmentActorAuthority.ASSIGNED_OPERATOR,
                    visibility="customer_safe", previous_status=DigitalFulfillmentStatus.IN_PROGRESS,
                    new_status=DigitalFulfillmentStatus.COMPLETED, idempotency_key=uuid4(),
                    request_fingerprint="s" * 64,
                ),
            ])
