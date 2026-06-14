from decimal import Decimal

from django.db import transaction
from django.db.models import Avg

from cheatgame.product.models import Product, Reviews, ReviewStatus
from cheatgame.users.models import BaseUser


@transaction.atomic
def create_or_update_review(*, user: BaseUser, product: Product, rating: int, comment: str) -> Reviews:
    review, _ = Reviews.objects.update_or_create(
        user=user,
        product=product,
        defaults={
            "rating": rating,
            "comment": comment,
            "status": ReviewStatus.PENDING,
            "accepted": False,
        },
    )
    calculate_product_rating(product=product)
    return review


def moderate_review(*, review: Reviews, status: str) -> Reviews:
    review.status = status
    review.accepted = status == ReviewStatus.APPROVED
    review.save(update_fields=["status", "accepted", "updated_at"])
    calculate_product_rating(product=review.product)
    return review


def calculate_product_rating(*, product: Product) -> None:
    review = Reviews.objects.filter(
        product=product,
        status=ReviewStatus.APPROVED,
        accepted=True,
    ).aggregate(average_rating=Avg("rating"))
    rating_avg = review.get("average_rating")
    product.score = Decimal(rating_avg) if rating_avg is not None else Decimal("0")
    product.save(update_fields=["score", "updated_at"])


# Backwards-compatible alias for existing imports.
calculate_product_ranting = calculate_product_rating
