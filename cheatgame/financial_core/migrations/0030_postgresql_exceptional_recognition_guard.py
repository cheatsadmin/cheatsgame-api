from django.db import migrations


def install_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_require_confirmed_evidence()
        RETURNS trigger AS $$
        DECLARE
            successful_amount numeric;
            successful_attempt_exists boolean;
            exceptional_amount numeric;
            exceptional_authority_exists boolean;
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

            SELECT COALESCE(SUM(fa.amount), 0),
                   COALESCE(BOOL_OR(era.id IS NOT NULL), false)
              INTO exceptional_amount, exceptional_authority_exists
              FROM financial_core_exceptionalrecognitionauthorization era
              JOIN financial_core_financialallocation fa
                ON fa.verification_id = era.verification_id
               AND fa.payment_id = era.payment_id
               AND fa.attempt_id = era.attempt_id
               AND fa.transaction_id = era.transaction_id
               AND fa.merchant_account_version_id = era.merchant_account_version_id
               AND fa.provider_reference = era.provider_reference
               AND fa.amount = era.amount
               AND fa.currency = era.currency
              JOIN financial_core_verification v
                ON v.id = era.verification_id
               AND v.transaction_id = era.transaction_id
               AND v.provider_reference = era.provider_reference
               AND v.canonical_allocation_amount = era.amount
               AND v.canonical_currency = era.currency
               AND v.evidence_hash = era.evidence_hash
              JOIN financial_core_latepaymentadjudication adj
                ON adj.id = era.adjudication_id
               AND adj.status = 'approved'
               AND adj.decision = 'accept'
             WHERE era.payment_id = NEW.id
               AND era.status IN ('authorized', 'applied');

            IF successful_amount + exceptional_amount < NEW.confirmed_amount THEN
                RAISE EXCEPTION 'Payment confirmed amount lacks successful or adjudicated provider evidence'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.confirmed_amount > 0
               AND NOT successful_attempt_exists
               AND NOT exceptional_authority_exists THEN
                RAISE EXCEPTION 'Payment confirmed amount lacks successful or adjudicated authority'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.collection_status IN ('paid_pending_finalization', 'paid')
               AND NOT successful_attempt_exists
               AND NOT exceptional_authority_exists THEN
                RAISE EXCEPTION 'paid Payment lacks successful or adjudicated authority'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_payment_allocation_projection()
        RETURNS trigger AS $$
        DECLARE
            target_payment_id bigint;
            payment_due numeric;
            payment_confirmed numeric;
            payment_status varchar;
            allocation_total numeric;
        BEGIN
            IF TG_TABLE_NAME = 'financial_core_financialallocation' THEN
                target_payment_id := NEW.payment_id;
            ELSE
                target_payment_id := NEW.id;
            END IF;
            SELECT amount_due, confirmed_amount, collection_status
              INTO payment_due, payment_confirmed, payment_status
              FROM financial_core_payment WHERE id = target_payment_id;
            SELECT COALESCE(SUM(amount), 0) INTO allocation_total
              FROM financial_core_financialallocation WHERE payment_id = target_payment_id;
            IF payment_confirmed <> allocation_total THEN
                RAISE EXCEPTION 'Payment confirmed amount must equal immutable allocations' USING ERRCODE = '23514';
            END IF;
            IF payment_status IN ('paid_pending_finalization', 'paid') AND payment_confirmed <> payment_due THEN
                RAISE EXCEPTION 'Paid Payment requires exact funding' USING ERRCODE = '23514';
            END IF;
            IF allocation_total > 0 AND payment_status IN ('open', 'processing', 'canceled') THEN
                RAISE EXCEPTION 'Applied provider funds cannot be reopened or canceled' USING ERRCODE = '23514';
            END IF;
            IF payment_status = 'paid' AND NOT EXISTS (
                SELECT 1 FROM financial_core_commercialfinalization WHERE payment_id = target_payment_id
            ) THEN
                RAISE EXCEPTION 'Paid Payment requires immutable commercial finalization' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM financial_core_financialallocation fa
                  JOIN financial_core_paymenttransaction tx ON tx.id = fa.transaction_id
                  JOIN financial_core_paymentattempt pa ON pa.id = fa.attempt_id
                 WHERE fa.payment_id = target_payment_id
                   AND (tx.status <> 'succeeded' OR pa.status <> 'succeeded')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM financial_core_exceptionalrecognitionauthorization era
                         JOIN financial_core_latepaymentadjudication adj
                           ON adj.id = era.adjudication_id
                          AND adj.status = 'approved'
                          AND adj.decision = 'accept'
                        WHERE era.verification_id = fa.verification_id
                          AND era.payment_id = fa.payment_id
                          AND era.attempt_id = fa.attempt_id
                          AND era.transaction_id = fa.transaction_id
                          AND era.merchant_account_version_id = fa.merchant_account_version_id
                          AND era.provider_reference = fa.provider_reference
                          AND era.amount = fa.amount
                          AND era.currency = fa.currency
                          AND era.status = 'applied'
                          AND era.allocation_id = fa.id
                   )
            ) THEN
                RAISE EXCEPTION 'Applied allocation requires successful or adjudicated authority'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def restore_guard(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
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
        CREATE OR REPLACE FUNCTION financial_core_validate_payment_allocation_projection()
        RETURNS trigger AS $$
        DECLARE
            target_payment_id bigint;
            payment_due numeric;
            payment_confirmed numeric;
            payment_status varchar;
            allocation_total numeric;
        BEGIN
            IF TG_TABLE_NAME = 'financial_core_financialallocation' THEN
                target_payment_id := NEW.payment_id;
            ELSE
                target_payment_id := NEW.id;
            END IF;
            SELECT amount_due, confirmed_amount, collection_status
              INTO payment_due, payment_confirmed, payment_status
              FROM financial_core_payment WHERE id = target_payment_id;
            SELECT COALESCE(SUM(amount), 0) INTO allocation_total
              FROM financial_core_financialallocation WHERE payment_id = target_payment_id;
            IF payment_confirmed <> allocation_total THEN
                RAISE EXCEPTION 'Payment confirmed amount must equal immutable allocations' USING ERRCODE = '23514';
            END IF;
            IF payment_status IN ('paid_pending_finalization', 'paid') AND payment_confirmed <> payment_due THEN
                RAISE EXCEPTION 'Paid Payment requires exact funding' USING ERRCODE = '23514';
            END IF;
            IF allocation_total > 0 AND payment_status IN ('open', 'processing', 'canceled') THEN
                RAISE EXCEPTION 'Applied provider funds cannot be reopened or canceled' USING ERRCODE = '23514';
            END IF;
            IF payment_status = 'paid' AND NOT EXISTS (
                SELECT 1 FROM financial_core_commercialfinalization WHERE payment_id = target_payment_id
            ) THEN
                RAISE EXCEPTION 'Paid Payment requires immutable commercial finalization' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM financial_core_financialallocation fa
                JOIN financial_core_paymenttransaction tx ON tx.id = fa.transaction_id
                JOIN financial_core_paymentattempt pa ON pa.id = fa.attempt_id
                WHERE fa.payment_id = target_payment_id
                  AND (tx.status <> 'succeeded' OR pa.status <> 'succeeded')
            ) THEN
                RAISE EXCEPTION 'Applied allocation requires successful Attempt and Transaction' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0029_latepaymentadjudication_and_more"),
    ]

    operations = [
        migrations.RunPython(install_guard, restore_guard),
    ]
