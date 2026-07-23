from django_filters import (
    BooleanFilter,
    CharFilter,
    FilterSet,

)
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.db import connection
from django.utils import timezone

from rest_framework.exceptions import APIException

from cheatgame.issue.models import Issue


class IssueFilter(FilterSet):
    search = CharFilter(method="filter_search")
    created_at__range = CharFilter(method="filter_created_at__range")
    categories__in = CharFilter(method="filter_categories__in")
    tags__in = CharFilter(method="filter_tags__in")
    is_active = BooleanFilter(field_name="is_active")
    

    def filter_search(self, queryset, name, value):
        search_term = value.strip()
        if not search_term:
            return queryset
        if connection.vendor == "postgresql":
            return queryset.annotate(
                search=SearchVector("title")
            ).filter(search=SearchQuery(search_term))
        return queryset.filter(title__icontains=search_term)

    def filter_categories__in(self, queryset, name, value):
        limit = 10
        categories = value.split(",")
        if len(categories) > limit:
            raise APIException(f"you cannot add more than {len(categories)} categories")
        return queryset.filter(categories__category__in=categories).distinct()
    
    def filter_tags__in(self, queryset, name, value):
        limit = 10
        tags = value.split(",")
        if len(tags) > limit:
            raise APIException(f"you cannot add more than {len(tags)} labels")

        return queryset.filter(tags__tag__in=tags)


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
        model = Issue
        fields = (
            "title",
            "is_active",
        )
