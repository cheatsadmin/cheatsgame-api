from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify

from cheatgame.product.models import Category, Product, ProductCategory, ProductNote, ProductStatus


class ProductDeleteProtectedError(ValueError):
    pass


class ProductDeleteDependencyError(ValueError):
    pass


@transaction.atomic
def create_product(*, product_type: int, title: str, main_image: str, price: float, off_price: float,
                   quantity: int, discount_end_time=None, description: str, included_products: list = None,
                   order_limit: int = None, device_model: str = None, slug: str = "", status: str = ProductStatus.PUBLISHED,
                   seo_title: str = "", meta_description: str = "", categories: list[Category] = None) -> Product:
    product = Product.objects.create(
        product_type=product_type,
        title=title,
        slug=build_unique_product_slug(slug or title),
        status=status,
        seo_title=seo_title or "",
        meta_description=meta_description or "",
        main_image=main_image,
        price=price,
        off_price=off_price,
        quantity=quantity,
        discount_end_time=discount_end_time,
        description=description,
        order_limit=order_limit,
        device_model=device_model,
    )
    if included_products:
        product_ids = [product.id for product in included_products]
        included_products = Product.objects.filter(id__in=product_ids)
        product.included_products.add(*included_products)
    set_product_categories(product=product, categories=categories or [])
    return product


def build_unique_product_slug(value: str, *, exclude_product_id: int = None) -> str:
    base_slug = slugify(value, allow_unicode=True) or "product"
    base_slug = base_slug[:110]
    slug = base_slug
    counter = 2
    queryset = Product.objects.all()
    if exclude_product_id:
        queryset = queryset.exclude(id=exclude_product_id)

    while queryset.filter(slug=slug).exists():
        suffix = f"-{counter}"
        slug = f"{base_slug[:120 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def set_product_categories(*, product: Product, categories: list[Category]) -> None:
    ProductCategory.objects.filter(product=product).delete()
    ProductCategory.objects.bulk_create(
        [ProductCategory(product=product, category=category) for category in categories],
        ignore_conflicts=True,
    )


def check_product_exists(*, product_id: int) -> bool:
    return Product.objects.filter(id=product_id).exists()

@transaction.atomic
def delete_product(*, product_id: int) -> None:
    from cheatgame.shop.models import CartItem, OrderItem, OrderItemAttachment

    product = Product.objects.select_for_update().get(id=product_id)
    has_order_history = (
        OrderItem.objects.filter(product=product).exists()
        or OrderItemAttachment.objects.filter(attachment__product=product).exists()
    )
    if has_order_history:
        raise ProductDeleteProtectedError(
            "این محصول به سفارش‌ها متصل است و قابل حذف نیست؛ آن را مخفی کنید."
        )

    CartItem.objects.filter(product=product).delete()
    try:
        product.delete()
    except ProtectedError as exc:
        raise ProductDeleteDependencyError(
            "ابتدا ارتباطات وابسته به این محصول باید پاک شود."
        ) from exc

@transaction.atomic
def update_product(*, product_id: int, product_type: int, title: str, main_image: str = None, price: float = 0,
                   off_price: float = 0, quantity: int = 0, discount_end_time=None, description: str = None,
                   order_limit: int = None, device_model: str = None, slug: str = None,
                   status: str = ProductStatus.PUBLISHED, seo_title: str = "", meta_description: str = "",
                   categories: list[Category] = None) -> Product:
    product = Product.objects.select_for_update().get(id=product_id)
    product.product_type = product_type
    product.title = title
    if slug is not None:
        product.slug = slugify(slug, allow_unicode=True) or build_unique_product_slug(
            title,
            exclude_product_id=product_id,
        )
    product.status = status
    product.seo_title = seo_title or ""
    product.meta_description = meta_description or ""
    if main_image is not None:
        product.main_image = main_image
    product.price = price
    product.off_price = off_price
    product.quantity = quantity
    product.discount_end_time = discount_end_time
    if description is not None:
        product.description = description
    product.order_limit = order_limit
    product.device_model = device_model
    product.save(
        update_fields=["product_type", "title", "slug", "status", "seo_title", "meta_description",
                       "main_image", "price", "off_price", "quantity", "discount_end_time",
                       "description", "order_limit", "device_model" ,"updated_at"])
    if categories is not None:
        set_product_categories(product=product, categories=categories)
    return product


def create_product_note(*, product: Product, title: str) -> ProductNote:
    return ProductNote.objects.create(
        product=product,
        title=title
    )


def update_product_note(*, product_note_id: int, title: str, product: Product) -> ProductNote:
    product_note = ProductNote.objects.get(id=product_note_id)
    product_note.product = product
    product_note.title = title
    product_note.save()
    return product_note


def delete_product_note(*, product_note_id: int) -> None:
    ProductNote.objects.get(id=product_note_id).delete()
