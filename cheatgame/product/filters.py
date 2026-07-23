from django_filters import (
    CharFilter,
    FilterSet,

)
from django.contrib.postgres.search import SearchVector
from django.db import connection
from django.db.models import F
from django.utils import timezone
from cheatgame.product.models import Category, Product, ProductOrderBy, ProductStatus, Question, Reviews
from rest_framework.exceptions import APIException


class ProductFilter(FilterSet):
    product_type = CharFilter(method="filter_product_type")
    search = CharFilter(method="filter_search")
    off_price__range = CharFilter(method="filter_off_price__range")
    created_at__range = CharFilter(method="filter_created_at__range")
    has_discount = CharFilter(method="filter_has_discount")
    categories__in = CharFilter(method="filter_categories__in")
    labels__in = CharFilter(method="filter_labels__in")
    is_exists = CharFilter(method="filter_is_exists")
    order_by = CharFilter(method="filter_order_by")
    status = CharFilter(method="filter_status")
    visibility = CharFilter(method="filter_visibility")

    def filter_product_type(self, queryset, name, value):
        return queryset.filter(product_type=int(value))

    def filter_search(self, queryset, name, value):
        if connection.vendor != "postgresql":
            return queryset.filter(title__icontains=value)
        return queryset.annotate(search=SearchVector("title")).filter(search=value)

    def filter_categories__in(self, queryset, name, value):
        limit = 10
        raw_categories = [category for category in value.split(",") if category]
        if len(raw_categories) > limit:
            raise APIException(f"you cannot add more than {limit} categories")
        try:
            category_ids = [int(category) for category in raw_categories]
        except (TypeError, ValueError):
            raise APIException("شناسه دسته‌بندی نامعتبر است.")
        if not category_ids:
            return queryset
        selected_categories = Category.objects.filter(id__in=category_ids)
        expanded_category_ids = set()
        for category in selected_categories:
            expanded_category_ids.update(
                category.get_descendants(include_self=True).values_list("id", flat=True)
            )
        if not expanded_category_ids:
            return queryset.none()
        return queryset.filter(categories__category__in=expanded_category_ids).distinct()

    def filter_labels__in(self, queryset, name, value):
        limit = 10
        labels = value.split(",")
        if len(labels) > limit:
            raise APIException(f"you cannot add more than {len(labels)} labels")

        return queryset.filter(labels__label__in=labels)

    def filter_off_price__range(self, queryset, name, value):
        limit = 2
        off_price__in = value.split(",")
        if len(off_price__in) > limit:
            raise APIException("please just add two off_price with , in the middle")
        off_price_0, off_price_1 = off_price__in if len(off_price__in) else off_price__in, None
        if not off_price_1:
            return queryset.filter(off_price__gte=off_price_0[0])
        return queryset.filter(off_price__range=(off_price_0, off_price_1))

    def filter_created_at__range(self, queryset, name, value):
        print("create_at")
        limit = 2
        created_at__in = value.split(",")
        if len(created_at__in) > limit:
            raise APIException("please just add two created_at with , in the middle")
        created_at_0, created_at_1 = created_at__in

        if not created_at_1:
            created_at_1 = timezone.now()

        if not created_at_0:
            return queryset.filter(created_at__date__lt=created_at_1)

        return queryset.filter(created_at__range=(created_at_0, created_at_1))

    def filter_has_discount(self, queryset, name, value):
        if value == "True":
            return queryset.filter(
                price__gt=0,
                off_price__gt=0,
                off_price__lt=F("price"),
                discount_end_time__gt=timezone.now(),
            )
        return queryset.filter(discount_end_time__isnull=True)

    def filter_is_exists(self, queryset, name, value):
        if value == "True":
            return queryset.filter(quantity__gt=0)
        return queryset.filter(quantity__lte=0)

    def filter_status(self, queryset, name, value):
        valid_statuses = {choice[0] for choice in ProductStatus.choices}
        if value in valid_statuses:
            return queryset.filter(status=value)
        return queryset

    def filter_visibility(self, queryset, name, value):
        if value == "active":
            return queryset.exclude(status=ProductStatus.HIDDEN)
        if value == "hidden":
            return queryset.filter(status=ProductStatus.HIDDEN)
        return queryset

    def filter_order_by(self, queryset, name, value):
        value = int(value)
        if value == ProductOrderBy.EXPENSIVE:
            print('hello-price')
            return queryset.order_by("-off_price")
        elif value == ProductOrderBy.INEXPENSIVE:
            return queryset.order_by("off_price")
        elif value == ProductOrderBy.NEWEST:
            return queryset.order_by("-created_at")
        return queryset

    class Meta:
        model = Product
        fields = (
            "slug",
            "title",
            "off_price"
        )


class QuestionFilter(FilterSet):
    is_answered = CharFilter(method="filter_is_answered")

    def filter_is_answered(self , queryset , name , value):
        if value == "True":
            return queryset.filter(answer__isnull=False)
        elif value == "False":
            return queryset.filter(answer__isnull=True)
        else:
            return queryset.filter()

    class Meta:
        model = Question
        fields = (
            "id",
            "product",
            "sender",
            "answer"
        )


class ReviewFilter(FilterSet):
    is_accepted = CharFilter(method="filter_is_accepted")
    status = CharFilter(method="filter_status")

    def filter_is_accepted(self , queryset , name , value):
        if value in (True, "True", "true", "1"):
            return queryset.filter(accepted=True)
        elif value in (False, "False", "false", "0"):
            return queryset.filter(accepted=False)
        else:
            return queryset.filter()

    def filter_status(self, queryset, name, value):
        if value:
            return queryset.filter(status=value)
        return queryset

    class Meta:
        model = Reviews
        fields = (
            "id",
            "user",
            "product",
            "comment",
            "status",
        )
