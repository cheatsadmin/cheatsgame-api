from django.db import migrations, models
import django.db.models.deletion


def create_contradiction_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_provider_event_contradiction()
        RETURNS trigger AS $$
        DECLARE original financial_core_providerevent%%ROWTYPE;
        BEGIN
            IF NEW.resolution_status <> 'contradictory' THEN
                IF NEW.original_event_id IS NOT NULL THEN
                    RAISE EXCEPTION 'Only contradictory provider events may reference an original event'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.original_event_id IS NULL THEN
                RAISE EXCEPTION 'Contradictory provider events require original evidence'
                    USING ERRCODE = '23514';
            END IF;

            SELECT * INTO STRICT original
            FROM financial_core_providerevent
            WHERE id = NEW.original_event_id;

            IF original.resolution_status = 'contradictory'
               OR original.provider_id <> NEW.provider_id
               OR original.capability_version_id <> NEW.capability_version_id
               OR original.merchant_account_version_id <> NEW.merchant_account_version_id
               OR original.provider_event_id = ''
               OR original.provider_event_id <> NEW.provider_event_id
            THEN
                RAISE EXCEPTION 'Contradictory provider-event lineage is inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            IF ROW(
                original.canonical_envelope_hash,
                original.merchant_reference,
                original.provider_authority,
                original.provider_reference,
                original.operation_type_hint,
                original.provider_amount_hint,
                original.provider_unit_hint,
                original.normalized_hint,
                original.provider_occurred_at
            ) IS NOT DISTINCT FROM ROW(
                NEW.canonical_envelope_hash,
                NEW.merchant_reference,
                NEW.provider_authority,
                NEW.provider_reference,
                NEW.operation_type_hint,
                NEW.provider_amount_hint,
                NEW.provider_unit_hint,
                NEW.normalized_hint,
                NEW.provider_occurred_at
            ) THEN
                IF NOT EXISTS (
                    SELECT 1
                    FROM financial_core_callbackreceipt new_receipt
                    JOIN financial_core_providereventreceipt original_link
                      ON original_link.provider_event_id = original.id
                    JOIN financial_core_callbackreceipt original_receipt
                      ON original_receipt.id = original_link.callback_receipt_id
                    WHERE new_receipt.correlation_id = NEW.correlation_id
                      AND (
                          new_receipt.authentication_version
                              IS DISTINCT FROM original_receipt.authentication_version
                          OR new_receipt.signing_key_reference_hash
                              IS DISTINCT FROM original_receipt.signing_key_reference_hash
                      )
                ) THEN
                    RAISE EXCEPTION 'Contradictory provider events require distinct evidence'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_provider_event_contradiction_valid
        BEFORE INSERT ON financial_core_providerevent
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_provider_event_contradiction();
        """
    )


def drop_contradiction_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_provider_event_contradiction_valid "
        "ON financial_core_providerevent;"
    )
    schema_editor.execute(
        "DROP FUNCTION IF EXISTS financial_core_validate_provider_event_contradiction();"
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0015_postgresql_finalized_projection_guards")]

    operations = [
        migrations.AddField(
            model_name="providerevent",
            name="original_event",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="contradictory_events",
                to="financial_core.providerevent",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerevent",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        resolution_status="contradictory",
                        original_event__isnull=False,
                    )
                    | (
                        ~models.Q(resolution_status="contradictory")
                        & models.Q(original_event__isnull=True)
                    )
                ),
                name="fin_provider_event_contradiction_origin",
            ),
        ),
        migrations.RunPython(create_contradiction_guard, drop_contradiction_guard),
    ]
