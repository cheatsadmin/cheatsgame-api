from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill_repair_items(apps, schema_editor):
    IssueReport = apps.get_model("issue", "IssueReport")
    IssueListReport = apps.get_model("issue", "IssueListReport")
    RepairItem = apps.get_model("issue", "RepairItem")
    RepairItemIssue = apps.get_model("issue", "RepairItemIssue")

    for report in IssueReport.objects.all().iterator():
        if RepairItem.objects.filter(issue_report_id=report.id).exists():
            continue
        repair_item = RepairItem.objects.create(
            issue_report_id=report.id,
            item_type="legacy",
            model="",
            customer_note=report.explanation or "",
            sort_order=1,
        )
        item_issues = [
            RepairItemIssue(repair_item_id=repair_item.id, issue_id=issue_link.issue_id)
            for issue_link in IssueListReport.objects.filter(report_id=report.id).only("issue_id")
        ]
        RepairItemIssue.objects.bulk_create(item_issues, ignore_conflicts=True)


def reverse_backfill_repair_items(apps, schema_editor):
    RepairItem = apps.get_model("issue", "RepairItem")
    RepairItem.objects.filter(item_type="legacy").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("issue", "0015_issuereport_public_tracking_code"),
    ]

    operations = [
        migrations.CreateModel(
            name="RepairItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "item_type",
                    models.CharField(
                        choices=[
                            ("controller", "controller"),
                            ("console", "console"),
                            ("legacy", "legacy"),
                            ("unknown", "unknown"),
                        ],
                        default="unknown",
                        max_length=20,
                    ),
                ),
                ("model", models.CharField(blank=True, max_length=100)),
                ("customer_note", models.TextField(blank=True)),
                ("sort_order", models.PositiveIntegerField(default=1)),
                (
                    "issue_report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="issue.issuereport",
                    ),
                ),
            ],
            options={
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="RepairItemIssue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "issue",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="issue.issue"),
                ),
                (
                    "repair_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="item_issues",
                        to="issue.repairitem",
                    ),
                ),
            ],
            options={
                "unique_together": {("repair_item", "issue")},
                "abstract": False,
            },
        ),
        migrations.RunPython(backfill_repair_items, reverse_backfill_repair_items),
    ]
