from datetime import timedelta
from io import StringIO
from threading import Barrier, Lock, Thread
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from cheatgame.digital_products.models import (
    DigitalEntitlementStatus,
    DigitalFulfillmentItem,
    DigitalFulfillmentStatus,
    Entitlement,
    FulfillmentActivity,
    FulfillmentActivityType,
)
from cheatgame.digital_products.services.fulfillment import (
    DigitalFulfillmentConflict,
    activate_digital_fulfillment_obligation,
    digital_fulfillment_activation_stats,
    pending_digital_fulfillment_obligation_ids,
)
from cheatgame.financial_core.models import (
    CommercialAccountingPolicyVersion,
    DigitalFulfillmentObligation,
    FinancialOutboxMessage,
)
from cheatgame.financial_core.services.commercial_finalization import (
    FULFILLMENT_OUTBOX_TOPIC,
)
from cheatgame.financial_core.test_commercial_finalizer_phase1 import (
    CommercialFinalizerFixture,
)


class DigitalFulfillmentActivationTests(
    CommercialFinalizerFixture,
    TransactionTestCase,
):
    reset_sequences = True

    def finalized_obligation(self):
        placement, pool = self.ready_digital()
        self.finalize(placement)
        obligation = DigitalFulfillmentObligation.objects.get(
            order=placement.order
        )
        return placement, pool, obligation

    def test_finalization_remains_dormant_until_explicit_activation(self):
        _, _, obligation = self.finalized_obligation()

        self.assertFalse(
            DigitalFulfillmentItem.objects.filter(
                obligation=obligation
            ).exists()
        )
        self.assertEqual(
            pending_digital_fulfillment_obligation_ids(limit=10),
            [obligation.public_id],
        )

    def test_outbox_activation_reuses_existing_provisioning_graph(self):
        _, pool, obligation = self.finalized_obligation()

        first = activate_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id
        )
        second = activate_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id
        )

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.status, DigitalFulfillmentStatus.QUEUED)
        self.assertEqual(
            Entitlement.objects.filter(
                obligation=obligation,
                status=DigitalEntitlementStatus.PENDING_FULFILLMENT,
            ).count(),
            1,
        )
        self.assertEqual(
            FulfillmentActivity.objects.filter(
                fulfillment_item=first,
                activity_type=FulfillmentActivityType.PROVISIONED,
            ).count(),
            1,
        )
        pool.refresh_from_db()
        self.assertEqual(pool.sellable_quantity, 1)

    def test_existing_database_guard_rejects_duplicate_outbox_authority(self):
        _, _, obligation = self.finalized_obligation()
        original = FinancialOutboxMessage.objects.get(
            topic=FULFILLMENT_OUTBOX_TOPIC
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FinancialOutboxMessage.objects.create(
                topic=original.topic,
                aggregate_type=original.aggregate_type,
                aggregate_id=original.aggregate_id,
                idempotency_key=f"{original.idempotency_key}:duplicate",
                correlation_id=original.correlation_id,
                causation_id=original.causation_id,
                available_at=original.available_at,
                safe_payload=original.safe_payload,
            )
        self.assertFalse(
            DigitalFulfillmentItem.objects.filter(
                obligation=obligation
            ).exists()
        )

    def test_command_is_dry_run_by_default_and_apply_is_bounded(self):
        _, _, obligation = self.finalized_obligation()
        output = StringIO()

        call_command(
            "activate_digital_fulfillment",
            "run-batch",
            "--limit",
            "1",
            stdout=output,
        )
        self.assertIn("dry_run=true", output.getvalue())
        self.assertFalse(
            DigitalFulfillmentItem.objects.filter(
                obligation=obligation
            ).exists()
        )

        output = StringIO()
        call_command(
            "activate_digital_fulfillment",
            "run-batch",
            "--limit",
            "1",
            "--apply",
            stdout=output,
        )
        self.assertIn(str(obligation.public_id), output.getvalue())
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 1)

    def test_run_one_requires_an_explicit_obligation(self):
        with self.assertRaises(CommandError):
            call_command(
                "activate_digital_fulfillment",
                "run-one",
                "--apply",
                stdout=StringIO(),
            )

    def test_stats_expose_oldest_eligible_age_and_resulting_queue(self):
        _, _, obligation = self.finalized_obligation()
        before = digital_fulfillment_activation_stats(
            now=timezone.now() + timedelta(seconds=10)
        )
        self.assertEqual(before["eligible_obligations"], 1)
        self.assertGreaterEqual(before["oldest_eligible_age_seconds"], 10)
        self.assertEqual(before["queued"], 0)

        activate_digital_fulfillment_obligation(
            obligation_public_id=obligation.public_id
        )
        after = digital_fulfillment_activation_stats()
        self.assertEqual(after["eligible_obligations"], 0)
        self.assertEqual(after["provisioned_obligations"], 1)
        self.assertEqual(after["queued"], 1)

    def test_concurrent_activation_converges_on_one_fulfillment_graph(self):
        _, _, obligation = self.finalized_obligation()
        barrier = Barrier(2)
        guard = Lock()
        item_ids = []
        errors = []

        def invoke():
            close_old_connections()
            try:
                barrier.wait()
                item = activate_digital_fulfillment_obligation(
                    obligation_public_id=obligation.public_id
                )
                with guard:
                    item_ids.append(item.pk)
            except Exception as exc:
                with guard:
                    errors.append(type(exc).__name__)
            finally:
                close_old_connections()

        first = Thread(target=invoke)
        second = Thread(target=invoke)
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(item_ids)), 1)
        self.assertEqual(DigitalFulfillmentItem.objects.count(), 1)
        self.assertEqual(Entitlement.objects.count(), 1)

    def test_batch_isolates_failure_and_returns_nonzero(self):
        _, _, broken = self.finalized_obligation()
        CommercialAccountingPolicyVersion.objects.filter(
            commerce_authority="digital_products",
            active_for_new_finalizations=True,
        ).update(active_for_new_finalizations=False)

        original_create = CommercialAccountingPolicyVersion.objects.create

        def create_next_policy(**kwargs):
            kwargs["version"] = 2
            return original_create(**kwargs)

        with patch.object(
            CommercialAccountingPolicyVersion.objects,
            "create",
            side_effect=create_next_policy,
        ):
            _, _, healthy = self.finalized_obligation()
        output = StringIO()

        from cheatgame.digital_products.services import fulfillment

        original = fulfillment._fulfillment_intake_event

        def fail_one(obligation):
            if obligation.pk == broken.pk:
                raise DigitalFulfillmentConflict(
                    "Synthetic incoherent intake authority."
                )
            return original(obligation)

        with patch(
            "cheatgame.digital_products.services.fulfillment._fulfillment_intake_event",
            side_effect=fail_one,
        ), self.assertRaises(CommandError):
            call_command(
                "activate_digital_fulfillment",
                "run-batch",
                "--limit",
                "2",
                "--apply",
                stdout=output,
            )

        self.assertIn('"failed": 1', output.getvalue())
        self.assertTrue(
            DigitalFulfillmentItem.objects.filter(
                obligation=healthy
            ).exists()
        )
        self.assertFalse(
            DigitalFulfillmentItem.objects.filter(
                obligation=broken
            ).exists()
        )
