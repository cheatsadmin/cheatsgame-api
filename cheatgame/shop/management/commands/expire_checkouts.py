from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cheatgame.shop.models import (
    CartState,
    Checkout,
    CheckoutStatus,
    CommerceEventType,
    PaymentTransactionStatus,
    StockReservation,
    StockReservationState,
)
from cheatgame.shop.services.commerce_foundation import append_commerce_event


PROTECTED_PAYMENT_STATUSES = (
    PaymentTransactionStatus.CREATED,
    PaymentTransactionStatus.PENDING,
    PaymentTransactionStatus.CALLBACK_RECEIVED,
    PaymentTransactionStatus.VERIFYING,
    PaymentTransactionStatus.PAID,
    PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
)


class Command(BaseCommand):
    help = "Report eligible V2 checkout drafts. Use --apply to expire them."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        now = timezone.now()
        checkout_ids = list(
            Checkout.objects.filter(
                status__in=(CheckoutStatus.CHECKOUT_DRAFT, CheckoutStatus.PENDING_PAYMENT),
                expires_at__lte=now,
            )
            .exclude(payment_transactions__status__in=PROTECTED_PAYMENT_STATUSES)
            .order_by("expires_at")
            .values_list("id", flat=True)
            .distinct()[: options["limit"]]
        )

        if not options["apply"]:
            self.stdout.write(f"dry_run=true eligible_count={len(checkout_ids)} checkout_ids={checkout_ids}")
            return

        expired_ids = []
        for checkout_id in checkout_ids:
            with transaction.atomic():
                checkout = Checkout.objects.select_for_update().filter(id=checkout_id).first()
                if checkout is None or checkout.status not in (
                    CheckoutStatus.CHECKOUT_DRAFT,
                    CheckoutStatus.PENDING_PAYMENT,
                ):
                    continue
                if checkout.payment_transactions.filter(status__in=PROTECTED_PAYMENT_STATUSES).exists():
                    continue

                previous_status = checkout.status
                checkout.status = CheckoutStatus.EXPIRED
                checkout.expired_at = now
                checkout.version += 1
                checkout.save(update_fields=["status", "expired_at", "version", "updated_at"])

                StockReservation.objects.filter(
                    checkout=checkout,
                    state=StockReservationState.ACTIVE,
                ).update(state=StockReservationState.RELEASED, updated_at=now)

                if checkout.cart_id:
                    cart = checkout.cart.__class__.objects.select_for_update().get(id=checkout.cart_id)
                    if cart.active_checkout_id == checkout.id:
                        cart.state = CartState.OPEN
                        cart.lock_reason = None
                        cart.active_checkout = None
                        cart.locked_at = None
                        cart.lock_version += 1
                        cart.save(
                            update_fields=[
                                "state",
                                "lock_reason",
                                "active_checkout",
                                "locked_at",
                                "lock_version",
                                "updated_at",
                            ]
                        )

                append_commerce_event(
                    checkout=checkout,
                    event_type=CommerceEventType.CHECKOUT_EXPIRED,
                    metadata={
                        "previous_status": previous_status,
                        "new_status": CheckoutStatus.EXPIRED,
                        "expires_at": checkout.expires_at.isoformat(),
                    },
                )
                expired_ids.append(checkout.id)

        self.stdout.write(f"expired_count={len(expired_ids)} checkout_ids={expired_ids}")
