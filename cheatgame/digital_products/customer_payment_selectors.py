from django.db.models import Prefetch

from cheatgame.financial_core.models import PaymentAttempt, PaymentTransaction
from cheatgame.product.models import ProductCommerceAuthority
from cheatgame.shop.models import Checkout, Order


def customer_digital_payment_queryset(*, user):
    transactions = PaymentTransaction.objects.order_by("sequence")
    attempts = PaymentAttempt.objects.order_by("sequence").prefetch_related(
        Prefetch("transactions", queryset=transactions)
    )
    orders = Order.objects.order_by("pk").select_related(
        "financial_payment",
        "financial_payment__obligation_source",
    ).prefetch_related(
        Prefetch("financial_payment__attempts", queryset=attempts)
    )
    return (
        Checkout.objects.filter(user=user)
        .filter(lines__commerce_authority=ProductCommerceAuthority.DIGITAL_PRODUCTS)
        .select_related("cart")
        .prefetch_related(Prefetch("orders", queryset=orders), "lines")
        .distinct()
    )


def owned_customer_digital_payment_checkout(*, user, public_id):
    return customer_digital_payment_queryset(user=user).filter(public_id=public_id).first()
