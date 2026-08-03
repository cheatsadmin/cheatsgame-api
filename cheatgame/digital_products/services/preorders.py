from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from cheatgame.digital_products.models import (
    DigitalCheckoutLineSnapshot,
    DigitalGameUpcomingStatus,
    DigitalInventoryReservation,
    DigitalInventoryReservationState,
)
from cheatgame.financial_core.models import (
    DigitalFulfillmentObligation,
    PaymentCollectionStatus,
    PerformanceObligationComponent,
    PerformanceObligationComponentType,
)
from cheatgame.financial_core.services.idempotency import canonical_request_hash
from cheatgame.shop.models import OrderItem, OrderStatus


PREORDER_PURCHASE_KIND = "preorder"


def snapshot_is_preorder(snapshot):
    return (
        snapshot.safe_display_metadata.get("purchase_kind")
        == PREORDER_PURCHASE_KIND
    )


def _create_release_component(*, finalization, order_item, line, obligation):
    base_components = list(
        PerformanceObligationComponent.objects.select_related("obligation")
        .filter(
            obligation__finalization=finalization,
            order_item=order_item,
            checkout_line=line,
        )
        .order_by("sequence", "pk")
    )
    if not base_components:
        return
    performance_obligation = base_components[0].obligation
    existing = PerformanceObligationComponent.objects.filter(
        obligation=performance_obligation,
        digital_fulfillment_obligation=obligation,
    ).first()
    if existing:
        return
    sequence = max(component.sequence for component in base_components) + 1
    PerformanceObligationComponent.objects.create(
        obligation=performance_obligation,
        order=finalization.order,
        order_item=order_item,
        checkout_line=line,
        digital_fulfillment_obligation=obligation,
        component_key=f"released-fulfillment:{order_item.pk}",
        component_type=PerformanceObligationComponentType.FULFILLMENT,
        source_authority_identity=str(obligation.public_id),
        quantity=1,
        commercial_snapshot_digest=canonical_request_hash(line.snapshot),
        sequence=sequence,
        component_contract_version="preorder-release-v1",
    )


@transaction.atomic
def release_paid_preorders_for_product(*, product):
    """Materialize ordinary Digital fulfillment authority when a Product is released."""
    metadata = product.digital_release_metadata
    if metadata.upcoming_status != DigitalGameUpcomingStatus.RELEASED:
        raise ValueError("Only a RELEASED Product can materialize preorder fulfillment.")

    snapshots = list(
        DigitalCheckoutLineSnapshot.objects.select_for_update()
        .select_related(
            "checkout_line__checkout",
            "inventory_pool",
        )
        .filter(
            product_id=product.pk,
            safe_display_metadata__purchase_kind=PREORDER_PURCHASE_KIND,
            checkout_line__checkout__orders__payment_status=OrderStatus.PAID.value,
            checkout_line__checkout__orders__commercial_finalization__payment__collection_status=(
                PaymentCollectionStatus.PAID
            ),
        )
        .order_by("checkout_line__checkout_id", "checkout_line_id")
    )
    created = 0
    for snapshot in snapshots:
        line = snapshot.checkout_line
        try:
            order = line.checkout.orders.get()
            finalization = order.commercial_finalization
        except (ObjectDoesNotExist, ValueError) as exc:
            raise ValueError("Paid preorder ownership is incomplete.") from exc

        lines = list(line.checkout.lines.order_by("pk"))
        items = list(OrderItem.objects.select_for_update().filter(order=order).order_by("pk"))
        if len(lines) != len(items):
            raise ValueError("Paid preorder line ownership is incoherent.")
        try:
            order_item = items[lines.index(line)]
        except (ValueError, IndexError) as exc:
            raise ValueError("Paid preorder OrderItem is missing.") from exc
        if order_item.product_id != snapshot.product_id or order_item.quantity != 1:
            raise ValueError("Paid preorder OrderItem identity is incoherent.")

        reservation = DigitalInventoryReservation.objects.select_for_update().filter(
            order=order,
            checkout_line=line,
            inventory_pool=snapshot.inventory_pool,
            state=DigitalInventoryReservationState.CONSUMED,
        ).first()
        if reservation is None:
            raise ValueError("Paid preorder consumed reservation is missing.")

        existing = DigitalFulfillmentObligation.objects.filter(
            order_item=order_item
        ).first()
        if existing:
            if (
                existing.finalization_id != finalization.pk
                or existing.order_id != order.pk
                or existing.reservation_id != reservation.pk
                or existing.checkout_line_id != line.pk
            ):
                raise ValueError("Existing preorder fulfillment lineage is contradictory.")
            _create_release_component(
                finalization=finalization,
                order_item=order_item,
                line=line,
                obligation=existing,
            )
            continue

        obligation = DigitalFulfillmentObligation.objects.create(
            finalization=finalization,
            order=order,
            order_item=order_item,
            reservation=reservation,
            inventory_pool=snapshot.inventory_pool,
            checkout_line=line,
            quantity=1,
            fulfillment_method=snapshot.fulfillment_method,
        )
        _create_release_component(
            finalization=finalization,
            order_item=order_item,
            line=line,
            obligation=obligation,
        )
        created += 1
    return created
