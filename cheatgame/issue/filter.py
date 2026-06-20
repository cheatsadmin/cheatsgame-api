from django.utils import timezone
from django_filters import CharFilter, FilterSet
from rest_framework.exceptions import APIException

from cheatgame.issue.models import IssueReport
from cheatgame.users.models import BaseUser


class IssueReportFilter(FilterSet):
    created_at__range = CharFilter(method="filter_created_at__range")
    user__phone_number = CharFilter(method="filter_user__phone_number")
    status = CharFilter(method="filter_status")




    def filter_created_at__range(self, queryset, name, value):
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

    def filter_user__phone_number(self, queryset, name, value):
        queryset = queryset.filter(user__phone_number=value)
        return queryset

    def filter_status(self, queryset, name, value):
        return queryset.filter(status=value)





    class Meta:
        model = IssueReport
        fields = (
            "created_at",
            "id",
            "user"
        )
