from django.contrib import admin

from cheatgame.issue.models import Issue, IssueCategory, IssueReport, Tag, IssueTag, RepairItem, RepairItemIssue


@admin.register(Issue)
class ImageAdmin(admin.ModelAdmin):
    fields = ("title", "picture", "description", "min_price", "max_price", "is_active", "sort_order")
    search_fields = ("title",)
    list_display = (
        "title",
        "picture",
        "is_active",
        "sort_order",
    )
    list_filter = ("is_active",)


@admin.register(IssueCategory)
class IssueCategoryAdmin(admin.ModelAdmin):
    fields = ("issue", "category")
    list_display = ("issue", "category")


@admin.register(IssueReport)
class IssueReportAdmin(admin.ModelAdmin):
    fields = ("user", "delivery_data", "is_paid")
    list_display = ("user", "delivery_data", "is_paid")


@admin.register(RepairItem)
class RepairItemAdmin(admin.ModelAdmin):
    fields = ("issue_report", "item_type", "model", "customer_note", "sort_order")
    list_display = ("issue_report", "item_type", "model", "sort_order")
    list_filter = ("item_type",)
    search_fields = ("issue_report__public_tracking_code", "model", "customer_note")


@admin.register(RepairItemIssue)
class RepairItemIssueAdmin(admin.ModelAdmin):
    fields = ("repair_item", "issue")
    list_display = ("repair_item", "issue")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    fields = ("title", "issue_type")
    list_display = ("title", "issue_type")


@admin.register(IssueTag)
class IssueTagAdmin(admin.ModelAdmin):
    fields = ("issue", "tag")
