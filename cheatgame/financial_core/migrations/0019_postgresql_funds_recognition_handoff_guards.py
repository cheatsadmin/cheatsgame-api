from django.db import migrations


def create_funds_recognition_handoff_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_recognition_handoff(p_payment_id bigint)
        RETURNS void AS $$
        DECLARE
            payment_status varchar;
            payment_due numeric;
            payment_confirmed numeric;
            payment_public_id uuid;
            allocation_total numeric;
        BEGIN
            SELECT collection_status, amount_due, confirmed_amount, public_id
              INTO STRICT payment_status, payment_due, payment_confirmed, payment_public_id
              FROM financial_core_payment WHERE id = p_payment_id;
            SELECT COALESCE(SUM(amount), 0) INTO allocation_total
              FROM financial_core_financialallocation allocation
             WHERE allocation.payment_id = p_payment_id;

            IF payment_status IN ('paid_pending_finalization', 'paid') THEN
                IF payment_confirmed <> payment_due OR allocation_total <> payment_due THEN
                    RAISE EXCEPTION 'Recognition handoff requires exact immutable funding'
                        USING ERRCODE = '23514';
                END IF;
                IF (
                    SELECT COUNT(*) FROM financial_core_commercialfinalizationworkitem work
                    WHERE work.payment_id = p_payment_id
                      AND work.finalizer_version = 'commercial-finalizer-v1-dormant'
                ) <> 1 OR (
                    SELECT COUNT(*) FROM financial_core_commercialfinalizationworkitem work
                    WHERE work.payment_id = p_payment_id
                ) <> 1 THEN
                    RAISE EXCEPTION 'Recognized Payment requires exactly one dormant finalization work item'
                        USING ERRCODE = '23514';
                END IF;
                IF (
                    SELECT COUNT(*) FROM financial_core_financialoutboxmessage message
                    WHERE message.topic = 'commercial.finalization.requested'
                      AND message.aggregate_type = 'financial_core.payment'
                      AND message.aggregate_id = payment_public_id::text
                ) <> 1 THEN
                    RAISE EXCEPTION 'Recognized Payment requires exactly one transactional finalization outbox'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_check_payment_recognition_handoff()
        RETURNS trigger AS $$
        BEGIN
            PERFORM financial_core_validate_recognition_handoff(NEW.id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_check_work_recognition_handoff()
        RETURNS trigger AS $$
        DECLARE payment_status varchar;
        BEGIN
            SELECT collection_status INTO STRICT payment_status
              FROM financial_core_payment WHERE id = NEW.payment_id;
            IF payment_status NOT IN ('paid_pending_finalization', 'paid') THEN
                RAISE EXCEPTION 'Finalization work requires recognized Payment'
                    USING ERRCODE = '23514';
            END IF;
            PERFORM financial_core_validate_recognition_handoff(NEW.payment_id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_check_outbox_recognition_handoff()
        RETURNS trigger AS $$
        DECLARE target_payment_id bigint;
        DECLARE payment_status varchar;
        BEGIN
            IF NEW.topic <> 'commercial.finalization.requested' THEN
                RETURN NULL;
            END IF;
            SELECT id, collection_status INTO STRICT target_payment_id, payment_status
              FROM financial_core_payment
             WHERE public_id::text = NEW.aggregate_id
               AND NEW.aggregate_type = 'financial_core.payment';
            IF payment_status NOT IN ('paid_pending_finalization', 'paid') THEN
                RAISE EXCEPTION 'Finalization outbox requires recognized Payment'
                    USING ERRCODE = '23514';
            END IF;
            PERFORM financial_core_validate_recognition_handoff(target_payment_id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DO $$
        DECLARE candidate record;
        BEGIN
            FOR candidate IN
                SELECT id FROM financial_core_payment
                 WHERE collection_status IN ('paid_pending_finalization', 'paid')
            LOOP
                PERFORM financial_core_validate_recognition_handoff(candidate.id);
            END LOOP;
            IF EXISTS (
                SELECT 1
                  FROM financial_core_commercialfinalizationworkitem work
                  JOIN financial_core_payment payment ON payment.id = work.payment_id
                 WHERE payment.collection_status NOT IN ('paid_pending_finalization', 'paid')
            ) THEN
                RAISE EXCEPTION 'Existing finalization work lacks recognized Payment'
                    USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM financial_core_financialoutboxmessage message
             LEFT JOIN financial_core_payment payment
                    ON payment.public_id::text = message.aggregate_id
                   AND message.aggregate_type = 'financial_core.payment'
                 WHERE message.topic = 'commercial.finalization.requested'
                   AND (
                       payment.id IS NULL
                       OR payment.collection_status NOT IN ('paid_pending_finalization', 'paid')
                   )
            ) THEN
                RAISE EXCEPTION 'Existing finalization outbox lacks recognized Payment'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER financial_core_payment_recognition_handoff
        AFTER UPDATE OF confirmed_amount, collection_status ON financial_core_payment
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_check_payment_recognition_handoff();

        CREATE CONSTRAINT TRIGGER financial_core_work_recognition_handoff
        AFTER INSERT ON financial_core_commercialfinalizationworkitem
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_check_work_recognition_handoff();

        CREATE CONSTRAINT TRIGGER financial_core_outbox_recognition_handoff
        AFTER INSERT ON financial_core_financialoutboxmessage
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_check_outbox_recognition_handoff();
        """
    )


def drop_funds_recognition_handoff_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS financial_core_outbox_recognition_handoff
            ON financial_core_financialoutboxmessage;
        DROP TRIGGER IF EXISTS financial_core_work_recognition_handoff
            ON financial_core_commercialfinalizationworkitem;
        DROP TRIGGER IF EXISTS financial_core_payment_recognition_handoff
            ON financial_core_payment;
        DROP FUNCTION IF EXISTS financial_core_check_outbox_recognition_handoff();
        DROP FUNCTION IF EXISTS financial_core_check_work_recognition_handoff();
        DROP FUNCTION IF EXISTS financial_core_check_payment_recognition_handoff();
        DROP FUNCTION IF EXISTS financial_core_validate_recognition_handoff(bigint);
        """
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0018_postgresql_verification_truth_guards")]

    operations = [
        migrations.RunPython(
            create_funds_recognition_handoff_guards,
            drop_funds_recognition_handoff_guards,
        )
    ]
