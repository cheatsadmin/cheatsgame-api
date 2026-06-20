from typing import Optional

from django.db.models import QuerySet

from cheatgame.product.models import Category, ProductCategory, Product
from django.utils.text import slugify


def _normalize_slug(value: str) -> str:
    return slugify(value or "", allow_unicode=True)


def _make_auto_slug(*, name: str, parent: Optional[Category], category_id: Optional[int] = None) -> str:
    base_parts = []
    if parent and parent.slug:
        base_parts.append(parent.slug)
    base_parts.append(_normalize_slug(name) or "category")
    base_slug = "-".join(part for part in base_parts if part) or "category"
    candidate = base_slug
    counter = 2
    queryset = Category.objects.all()
    if category_id:
        queryset = queryset.exclude(id=category_id)
    while queryset.filter(slug=candidate).exists():
        candidate = f"{base_slug}-{counter}"
        counter += 1
    return candidate


def _validate_unique_slug(*, slug: str, category_id: Optional[int] = None) -> str:
    normalized_slug = _normalize_slug(slug)
    if not normalized_slug:
        raise ValueError("اسلاگ دسته‌بندی را وارد کنید.")
    queryset = Category.objects.filter(slug=normalized_slug)
    if category_id:
        queryset = queryset.exclude(id=category_id)
    if queryset.exists():
        raise ValueError("این اسلاگ قبلاً برای دسته‌بندی دیگری ثبت شده است.")
    return normalized_slug


def _validate_category_parent(
    *,
    parent: Optional[Category],
    category_type: int,
    category: Optional[Category] = None,
) -> None:
    if not parent:
        return
    if parent.category_type != category_type:
        raise ValueError("دسته‌بندی والد باید از همان نوع دسته‌بندی باشد.")
    if category:
        if parent.id == category.id:
            raise ValueError("یک دسته‌بندی نمی‌تواند والد خودش باشد.")
        if category.get_descendants().filter(id=parent.id).exists():
            raise ValueError("دسته‌بندی والد نمی‌تواند زیرمجموعه همین دسته‌بندی باشد.")


def _validate_unique_name_in_parent(
    *,
    name: str,
    category_type: int,
    parent: Optional[Category],
    category_id: Optional[int] = None,
) -> None:
    queryset = Category.objects.filter(
        name=name.strip(),
        category_type=category_type,
        parent=parent,
    )
    if category_id:
        queryset = queryset.exclude(id=category_id)
    if queryset.exists():
        raise ValueError("این عنوان قبلاً در همین سطح دسته‌بندی ثبت شده است.")


def create_category(*, name: str, category_type: int, parent: Category = None, slug: str = "") -> Category:
    _validate_category_parent(parent=parent, category_type=category_type)
    _validate_unique_name_in_parent(name=name, category_type=category_type, parent=parent)
    category_slug = _validate_unique_slug(slug=slug) if slug else _make_auto_slug(name=name, parent=parent)
    return Category.objects.create(
        name=name.strip(),
        slug=category_slug,
        category_type=category_type,
        parent=parent
    )


def update_category(*, category_id: int, name: str, category_type: int, parent: Category = None, slug: str = "") -> Category:
    category = Category.objects.get(id=category_id)
    if category.category_type != category_type and category.get_children().exists():
        raise ValueError("برای تغییر نوع دسته‌بندی ابتدا زیرمجموعه‌ها را جابه‌جا کنید.")
    _validate_category_parent(parent=parent, category_type=category_type, category=category)
    _validate_unique_name_in_parent(
        name=name,
        category_type=category_type,
        parent=parent,
        category_id=category.id,
    )
    category.name = name.strip()
    category.category_type = category_type
    category.parent = parent
    if slug:
        category.slug = _validate_unique_slug(slug=slug, category_id=category.id)
    category.save()
    return category


def delete_category(category_id: int) -> None:
    category = Category.objects.get(id=category_id)
    if category.get_children().exists():
        raise ValueError("ابتدا زیرمجموعه‌های این دسته‌بندی را حذف یا جابه‌جا کنید.")
    if ProductCategory.objects.filter(category=category).exists():
        raise ValueError("این دسته‌بندی به محصول متصل است و قابل حذف نیست. ابتدا دسته‌بندی محصولات را تغییر دهید.")
    category.delete()


def create_product_categories(*, product_category: list[ProductCategory]) -> QuerySet[ProductCategory]:
    return ProductCategory.objects.bulk_create(product_category)


def update_product_category(*, product_category_id: int, product: Product, category: Category) -> ProductCategory:
    product_category = ProductCategory.objects.get(id=product_category_id)
    product_category.product = product
    product_category.category = category
    product_category.save()
    return product_category


def delete_product_category(*, product_category_id) -> None:
    ProductCategory.objects.get(id=product_category_id).delete()
