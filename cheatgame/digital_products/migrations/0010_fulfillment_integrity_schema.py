from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0009_installedgamerecord_digital_one_current_purchased_evidence"),
    ]

    operations = [
        migrations.RemoveConstraint("entitlement", "digital_entitlement_status_valid"),
        migrations.RemoveConstraint("entitlement", "digital_entitlement_lifecycle_fields"),
        migrations.RemoveConstraint("installedgamerecord", "digital_installed_correction_fields"),
        migrations.RemoveConstraint("installedgamerecord", "digital_one_current_purchased_evidence"),
        migrations.RemoveConstraint("installedgamerecord", "digital_installed_state_valid"),
        migrations.RemoveField("entitlement", "end_reason"),
        migrations.RemoveField("entitlement", "ended_at"),
        migrations.AddField(
            "fulfillmentactivity", "actor_authority",
            models.CharField(
                choices=[
                    ("system", "SYSTEM"), ("customer_owner", "CUSTOMER_OWNER"),
                    ("assigned_operator", "ASSIGNED_OPERATOR"),
                    ("unassigned_staff", "UNASSIGNED_STAFF"),
                    ("admin_override", "ADMIN_OVERRIDE"),
                ],
                default="system", max_length=24,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            "fulfillmentactivity", "request_fingerprint",
            models.CharField(default="migration-only", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            "installedgamerecord", "actor_authority",
            models.CharField(
                choices=[
                    ("system", "SYSTEM"), ("customer_owner", "CUSTOMER_OWNER"),
                    ("assigned_operator", "ASSIGNED_OPERATOR"),
                    ("unassigned_staff", "UNASSIGNED_STAFF"),
                    ("admin_override", "ADMIN_OVERRIDE"),
                ],
                default="unassigned_staff", max_length=24,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            "installedgamerecord", "request_fingerprint",
            models.CharField(default="migration-only", max_length=64),
            preserve_default=False,
        ),
        migrations.AlterField(
            "entitlement", "status",
            models.CharField(
                choices=[("pending_fulfillment", "PENDING_FULFILLMENT"), ("active", "ACTIVE")],
                default="pending_fulfillment", max_length=24,
            ),
        ),
        migrations.AlterField(
            "installedgamerecord", "state",
            models.CharField(
                choices=[("recorded", "RECORDED"), ("removed", "REMOVED")],
                default="recorded", max_length=16,
            ),
        ),
        migrations.AlterField(
            "installedgamerecord", "corrects",
            models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_by", to="digital_products.installedgamerecord",
            ),
        ),
        migrations.AddConstraint(
            "entitlement",
            models.CheckConstraint(
                check=models.Q(status__in=["pending_fulfillment", "active"]),
                name="digital_entitlement_status_valid",
            ),
        ),
        migrations.AddConstraint(
            "entitlement",
            models.CheckConstraint(
                check=(
                    models.Q(status="pending_fulfillment", activated_at__isnull=True)
                    | models.Q(status="active", activated_at__isnull=False)
                ),
                name="digital_entitlement_lifecycle_fields",
            ),
        ),
        migrations.AddConstraint(
            "fulfillmentactivity",
            models.CheckConstraint(
                check=models.Q(actor_authority__in=[
                    "system", "customer_owner", "assigned_operator", "unassigned_staff", "admin_override",
                ]),
                name="digital_activity_authority_valid",
            ),
        ),
        migrations.AddConstraint(
            "fulfillmentactivity",
            models.UniqueConstraint(
                fields=("fulfillment_item",),
                condition=models.Q(activity_type="provisioned"),
                name="digital_one_provisioned_activity",
            ),
        ),
        migrations.AddConstraint(
            "fulfillmentactivity",
            models.CheckConstraint(
                check=~models.Q(request_fingerprint=""),
                name="digital_activity_fingerprint_nonempty",
            ),
        ),
        migrations.AddConstraint(
            "installedgamerecord",
            models.CheckConstraint(
                check=models.Q(state__in=["recorded", "removed"]),
                name="digital_installed_state_valid",
            ),
        ),
        migrations.AddConstraint(
            "installedgamerecord",
            models.CheckConstraint(
                check=(
                    models.Q(corrects__isnull=True, correction_reason="")
                    | (models.Q(corrects__isnull=False) & ~models.Q(correction_reason=""))
                ),
                name="digital_installed_correction_fields",
            ),
        ),
        migrations.AddConstraint(
            "installedgamerecord",
            models.CheckConstraint(
                check=~models.Q(request_fingerprint=""),
                name="digital_installed_fingerprint_nonempty",
            ),
        ),
        migrations.AddConstraint(
            "installedgamerecord",
            models.CheckConstraint(
                check=models.Q(actor_authority__in=[
                    "system", "customer_owner", "assigned_operator", "unassigned_staff", "admin_override",
                ]),
                name="digital_installed_authority_valid",
            ),
        ),
    ]
