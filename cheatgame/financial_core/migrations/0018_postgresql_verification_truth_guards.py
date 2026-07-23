from django.db import migrations, models


def create_verification_truth_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_verification_insert()
        RETURNS trigger AS $$
        DECLARE
            claim_tx bigint;
            claim_work bigint;
            work_tx bigint;
            work_event bigint;
            tx financial_core_paymenttransaction%%ROWTYPE;
            cap financial_core_providercapabilityversion%%ROWTYPE;
            event financial_core_providerevent%%ROWTYPE;
            expected_sequence integer;
        BEGIN
            SELECT transaction_id, work_item_id INTO STRICT claim_tx, claim_work
            FROM financial_core_verificationclaim WHERE id = NEW.claim_id;
            SELECT transaction_id, provider_event_id INTO STRICT work_tx, work_event
            FROM financial_core_verificationworkitem WHERE id = NEW.work_item_id;
            SELECT * INTO STRICT tx
            FROM financial_core_paymenttransaction WHERE id = NEW.transaction_id;
            SELECT * INTO STRICT cap
            FROM financial_core_providercapabilityversion WHERE id = tx.capability_version_id;

            IF claim_tx <> NEW.transaction_id
               OR claim_work <> NEW.work_item_id
               OR work_tx <> NEW.transaction_id
               OR work_event IS DISTINCT FROM NEW.provider_event_id
            THEN
                RAISE EXCEPTION 'Verification claim/work/transaction lineage is inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.provider_id <> cap.provider_id
               OR NEW.capability_version_id <> tx.capability_version_id
               OR NEW.merchant_account_version_id <> tx.merchant_account_version_id
               OR NEW.adapter_contract_version <> tx.adapter_contract_version
               OR NEW.operation_type <> tx.operation_type
               OR NEW.requested_provider_amount <> tx.provider_amount
               OR NEW.requested_provider_unit <> tx.provider_unit
               OR NEW.canonical_allocation_amount <> tx.amount
               OR NEW.canonical_currency <> tx.currency
            THEN
                RAISE EXCEPTION 'Verification frozen provider-policy lineage is inconsistent'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.provider_event_id IS NOT NULL THEN
                SELECT * INTO STRICT event
                FROM financial_core_providerevent WHERE id = NEW.provider_event_id;
                IF event.transaction_id IS DISTINCT FROM NEW.transaction_id
                   OR event.provider_id <> NEW.provider_id
                   OR event.capability_version_id <> NEW.capability_version_id
                   OR event.merchant_account_version_id <> NEW.merchant_account_version_id
                THEN
                    RAISE EXCEPTION 'Verification provider-event lineage is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            SELECT COALESCE(MAX(sequence), 0) + 1 INTO expected_sequence
            FROM financial_core_verification WHERE transaction_id = NEW.transaction_id;
            IF NEW.sequence <> expected_sequence THEN
                RAISE EXCEPTION 'Verification sequence is not the next transaction observation'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.application_state = 'applied_blocking_success' THEN
                IF NEW.normalized_outcome <> 'confirmed_success'
                   OR NEW.normalized_financial_effect <> 'paid'
                   OR NEW.finality <> 'final'
                   OR NEW.merchant_reference <> tx.merchant_reference
                   OR NEW.observed_provider_amount IS DISTINCT FROM tx.provider_amount
                   OR NEW.observed_provider_unit <> tx.provider_unit
                   OR NEW.provider_reference = ''
                   OR NEW.evidence_basis NOT IN ('server_to_server', 'authenticated_settlement')
                THEN
                    RAISE EXCEPTION 'Recognition-eligible Verification evidence is incomplete or contradictory'
                        USING ERRCODE = '23514';
                END IF;

                IF NEW.evidence_basis = 'authenticated_settlement' THEN
                    IF NEW.provider_event_id IS NULL
                       OR event.provider_event_id = ''
                       OR event.merchant_reference <> tx.merchant_reference
                       OR event.provider_reference <> NEW.provider_reference
                       OR event.operation_type_hint <> tx.operation_type
                       OR event.provider_amount_hint IS DISTINCT FROM tx.provider_amount
                       OR event.provider_unit_hint <> tx.provider_unit
                       OR event.financial_effect_hint <> 'paid'
                       OR event.finality_hint <> 'final'
                       OR event.provider_occurred_at IS NULL
                       OR NOT cap.callback_verification_is_final
                       OR NOT EXISTS (
                            SELECT 1
                            FROM financial_core_providereventreceipt link
                            JOIN financial_core_callbackreceipt receipt
                              ON receipt.id = link.callback_receipt_id
                            JOIN financial_core_merchantaccountversion account
                              ON account.id = NEW.merchant_account_version_id
                            WHERE link.provider_event_id = NEW.provider_event_id
                              AND receipt.authentication_status = 'authenticated'
                              AND receipt.authentication_strength = cap.callback_authentication
                              AND receipt.authentication_method = cap.callback_authentication_method
                              AND receipt.authentication_version = cap.callback_authentication_version
                              AND receipt.signing_key_reference_hash = account.callback_signing_key_reference_hash
                              AND receipt.replay_window_status = 'valid'
                       )
                    THEN
                        RAISE EXCEPTION 'Callback-final Verification lacks complete frozen authority evidence'
                            USING ERRCODE = '23514';
                    END IF;
                END IF;
            ELSIF NEW.normalized_outcome = 'confirmed_success' THEN
                RAISE EXCEPTION 'Confirmed success must use the recognition-blocking evidence state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_verification_insert_valid
        BEFORE INSERT ON financial_core_verification
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_verification_insert();

        CREATE OR REPLACE FUNCTION financial_core_validate_verification_reference_ownership()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.application_state = 'applied_blocking_success'
               AND NOT EXISTS (
                    SELECT 1 FROM financial_core_providerreferenceallocation ownership
                    WHERE ownership.transaction_id = NEW.transaction_id
                      AND ownership.merchant_account_version_id = NEW.merchant_account_version_id
                      AND ownership.provider_reference = NEW.provider_reference
                      AND ownership.verification_id = NEW.id
               )
            THEN
                RAISE EXCEPTION 'Successful Verification requires coherent provider-reference ownership'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER financial_core_verification_reference_valid
        AFTER INSERT ON financial_core_verification
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_verification_reference_ownership();

        CREATE OR REPLACE FUNCTION financial_core_validate_provider_reference_allocation_insert()
        RETURNS trigger AS $$
        DECLARE
            verification financial_core_verification%%ROWTYPE;
            tx financial_core_paymenttransaction%%ROWTYPE;
        BEGIN
            SELECT * INTO STRICT verification
            FROM financial_core_verification WHERE id = NEW.verification_id;
            SELECT * INTO STRICT tx
            FROM financial_core_paymenttransaction WHERE id = NEW.transaction_id;

            IF verification.transaction_id <> NEW.transaction_id
               OR verification.merchant_account_version_id <> NEW.merchant_account_version_id
               OR tx.merchant_account_version_id <> NEW.merchant_account_version_id
               OR verification.provider_reference <> NEW.provider_reference
               OR verification.normalized_outcome <> 'confirmed_success'
               OR verification.normalized_financial_effect <> 'paid'
               OR verification.finality <> 'final'
               OR verification.application_state <> 'applied_blocking_success'
               OR verification.evidence_basis NOT IN ('server_to_server', 'authenticated_settlement')
            THEN
                RAISE EXCEPTION 'Provider-reference allocation ownership is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_provider_reference_allocation_insert_valid
        BEFORE INSERT ON financial_core_providerreferenceallocation
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_provider_reference_allocation_insert();
        """
    )
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
            SELECT * INTO STRICT original FROM financial_core_providerevent
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
            IF ROW(original.canonical_envelope_hash, original.merchant_reference,
                   original.provider_authority, original.provider_reference,
                   original.operation_type_hint, original.provider_amount_hint,
                   original.provider_unit_hint, original.normalized_hint,
                   original.provider_occurred_at, original.financial_effect_hint,
                   original.finality_hint)
               IS NOT DISTINCT FROM
               ROW(NEW.canonical_envelope_hash, NEW.merchant_reference,
                   NEW.provider_authority, NEW.provider_reference,
                   NEW.operation_type_hint, NEW.provider_amount_hint,
                   NEW.provider_unit_hint, NEW.normalized_hint,
                   NEW.provider_occurred_at, NEW.financial_effect_hint,
                   NEW.finality_hint)
            THEN
                IF NOT EXISTS (
                    SELECT 1 FROM financial_core_callbackreceipt new_receipt
                    JOIN financial_core_providereventreceipt original_link
                      ON original_link.provider_event_id = original.id
                    JOIN financial_core_callbackreceipt original_receipt
                      ON original_receipt.id = original_link.callback_receipt_id
                    WHERE new_receipt.correlation_id = NEW.correlation_id
                      AND (new_receipt.authentication_version
                              IS DISTINCT FROM original_receipt.authentication_version
                           OR new_receipt.signing_key_reference_hash
                              IS DISTINCT FROM original_receipt.signing_key_reference_hash)
                ) THEN
                    RAISE EXCEPTION 'Contradictory provider events require distinct evidence'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def restore_0017_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS financial_core_verification_reference_valid
            ON financial_core_verification;
        DROP TRIGGER IF EXISTS financial_core_verification_insert_valid
            ON financial_core_verification;
        DROP TRIGGER IF EXISTS financial_core_provider_reference_allocation_insert_valid
            ON financial_core_providerreferenceallocation;
        DROP FUNCTION IF EXISTS financial_core_validate_provider_reference_allocation_insert();
        DROP FUNCTION IF EXISTS financial_core_validate_verification_reference_ownership();
        DROP FUNCTION IF EXISTS financial_core_validate_verification_insert();
        """
    )
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
            SELECT * INTO STRICT original FROM financial_core_providerevent
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
            IF ROW(original.canonical_envelope_hash, original.merchant_reference,
                   original.provider_authority, original.provider_reference,
                   original.operation_type_hint, original.provider_amount_hint,
                   original.provider_unit_hint, original.normalized_hint,
                   original.provider_occurred_at)
               IS NOT DISTINCT FROM
               ROW(NEW.canonical_envelope_hash, NEW.merchant_reference,
                   NEW.provider_authority, NEW.provider_reference,
                   NEW.operation_type_hint, NEW.provider_amount_hint,
                   NEW.provider_unit_hint, NEW.normalized_hint,
                   NEW.provider_occurred_at)
            THEN
                IF NOT EXISTS (
                    SELECT 1 FROM financial_core_callbackreceipt new_receipt
                    JOIN financial_core_providereventreceipt original_link
                      ON original_link.provider_event_id = original.id
                    JOIN financial_core_callbackreceipt original_receipt
                      ON original_receipt.id = original_link.callback_receipt_id
                    WHERE new_receipt.correlation_id = NEW.correlation_id
                      AND (new_receipt.authentication_version
                              IS DISTINCT FROM original_receipt.authentication_version
                           OR new_receipt.signing_key_reference_hash
                              IS DISTINCT FROM original_receipt.signing_key_reference_hash)
                ) THEN
                    RAISE EXCEPTION 'Contradictory provider events require distinct evidence'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0017_providercapabilityversion_callback_verification_is_final")]

    operations = [
        migrations.AddField(
            model_name="providercapabilityversion",
            name="callback_authentication_method",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="providercapabilityversion",
            name="callback_authentication_version",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="merchantaccountversion",
            name="callback_signing_key_reference_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="providerevent",
            name="financial_effect_hint",
            field=models.CharField(
                choices=[("paid", "PAID"), ("unpaid", "UNPAID"), ("none", "NONE"), ("unknown", "UNKNOWN")],
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="providerevent",
            name="finality_hint",
            field=models.CharField(
                choices=[("final", "FINAL"), ("non_final", "NON_FINAL"), ("unknown", "UNKNOWN")],
                default="unknown",
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="providercapabilityversion",
            name="fin_cap_callback_final_authenticated",
        ),
        migrations.AddConstraint(
            model_name="providercapabilityversion",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(callback_verification_is_final=False)
                    | (
                        ~models.Q(callback_authentication="none")
                        & ~models.Q(callback_authentication_method="")
                        & ~models.Q(callback_authentication_version="")
                    )
                ),
                name="fin_cap_callback_final_authenticated",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerevent",
            constraint=models.CheckConstraint(
                check=models.Q(financial_effect_hint__in=["paid", "unpaid", "none", "unknown"]),
                name="fin_provider_event_effect_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerevent",
            constraint=models.CheckConstraint(
                check=models.Q(finality_hint__in=["final", "non_final", "unknown"]),
                name="fin_provider_event_finality_valid",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="verification",
            name="fin_ver_success_evidence_complete",
        ),
        migrations.AddConstraint(
            model_name="verification",
            constraint=models.CheckConstraint(
                check=(
                    models.Q(
                        normalized_outcome="confirmed_success",
                        normalized_financial_effect="paid",
                        finality="final",
                        application_state="applied_blocking_success",
                        evidence_basis__in=("server_to_server", "authenticated_settlement"),
                        observed_provider_amount__isnull=False,
                    )
                    & ~models.Q(provider_reference="")
                    | ~models.Q(normalized_outcome="confirmed_success")
                ),
                name="fin_ver_success_evidence_complete",
            ),
        ),
        migrations.RunPython(create_verification_truth_guards, restore_0017_guards),
    ]
