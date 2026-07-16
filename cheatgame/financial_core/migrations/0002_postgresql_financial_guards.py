from django.db import migrations


APPEND_ONLY_TABLES = (
    "financial_core_financialevent",
    "financial_core_journalentry",
    "financial_core_journalposting",
    "financial_core_reviewaction",
)


def create_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_reject_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'financial record %% is append-only', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
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
        CREATE OR REPLACE FUNCTION financial_core_assert_balanced_journal()
        RETURNS trigger AS $$
        DECLARE
            target_entry_id bigint;
            posting_count bigint;
        BEGIN
            IF TG_TABLE_NAME = 'financial_core_journalentry' THEN
                target_entry_id := NEW.id;
            ELSE
                target_entry_id := NEW.entry_id;
            END IF;

            SELECT COUNT(*) INTO posting_count
            FROM financial_core_journalposting
            WHERE entry_id = target_entry_id;

            IF posting_count < 2 THEN
                RAISE EXCEPTION 'journal entry %% requires at least two postings', target_entry_id
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM financial_core_journalposting
                WHERE entry_id = target_entry_id
                GROUP BY currency
                HAVING SUM(
                    CASE direction
                        WHEN 'debit' THEN amount
                        WHEN 'credit' THEN -amount
                        ELSE 0
                    END
                ) <> 0
            ) THEN
                RAISE EXCEPTION 'journal entry %% is not balanced by currency', target_entry_id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE CONSTRAINT TRIGGER financial_core_journal_entry_balanced
        AFTER INSERT ON financial_core_journalentry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_assert_balanced_journal();
        """
    )
    schema_editor.execute(
        """
        CREATE CONSTRAINT TRIGGER financial_core_journal_posting_balanced
        AFTER INSERT ON financial_core_journalposting
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_assert_balanced_journal();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_require_review_resolution_action()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.status = 'resolved' AND OLD.status <> 'resolved' AND NOT EXISTS (
                SELECT 1
                FROM financial_core_reviewaction
                WHERE review_case_id = OLD.id
                  AND action_type = 'transition:resolved'
            ) THEN
                RAISE EXCEPTION 'ReviewCase %% resolution requires an append-only resolution action', OLD.id
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_review_resolution_action
        BEFORE UPDATE OF status ON financial_core_reviewcase
        FOR EACH ROW EXECUTE FUNCTION financial_core_require_review_resolution_action();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_payment()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Payment obligation cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.order_id, OLD.amount_due, OLD.currency)
                IS DISTINCT FROM ROW(NEW.public_id, NEW.order_id, NEW.amount_due, NEW.currency)
            THEN
                RAISE EXCEPTION 'Payment obligation identity and amount are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.collection_status IN ('paid', 'canceled')
               AND NEW.collection_status <> OLD.collection_status THEN
                RAISE EXCEPTION 'terminal Payment cannot be reopened'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_payment_protected
        BEFORE UPDATE OR DELETE ON financial_core_payment
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_payment();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentAttempt cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                OLD.public_id, OLD.payment_id, OLD.sequence, OLD.requested_amount,
                OLD.currency, OLD.tender_type, OLD.provider, OLD.merchant_account_ref,
                OLD.idempotency_key, OLD.request_hash
            ) IS DISTINCT FROM ROW(
                NEW.public_id, NEW.payment_id, NEW.sequence, NEW.requested_amount,
                NEW.currency, NEW.tender_type, NEW.provider, NEW.merchant_account_ref,
                NEW.idempotency_key, NEW.request_hash
            ) THEN
                RAISE EXCEPTION 'PaymentAttempt identity and requested terms are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('succeeded', 'definitive_failed') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'terminal PaymentAttempt cannot be reopened'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_paymentattempt_protected
        BEFORE UPDATE OR DELETE ON financial_core_paymentattempt
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_attempt();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentTransaction provider operation cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                OLD.public_id, OLD.attempt_id, OLD.sequence, OLD.operation_type,
                OLD.parent_id, OLD.provider, OLD.merchant_account_ref,
                OLD.merchant_reference, OLD.amount, OLD.currency,
                OLD.provider_amount, OLD.provider_unit, OLD.idempotency_key
            ) IS DISTINCT FROM ROW(
                NEW.public_id, NEW.attempt_id, NEW.sequence, NEW.operation_type,
                NEW.parent_id, NEW.provider, NEW.merchant_account_ref,
                NEW.merchant_reference, NEW.amount, NEW.currency,
                NEW.provider_amount, NEW.provider_unit, NEW.idempotency_key
            ) THEN
                RAISE EXCEPTION 'PaymentTransaction provider identity and money terms are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF (OLD.provider_authority IS NOT NULL AND OLD.provider_authority <> ''
                    AND NEW.provider_authority IS DISTINCT FROM OLD.provider_authority)
                OR (OLD.provider_reference IS NOT NULL AND OLD.provider_reference <> ''
                    AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
                OR (OLD.evidence_hash <> '' AND NEW.evidence_hash IS DISTINCT FROM OLD.evidence_hash)
                OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at)
            THEN
                RAISE EXCEPTION 'PaymentTransaction provider evidence is write-once'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_paymenttransaction_protected
        BEFORE UPDATE OR DELETE ON financial_core_paymenttransaction
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_transaction();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_require_confirmed_evidence()
        RETURNS trigger AS $$
        DECLARE
            successful_amount numeric;
            successful_attempt_exists boolean;
        BEGIN
            IF NEW.confirmed_amount = 0
               AND NEW.collection_status NOT IN ('paid_pending_finalization', 'paid') THEN
                RETURN NEW;
            END IF;
            SELECT COALESCE(SUM(t.amount), 0),
                   COALESCE(BOOL_OR(a.status = 'succeeded'), false)
              INTO successful_amount, successful_attempt_exists
              FROM financial_core_paymentattempt a
              LEFT JOIN financial_core_paymenttransaction t
                ON t.attempt_id = a.id
               AND t.status = 'succeeded'
               AND t.operation_type IN ('sale', 'capture')
             WHERE a.payment_id = NEW.id;
            IF successful_amount < NEW.confirmed_amount THEN
                RAISE EXCEPTION 'Payment confirmed amount lacks successful provider transaction evidence'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.confirmed_amount > 0 AND NOT successful_attempt_exists THEN
                RAISE EXCEPTION 'Payment confirmed amount lacks a successful PaymentAttempt'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.collection_status IN ('paid_pending_finalization', 'paid')
               AND NOT successful_attempt_exists THEN
                RAISE EXCEPTION 'paid Payment lacks a successful PaymentAttempt'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_payment_confirmed_evidence
        BEFORE INSERT OR UPDATE OF confirmed_amount, collection_status
        ON financial_core_payment
        FOR EACH ROW EXECUTE FUNCTION financial_core_require_confirmed_evidence();
        """
    )


def drop_postgresql_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_payment_confirmed_evidence "
        "ON financial_core_payment;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_payment_protected "
        "ON financial_core_payment;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_paymenttransaction_protected "
        "ON financial_core_paymenttransaction;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_paymentattempt_protected "
        "ON financial_core_paymentattempt;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_review_resolution_action "
        "ON financial_core_reviewcase;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_journal_posting_balanced "
        "ON financial_core_journalposting;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_journal_entry_balanced "
        "ON financial_core_journalentry;"
    )
    for table in reversed(APPEND_ONLY_TABLES):
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_assert_balanced_journal();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_require_confirmed_evidence();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_transaction();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_attempt();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_payment();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_require_review_resolution_action();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_reject_mutation();")


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0001_initial")]

    operations = [
        migrations.RunPython(create_postgresql_guards, drop_postgresql_guards),
    ]
