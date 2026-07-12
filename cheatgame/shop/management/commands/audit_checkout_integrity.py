from django.core.management.base import BaseCommand
from django.db.models import Count, F

from cheatgame.shop.models import (
    Checkout,
    CheckoutStatus,
    CommerceEventType,
    PaymentTransaction,
    StockReservation,
    StockReservationState,
)


class Command(BaseCommand):
    help = "Report V2 checkout integrity inconsistencies without changing data."

    def handle(self, *args, **options):
        active = Checkout.objects.filter(status__in=Checkout.ACTIVE_STATUSES)
        active_cart_mismatch = list(
            active.filter(cart__isnull=False)
            .exclude(cart__active_checkout=F("id"))
            .values_list("id", flat=True)
        )
        active_without_cart = list(active.filter(cart__isnull=True).values_list("id", flat=True))
        paid_with_locked_cart = list(
            Checkout.objects.filter(status=CheckoutStatus.PAID, cart__active_checkout=F("id"))
            .values_list("id", flat=True)
        )
        duplicate_reservations = list(
            StockReservation.objects.filter(state=StockReservationState.ACTIVE)
            .values("checkout_id", "product_id")
            .annotate(count=Count("id"))
            .filter(count__gt=1)
        )
        manual_review_without_event = list(
            Checkout.objects.filter(status=CheckoutStatus.REQUIRES_MANUAL_REVIEW)
            .exclude(events__event_type=CommerceEventType.MANUAL_REVIEW_REQUIRED)
            .values_list("id", flat=True)
        )
        manual_review_without_transaction = list(
            Checkout.objects.filter(status=CheckoutStatus.REQUIRES_MANUAL_REVIEW)
            .filter(payment_transactions__isnull=True)
            .values_list("id", flat=True)
        )
        legacy_transactions = PaymentTransaction.objects.filter(checkout__isnull=True).count()

        report = {
            "active_cart_mismatch": active_cart_mismatch,
            "active_without_cart": active_without_cart,
            "paid_with_locked_cart": paid_with_locked_cart,
            "duplicate_active_reservations": duplicate_reservations,
            "manual_review_without_event": manual_review_without_event,
            "manual_review_without_transaction": manual_review_without_transaction,
            "legacy_transactions_without_checkout_info": legacy_transactions,
        }
        issue_count = sum(
            len(value) if isinstance(value, list) else 0
            for key, value in report.items()
            if not key.endswith("_info")
        )
        self.stdout.write(f"report_only=true issue_count={issue_count} report={report}")
