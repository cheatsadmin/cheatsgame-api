from typing import List

from django.db import transaction
from django.db.models import QuerySet

from cheatgame.issue.models import IssueReport, Issue, IssueListReport, IssueCategory, Tag, IssueReportStatus, IssueTag
from cheatgame.product.models import Category
from cheatgame.shop.models import DeliveryData
from cheatgame.shop.services.delivery_schedule import reserve_delivery_data
from cheatgame.users.models import BaseUser


@transaction.atomic
def create_issue_report(*, user: BaseUser, explanation: str, issue_list: List[Issue]) -> IssueReport:
    issue_report = IssueReport.objects.create(
        user=user,
        explanation=explanation
    )
    issue_report_list = []
    for issue in issue_list:
        issue_report_list.append(IssueListReport(issue=issue, report=issue_report))
    IssueListReport.objects.bulk_create(issue_report_list)
    issue_report = IssueReport.objects.prefetch_related('issue_list_report').get(id=issue_report.id)
    return issue_report


@transaction.atomic
def update_issue_report(*, issue_report_id: int, user: BaseUser, delivery_data: DeliveryData) -> IssueReport:
    issue_report = IssueReport.objects.select_for_update().get(id=issue_report_id, user=user)
    if issue_report.delivery_data_id is None:
        delivery_data = reserve_delivery_data(delivery_data=delivery_data)
    elif issue_report.delivery_data_id != delivery_data.id:
        raise ValueError("Issue report already has a reservation.")
    issue_report.delivery_data = delivery_data
    issue_report.status = IssueReportStatus.DURING
    issue_report.save(update_fields=['delivery_data'  , 'status' ])
    return issue_report


def create_issue(*, picture: str, title: str, description: str , min_price:str , max_price:str):
    return Issue.objects.create(
        picture=picture,
        title=title,
        description=description,
        min_price=min_price,
        max_price=max_price,
    )

def create_issue_categories(*, issue_category: list[IssueCategory]) -> QuerySet[IssueCategory]:
    return IssueCategory.objects.bulk_create(issue_category)


def create_issue_tags(* , issue_tag: list[IssueTag]) -> QuerySet[IssueTag]:
    return IssueTag.objects.bulk_create(issue_tag)



def update_issue_category(*, issue_category_id: int, issue: Issue, category: Category) -> IssueCategory:
    issue_category = IssueCategory.objects.get(id=issue_category_id)
    issue_category.issue =  issue
    issue_category.category = category
    issue_category.save()
    return issue_category


def update_issue_tag(* , issue_tag_id: int , issue: Issue , tag: Tag) -> IssueTag:
    issue_tag = IssueTag.objects.get(id=issue_tag_id)
    issue_tag.issue = issue
    issue_tag.tag = tag
    issue_tag.save()
    return issue_tag

def delete_issue_category(*, issue_category_id) -> None:
    IssueCategory.objects.get(id=issue_category_id).delete()
    

def delete_issue_tag(*, issue_tag_id) -> None:
    IssueTag.objects.get(id=issue_tag_id).delete()


def check_issue_exists(*, issue_id: int) -> bool:
    return Issue.objects.filter(id=issue_id).exists()

def get_issue(* , issue_id: int) -> Issue:
    return Issue.objects.get(id=issue_id)

def create_tag(* , title:str , issue_type:int) -> Tag:
    return Tag.objects.create(
        title = title,
        issue_type = issue_type
    )
def update_tag(* , tag_id: int , title:str , issue_type:int) -> Tag:
    issue_tag = Tag.objects.get(id=tag_id)
    issue_tag.title = title
    issue_tag.issue_type = issue_type
    issue_tag.save()
    return issue_tag

def delete_tag(* , tag_id: int) -> None:
    Tag.objects.get(id=tag_id).delete()
