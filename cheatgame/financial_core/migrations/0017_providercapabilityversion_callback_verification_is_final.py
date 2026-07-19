from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0016_provider_event_contradiction_lineage")]

    operations = [
        migrations.AddField(
            model_name="providercapabilityversion",
            name="callback_verification_is_final",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="providercapabilityversion",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(callback_verification_is_final=False)
                    | ~models.Q(callback_authentication="none")
                ),
                name="fin_cap_callback_final_authenticated",
            ),
        ),
    ]
