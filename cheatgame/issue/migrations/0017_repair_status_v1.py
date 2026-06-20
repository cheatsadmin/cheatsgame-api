from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def migrate_old_repair_statuses(apps, schema_editor):
    IssueReport = apps.get_model("issue", "IssueReport")

    # Old values:
    # 1 DURING -> 1 SUBMITTED
    # 2 DONE -> 6 DELIVERED
    # 3 CANCELED -> 7 CANCELED
    # 4 IMPERFECT -> 1 SUBMITTED
    IssueReport.objects.filter(status=4).update(status=1)
    IssueReport.objects.filter(status=2).update(status=6)
    IssueReport.objects.filter(status=3).update(status=7)


def reverse_repair_statuses(apps, schema_editor):
    IssueReport = apps.get_model("issue", "IssueReport")

    # Collapse V1 statuses back to the old coarse lifecycle.
    IssueReport.objects.filter(status__in=[1, 2, 3, 4, 5]).update(status=1)
    IssueReport.objects.filter(status=6).update(status=2)
    IssueReport.objects.filter(status=7).update(status=3)


class Migration(migrations.Migration):

    dependencies = [
        ("issue", "0016_repairitem_repairitemissue"),
    ]

    operations = [
        migrations.AlterField(
            model_name="issuereport",
            name="status",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (1, "SUBMITTED"),
                    (2, "RECEIVED"),
                    (3, "INSPECTING"),
                    (4, "REPAIRING"),
                    (5, "READY_FOR_DELIVERY"),
                    (6, "DELIVERED"),
                    (7, "CANCELED"),
                ],
                default=1,
            ),
        ),
        migrations.RunPython(migrate_old_repair_statuses, reverse_repair_statuses),
        migrations.CreateModel(
            name="RepairStatusHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "old_status",
                    models.PositiveSmallIntegerField(
                        blank=True,
                        choices=[
                            (1, "SUBMITTED"),
                            (2, "RECEIVED"),
                            (3, "INSPECTING"),
                            (4, "REPAIRING"),
                            (5, "READY_FOR_DELIVERY"),
                            (6, "DELIVERED"),
                            (7, "CANCELED"),
                        ],
                        null=True,
                    ),
                ),
                (
                    "new_status",
                    models.PositiveSmallIntegerField(
                        choices=[
                            (1, "SUBMITTED"),
                            (2, "RECEIVED"),
                            (3, "INSPECTING"),
                            (4, "REPAIRING"),
                            (5, "READY_FOR_DELIVERY"),
                            (6, "DELIVERED"),
                            (7, "CANCELED"),
                        ],
                    ),
                ),
                ("note", models.TextField(blank=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="users.baseuser",
                    ),
                ),
                (
                    "issue_report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="status_history",
                        to="issue.issuereport",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-id"),
                "abstract": False,
            },
        ),
    ]
