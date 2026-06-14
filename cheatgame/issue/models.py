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


class IssueReportStatus(IntEnum):

    DURING = 1
    DONE = 2
    CANCELED = 3
    IMPERFECT = 4


    @classmethod
    def choices(cls):
        return [(key.value ,key.name) for key in cls]




class Issue(BaseModel):
    picture = models.FileField()
    title = models.CharField(max_length=150)
    description = models.FileField()
    min_price = models.DecimalField(max_digits=15 , decimal_places=0 , default=0)
    max_price = models.DecimalField(max_digits=15 , decimal_places=0 , default=0)

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
    status = models.PositiveSmallIntegerField(choices=IssueReportStatus.choices() ,default=IssueReportStatus.IMPERFECT)
    public_tracking_code = models.CharField(max_length=16, unique=True, editable=False, blank=True)

    def save(self, *args, **kwargs):
        if not self.public_tracking_code:
            self.public_tracking_code = generate_repair_tracking_code()
        super().save(*args, **kwargs)


class IssueListReport(BaseModel):
    issue = models.ForeignKey("Issue" , on_delete=models.CASCADE ,)
    report = models.ForeignKey("IssueReport" ,on_delete=models.CASCADE , related_name="issue_list_report")


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
