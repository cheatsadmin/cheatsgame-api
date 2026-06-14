from django.db.models import QuerySet, Prefetch

from cheatgame.product.filters import ProductFilter
from cheatgame.product.models import Product, ProductStatus, Question, Reviews, Label, LabelType, SuggestionProduct, ReviewStatus


def product_list(*, filters=None, include_unpublished: bool = False) -> QuerySet[Product]:
    filters = filters or {}
    qs = Product.objects.all()
    if not include_unpublished:
        qs = qs.filter(status=ProductStatus.PUBLISHED)
    return ProductFilter(filters, qs).qs.prefetch_related(
        "attachments",
        "categories__category",
    )


def products_numbers() -> int:
    return Product.objects.all().count()


def product_detail(*, slug: str, include_unpublished: bool = False) -> Product:
    qs = Product.objects.filter(slug=slug)
    if not include_unpublished:
        qs = qs.filter(status=ProductStatus.PUBLISHED)
    return qs.prefetch_related(
        "images",
        "categories__category",
        "valueslist",
        "attachments",
        "suggestions",
        "labels",
        Prefetch("reviews", queryset=Reviews.objects.filter(status=ReviewStatus.APPROVED, accepted=True)),
        Prefetch("questions", queryset=Question.objects.filter(accepted=True)),
        "notes"
    ).first()


def label_list_brands() -> QuerySet[Label]:
    return Label.objects.filter(label_type=LabelType.BRAND)

def label_list_consoles() -> QuerySet[Label]:
    return Label.objects.filter(label_type=LabelType.CONSOLE)

def label_list_capabilities() -> QuerySet[Label]:
    return Label.objects.filter(label_type=LabelType.CAPACITY)


def suggestions_product(*, product: Product):
    suggestion_objects = SuggestionProduct.objects.filter(product=product).select_related("suggested")
    suggested_list = [instance.suggested for instance in suggestion_objects ]
    return suggested_list
