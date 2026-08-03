import hmac

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from cheatgame.financial_core.models import (
    CANONICAL_CURRENCY,
    FinancialActorType,
    FinancialEvent,
    PaymentAttempt,
    PaymentAttemptStatus,
    PaymentCollectionStatus,
    PaymentTransactionStatus,
    VerificationWorkType,
    Payment,
)
from cheatgame.financial_core.services.adapters import (
    ADAPTER_CONTRACT_VERSION,
    build_production_adapter_registry,
)
from cheatgame.financial_core.services.events import append_financial_event
from cheatgame.financial_core.services.financial_certification import (
    FINANCIAL_CERTIFICATION_ADAPTER_KEY,
    FINANCIAL_CERTIFICATION_PROVIDER_KEY,
    certification_authority,
    certification_identity_from_transaction,
    certification_reference,
)
from cheatgame.financial_core.services.verification import enqueue_verification_work
from cheatgame.shop.models import Checkout, CheckoutStatus, Order
from cheatgame.users.models import UserTypes


class Command(BaseCommand):
    help = "Authorize one staging-only non-monetary PaymentAttempt for durable verification."

    def add_arguments(self, parser):
        parser.add_argument("--payment-attempt", required=True)
        parser.add_argument("--actor-id", required=True, type=int)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Explicitly confirm this non-monetary staging certification.",
        )

    def handle(self, *args, **options):
        del args
        if not options["confirm"]:
            raise CommandError("--confirm is required.")
        adapter = build_production_adapter_registry().resolve(
            adapter_key=FINANCIAL_CERTIFICATION_ADAPTER_KEY,
            contract_version=ADAPTER_CONTRACT_VERSION,
        )
        with transaction.atomic():
            actor = get_user_model().objects.select_for_update().filter(pk=options["actor_id"]).first()
            if (
                actor is None
                or not actor.is_active
                or actor.user_type != UserTypes.ADMIN
            ):
                raise CommandError("An active application Admin actor is required.")
            attempt = PaymentAttempt.objects.select_for_update().filter(
                public_id=options["payment_attempt"]
            ).first()
            if attempt is None:
                raise CommandError("PaymentAttempt was not found.")
            if attempt.provider != FINANCIAL_CERTIFICATION_PROVIDER_KEY:
                raise CommandError("PaymentAttempt does not belong to Financial Certification.")
            payment = Payment.objects.select_for_update().get(pk=attempt.payment_id)
            order = Order.objects.select_for_update().get(pk=payment.order_id)
            checkout = Checkout.objects.select_for_update().get(pk=order.checkout_id)
            attempt.payment = payment
            transactions = list(
                attempt.transactions.select_for_update().order_by("sequence", "pk")
            )
            if len(transactions) != 1:
                raise CommandError("PaymentAttempt transaction lineage is not singular.")
            transaction_obj = transactions[0]
            event_key = f"financial-certification:{attempt.public_id}"
            existing_event = FinancialEvent.objects.filter(idempotency_key=event_key).first()
            existing_work = transaction_obj.verification_work_items.filter(
                deterministic_identity=event_key
            ).first()
            if existing_event or existing_work:
                if not existing_event or not existing_work:
                    raise CommandError("Certification evidence is incomplete.")
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Certification already recorded: payment_attempt={attempt.public_id}, "
                        f"work={existing_work.public_id}"
                    )
                )
                return
            if (
                attempt.status != PaymentAttemptStatus.PROCESSING
                or transaction_obj.status != PaymentTransactionStatus.PENDING_PROVIDER
                or payment.collection_status != PaymentCollectionStatus.PROCESSING
                or checkout.status != CheckoutStatus.PENDING_PAYMENT
                or checkout.expires_at <= timezone.now()
            ):
                raise CommandError("PaymentAttempt is no longer eligible for certification.")
            remaining = payment.amount_due - payment.confirmed_amount
            if (
                attempt.requested_amount != remaining
                or transaction_obj.amount != remaining
                or attempt.currency != CANONICAL_CURRENCY
                or transaction_obj.currency != CANONICAL_CURRENCY
            ):
                raise CommandError("Immutable payment amount or currency is contradictory.")
            identity = certification_identity_from_transaction(transaction_obj)
            expected_authority = certification_authority(secret=adapter.secret, identity=identity)
            if not hmac.compare_digest(transaction_obj.provider_authority, expected_authority):
                raise CommandError("Certification provider authority is invalid.")
            reference = certification_reference(secret=adapter.secret, identity=identity)
            aggregate_version = (
                FinancialEvent.objects.filter(
                    aggregate_type="payment_attempt",
                    aggregate_id=str(attempt.public_id),
                ).aggregate(value=Max("aggregate_version"))["value"]
                or 0
            ) + 1
            append_financial_event(
                aggregate_type="payment_attempt",
                aggregate_id=str(attempt.public_id),
                aggregate_version=aggregate_version,
                event_type="financial_certification.confirmed",
                actor_type=FinancialActorType.ADMIN,
                actor_id=actor.pk,
                idempotency_key=event_key,
                correlation_id=transaction_obj.correlation_id,
                causation_id=transaction_obj.public_id,
                metadata={
                    "provider": FINANCIAL_CERTIFICATION_PROVIDER_KEY,
                    "currency": transaction_obj.currency,
                    "amount": transaction_obj.amount,
                    "outcome": "confirmed",
                    "provider_reference": reference,
                },
            )
            work, _ = enqueue_verification_work(
                transaction_obj=transaction_obj,
                work_type=VerificationWorkType.POLL_PENDING_OPERATION,
                deterministic_identity=event_key,
                correlation_id=transaction_obj.correlation_id,
                causation_id=transaction_obj.public_id,
                max_attempts=1,
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Certification recorded: payment_attempt={attempt.public_id}, work={work.public_id}"
            )
        )
