from enum import IntEnum

from django.db import models

from cheatgame.common.models import BaseModel

REPAIR_TRACKING_CODE_PREFIX = "FX"
REPAIR_TRACKING_CODE_START = 1001

class IssueType(IntEnum):
    CONSOLE = 1
    CONTROLLER = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class RepairItemType(models.TextChoices):
    CONTROLLER = "controller", "controller"
    CONSOLE = "console", "console"
    LEGACY = "legacy", "legacy"
    UNKNOWN = "unknown", "unknown"


class IssueReportStatus(IntEnum):

    SUBMITTED = 1
    RECEIVED = 2
    INSPECTING = 3
    REPAIRING = 4
    READY_FOR_DELIVERY = 5
    DELIVERED = 6
    CANCELED = 7

    # Backward-compatible aliases for older code paths and tests.
    DURING = SUBMITTED
    IMPERFECT = SUBMITTED
    DONE = DELIVERED


    @classmethod
    def choices(cls):
        return [(key.value ,key.name) for key in cls]




class Issue(BaseModel):
    picture = models.FileField()
    title = models.CharField(max_length=150)
    description = models.FileField()
    min_price = models.DecimalField(max_digits=15 , decimal_places=0 , default=0)
    max_price = models.DecimalField(max_digits=15 , decimal_places=0 , default=0)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.title


class IssueCategory(BaseModel):
    issue = models.ForeignKey("Issue", on_delete=models.CASCADE ,related_name="categories")
    category = models.ForeignKey("product.Category", on_delete=models.CASCADE)


class IssueReport(BaseModel):
    user = models.ForeignKey("users.BaseUser", on_delete=models.CASCADE)
    delivery_data = models.ForeignKey("shop.DeliveryData", on_delete=models.CASCADE , null=True , blank=True)
    explanation = models.CharField(max_length=1000 , null=True , blank=True)
    is_paid = models.BooleanField(default=False)
    status = models.PositiveSmallIntegerField(choices=IssueReportStatus.choices() ,default=IssueReportStatus.SUBMITTED)
    public_tracking_code = models.CharField(max_length=16, unique=True, editable=False, blank=True)

    def save(self, *args, **kwargs):
        if not self.public_tracking_code:
            self.public_tracking_code = generate_repair_tracking_code()
        super().save(*args, **kwargs)


class IssueListReport(BaseModel):
    issue = models.ForeignKey("Issue" , on_delete=models.CASCADE ,)
    report = models.ForeignKey("IssueReport" ,on_delete=models.CASCADE , related_name="issue_list_report")


class RepairStatusHistory(BaseModel):
    issue_report = models.ForeignKey("IssueReport", on_delete=models.CASCADE, related_name="status_history")
    old_status = models.PositiveSmallIntegerField(choices=IssueReportStatus.choices(), null=True, blank=True)
    new_status = models.PositiveSmallIntegerField(choices=IssueReportStatus.choices())
    changed_by = models.ForeignKey("users.BaseUser", on_delete=models.SET_NULL, null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at", "-id")


class RepairItem(BaseModel):
    issue_report = models.ForeignKey("IssueReport", on_delete=models.CASCADE, related_name="items")
    item_type = models.CharField(
        max_length=20,
        choices=RepairItemType.choices,
        default=RepairItemType.UNKNOWN,
    )
    model = models.CharField(max_length=100, blank=True)
    customer_note = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ("sort_order", "id")

    def __str__(self):
        return f"{self.issue_report.public_tracking_code} - {self.item_type} - {self.model}".strip()


class RepairItemIssue(BaseModel):
    repair_item = models.ForeignKey("RepairItem", on_delete=models.CASCADE, related_name="item_issues")
    issue = models.ForeignKey("Issue", on_delete=models.CASCADE)

    class Meta:
        unique_together = ("repair_item", "issue")


def generate_repair_tracking_code() -> str:
    existing_codes = IssueReport.objects.filter(
        public_tracking_code__startswith=f"{REPAIR_TRACKING_CODE_PREFIX}-"
    ).values_list("public_tracking_code", flat=True)
    max_number = REPAIR_TRACKING_CODE_START - 1
    for code in existing_codes:
        try:
            max_number = max(max_number, int(str(code).split("-", 1)[1]))
        except (IndexError, TypeError, ValueError):
            continue

    next_number = max_number + 1
    while True:
        code = f"{REPAIR_TRACKING_CODE_PREFIX}-{next_number}"
        if not IssueReport.objects.filter(public_tracking_code=code).exists():
            return code
        next_number += 1

class IssueTag(BaseModel):
    issue = models.ForeignKey("Issue", on_delete=models.CASCADE , related_name="tags")
    tag = models.ForeignKey("Tag", on_delete=models.CASCADE)


class Tag(BaseModel):
    title = models.CharField(max_length=150)
    issue_type = models.IntegerField(choices=IssueType.choices())
    def __str__(self):
        return self.title
