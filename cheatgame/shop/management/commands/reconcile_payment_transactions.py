from django.core.management.base import BaseCommand
from django.db.models import Q

from cheatgame.shop.models import CheckoutStatus, PaymentTransaction, PaymentTransactionStatus


class Command(BaseCommand):
    help = "Report payment transactions requiring reconciliation. Phase A never calls a provider or mutates state."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)

    def handle(self, *args, **options):
        queryset = (
            PaymentTransaction.objects.filter(checkout__isnull=False)
            .filter(
                Q(
                    status__in=(
                        PaymentTransactionStatus.CALLBACK_RECEIVED,
                        PaymentTransactionStatus.VERIFYING,
                        PaymentTransactionStatus.REQUIRES_MANUAL_REVIEW,
                    )
                )
                | Q(status=PaymentTransactionStatus.PAID, checkout__status__in=(
                    CheckoutStatus.CHECKOUT_DRAFT,
                    CheckoutStatus.PENDING_PAYMENT,
                    CheckoutStatus.REQUIRES_MANUAL_REVIEW,
                    CheckoutStatus.EXPIRED,
                ))
            )
            .select_related("checkout")
            .order_by("created_at")[: options["limit"]]
        )
        rows = [
            {
                "transaction_id": item.id,
                "checkout_id": item.checkout_id,
                "payment_status": item.status,
                "checkout_status": item.checkout.status,
                "reason": item.manual_review_reason or "",
            }
            for item in queryset
        ]
        self.stdout.write(f"report_only=true count={len(rows)} rows={rows}")
