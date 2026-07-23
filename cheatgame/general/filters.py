from django_filters import (
    CharFilter,
    FilterSet,

)
from django.contrib.postgres.search import SearchVector
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from cheatgame.general.models import Blog
from rest_framework.exceptions import APIException


class BlogFilter(FilterSet):
    search = CharFilter(method="filter_search")
    created_at__range = CharFilter(method="filter_created_at__range")
    categories__in = CharFilter(method="filter_categories__in")
    status = CharFilter(field_name="status")

    def filter_search(self, queryset, name, value):
        if not value:
            return queryset
        if connection.vendor != "postgresql":
            return queryset.filter(Q(title__icontains=value) | Q(slug__icontains=value))
        return queryset.annotate(search=SearchVector("title", "slug")).filter(search=value)

    def filter_categories__in(self, queryset, name, value):
        limit = 10
        categories = value.split(",")
        if len(categories) > limit:
            raise APIException(f"you cannot add more than {len(categories)} categories")
        return queryset.filter(categories__category__in=categories).distinct()


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

    class Meta:
        model = Blog
        fields = (
            "slug",
            "title",
        )
