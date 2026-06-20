from typing import List, Optional

from django.db import transaction
from django.db.models import QuerySet

from cheatgame.issue.models import (
    IssueReport,
    Issue,
    IssueListReport,
    IssueCategory,
    Tag,
    IssueReportStatus,
    IssueTag,
    RepairItem,
    RepairItemIssue,
    RepairItemType,
)
from cheatgame.product.models import Category
from cheatgame.shop.models import DeliveryData
from cheatgame.shop.services.delivery_schedule import reserve_delivery_data
from cheatgame.users.models import BaseUser


def _safe_repair_item_type(item_type: Optional[str]) -> str:
    if item_type in RepairItemType.values:
        return item_type
    return RepairItemType.UNKNOWN


def _create_repair_item(*, issue_report: IssueReport, item_data: dict, sort_order: int) -> RepairItem:
    issues = list(item_data.get("issue_ids") or item_data.get("issues") or [])
    repair_item = RepairItem.objects.create(
        issue_report=issue_report,
        item_type=_safe_repair_item_type(item_data.get("item_type")),
        model=item_data.get("model") or "",
        customer_note=item_data.get("customer_note") or "",
        sort_order=item_data.get("sort_order") or sort_order,
    )
    RepairItemIssue.objects.bulk_create(
        [RepairItemIssue(repair_item=repair_item, issue=issue) for issue in issues],
        ignore_conflicts=True,
    )
    return repair_item


@transaction.atomic
def create_issue_report(
    *,
    user: BaseUser,
    explanation: Optional[str] = None,
    issue_list: Optional[List[Issue]] = None,
    items: Optional[List[dict]] = None,
    overall_explanation: Optional[str] = None,
) -> IssueReport:
    issue_list = list(issue_list or [])
    items = list(items or [])
    report_explanation = overall_explanation if overall_explanation is not None else explanation
    issue_report = IssueReport.objects.create(
        user=user,
        explanation=report_explanation
    )
    if items:
        mirrored_issues = []
        for index, item_data in enumerate(items, start=1):
            _create_repair_item(issue_report=issue_report, item_data=item_data, sort_order=index)
            mirrored_issues.extend(item_data.get("issue_ids") or item_data.get("issues") or [])
    else:
        mirrored_issues = issue_list
        _create_repair_item(
            issue_report=issue_report,
            item_data={
                "item_type": RepairItemType.LEGACY,
                "model": "",
                "customer_note": explanation or "",
                "issue_ids": issue_list,
                "sort_order": 1,
            },
            sort_order=1,
        )

    IssueListReport.objects.bulk_create(
        [IssueListReport(issue=issue, report=issue_report) for issue in mirrored_issues]
    )
    issue_report = IssueReport.objects.select_related("user").prefetch_related(
        "issue_list_report__issue",
        "items__item_issues__issue",
    ).get(id=issue_report.id)
    return issue_report


@transaction.atomic
def update_issue_report(*, issue_report_id: int, user: BaseUser, delivery_data: DeliveryData) -> IssueReport:
    issue_report = IssueReport.objects.select_for_update().get(id=issue_report_id, user=user)
    if issue_report.delivery_data_id is None:
        if delivery_data.schedule_id is not None:
            delivery_data = reserve_delivery_data(delivery_data=delivery_data)
    elif issue_report.delivery_data_id != delivery_data.id:
        raise ValueError("Issue report already has a reservation.")
    issue_report.delivery_data = delivery_data
    issue_report.status = IssueReportStatus.SUBMITTED
    issue_report.save(update_fields=['delivery_data'  , 'status' ])
    return issue_report


def create_issue(
    *,
    picture: str,
    title: str,
    description: str,
    min_price: str,
    max_price: str,
    is_active: bool = True,
    sort_order: int = 0,
):
    return Issue.objects.create(
        picture=picture,
        title=title,
        description=description,
        min_price=min_price,
        max_price=max_price,
        is_active=is_active,
        sort_order=sort_order,
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
