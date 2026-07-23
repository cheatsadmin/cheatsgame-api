from django.db.models import QuerySet

from cheatgame.issue.filter import IssueReportFilter
from cheatgame.issue.filters import IssueFilter
from cheatgame.issue.models import Issue, Tag, IssueReport
from cheatgame.users.models import BaseUser


def issue_list(*, filters=None) -> QuerySet[Issue]:
    filters = filters or {}
    qs = Issue.objects.prefetch_related("tags__tag").order_by("sort_order", "id")
    return IssueFilter(filters, qs).qs


def get_tag_list(*, issue_type) -> QuerySet[Tag]:
    return Tag.objects.filter(issue_type=issue_type)

def issue_report_user(* , user: BaseUser) -> QuerySet[IssueReport]:
    return IssueReport.objects.filter(user=user).select_related("user").prefetch_related(
        "items__item_issues__issue",
        "issue_list_report__issue",
    )



def get_tag_list_of_issue(* , issue_id:int)-> QuerySet[Tag]:
    return Tag.objects.filter(issue_id=issue_id)
def issue_report_list(* , filters=None) -> QuerySet[IssueReport]:
    filters = filters or {}
    qs = IssueReport.objects.select_related(
        "user",
        "delivery_data__type",
        "delivery_data__schedule",
        "delivery_data__address",
    ).prefetch_related(
        "items__item_issues__issue",
        "issue_list_report__issue",
    )
    return IssueReportFilter(filters , qs).qs
