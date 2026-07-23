from decimal import Decimal
from typing import Iterable

from cheatgame.product.models import Attachment, Product, ProductType


def product_original_unit_price(*, product: Product) -> Decimal:
    return Decimal(product.price or 0)


def product_effective_unit_price(*, product: Product) -> Decimal:
    off_price = Decimal(product.off_price or 0)
    if off_price > Decimal("0"):
        return off_price
    return product_original_unit_price(product=product)


def product_discount_per_unit(*, product: Product) -> Decimal:
    original_price = product_original_unit_price(product=product)
    effective_price = product_effective_unit_price(product=product)
    return max(original_price - effective_price, Decimal("0"))


def selected_attachment_unit_total(*, attachments: Iterable[Attachment], product: Product) -> Decimal:
    total = Decimal("0")
    for attachment in attachments or []:
        if product.product_type == ProductType.GAME:
            total = Decimal(attachment.price or 0)
        else:
            total += Decimal(attachment.price or 0)
    return total


def product_line_payable_total(*, product: Product, attachment_total: Decimal, quantity: int) -> Decimal:
    quantity = int(quantity or 1)
    if product.product_type == ProductType.GAME:
        return attachment_total * quantity
    return (product_effective_unit_price(product=product) + attachment_total) * quantity


def product_line_original_total(*, product: Product, attachment_total: Decimal, quantity: int) -> Decimal:
    quantity = int(quantity or 1)
    if product.product_type == ProductType.GAME:
        return attachment_total * quantity
    return (product_original_unit_price(product=product) + attachment_total) * quantity


def product_line_savings(*, product: Product, quantity: int) -> Decimal:
    quantity = int(quantity or 1)
    if product.product_type == ProductType.GAME:
        return Decimal("0")
    return product_discount_per_unit(product=product) * quantity
