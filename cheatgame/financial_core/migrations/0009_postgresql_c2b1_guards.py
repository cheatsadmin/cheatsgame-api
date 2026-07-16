from django.db import migrations


APPEND_ONLY_TABLES = (
    "financial_core_callbackreceipt",
    "financial_core_providerevent",
    "financial_core_providereventreceipt",
    "financial_core_verificationclaim",
    "financial_core_verification",
    "financial_core_providerreferenceallocation",
)


def create_c2b1_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in APPEND_ONLY_TABLES:
        schema_editor.execute(
            f"""
            CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();
            """
        )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_verification_work()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Verification work history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.transaction_id, OLD.provider_event_id, OLD.work_type,
                   OLD.deterministic_identity, OLD.max_attempts, OLD.correlation_id, OLD.causation_id)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.transaction_id, NEW.provider_event_id, NEW.work_type,
                   NEW.deterministic_identity, NEW.max_attempts, NEW.correlation_id, NEW.causation_id)
            THEN
                RAISE EXCEPTION 'Verification work identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('completed', 'canceled') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'Terminal verification work cannot be reopened' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_verification_work_protected
        BEFORE UPDATE OR DELETE ON financial_core_verificationworkitem
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_verification_work();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_c2b1_attempt_evidence_guard()
        RETURNS trigger AS $$
        DECLARE target_payment_id bigint;
        BEGIN
            target_payment_id := NEW.payment_id;
            IF TG_OP = 'INSERT' AND EXISTS (
                SELECT 1 FROM financial_core_verification v
                JOIN financial_core_paymenttransaction t ON t.id = v.transaction_id
                JOIN financial_core_paymentattempt a ON a.id = t.attempt_id
                WHERE a.payment_id = target_payment_id
                  AND v.application_state IN ('unapplied', 'applied_blocking_success', 'review_required')
            ) THEN
                RAISE EXCEPTION 'Verification evidence blocks a new collection attempt' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' AND NEW.status = 'definitive_failed' AND OLD.status <> NEW.status
               AND EXISTS (
                    SELECT 1 FROM financial_core_verification v
                    JOIN financial_core_paymenttransaction t ON t.id = v.transaction_id
                    WHERE t.attempt_id = NEW.id
                      AND v.application_state = 'applied_blocking_success'
               )
            THEN
                RAISE EXCEPTION 'Successful Verification blocks definitive failure' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_c2b1_attempt_evidence
        BEFORE INSERT OR UPDATE ON financial_core_paymentattempt
        FOR EACH ROW EXECUTE FUNCTION financial_core_c2b1_attempt_evidence_guard();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_c2b1_payment_reopen_guard()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.collection_status = 'open' AND OLD.collection_status <> 'open' AND (
                EXISTS (
                    SELECT 1 FROM financial_core_verification v
                    JOIN financial_core_paymenttransaction t ON t.id = v.transaction_id
                    JOIN financial_core_paymentattempt a ON a.id = t.attempt_id
                    WHERE a.payment_id = NEW.id
                      AND v.application_state IN ('unapplied', 'applied_blocking_success', 'review_required')
                ) OR EXISTS (
                    SELECT 1 FROM financial_core_reviewcase r
                    WHERE r.payment_id = NEW.id AND r.status IN ('open', 'investigating', 'approval_pending')
                )
            ) THEN
                RAISE EXCEPTION 'Evidence or review blocker prevents Payment reopening' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_c2b1_payment_reopen
        BEFORE UPDATE OF collection_status ON financial_core_payment
        FOR EACH ROW EXECUTE FUNCTION financial_core_c2b1_payment_reopen_guard();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_c2b1_no_provider_receipt_journal()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.source_type IN ('provider_receipt', 'provider_verified_funds') THEN
                RAISE EXCEPTION 'C2B1 cannot post provider receipt journals' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_c2b1_no_provider_receipt_journal
        BEFORE INSERT ON financial_core_journalentry
        FOR EACH ROW EXECUTE FUNCTION financial_core_c2b1_no_provider_receipt_journal();
        """
    )


def drop_c2b1_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_c2b1_no_provider_receipt_journal ON financial_core_journalentry;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_c2b1_payment_reopen ON financial_core_payment;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_c2b1_attempt_evidence ON financial_core_paymentattempt;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_verification_work_protected ON financial_core_verificationworkitem;"
    )
    for table in reversed(APPEND_ONLY_TABLES):
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_c2b1_no_provider_receipt_journal();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_c2b1_payment_reopen_guard();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_c2b1_attempt_evidence_guard();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_verification_work();")


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0008_callbackreceipt_providerevent_and_more")]

    operations = [migrations.RunPython(create_c2b1_guards, drop_c2b1_guards)]
