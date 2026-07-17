from django.db import migrations


def create_verified_funds_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_c2b1_no_provider_receipt_journal ON financial_core_journalentry;"
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_financialallocation_append_only
        BEFORE UPDATE OR DELETE ON financial_core_financialallocation
        FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

        CREATE OR REPLACE FUNCTION financial_core_protect_receipt_policy()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Receipt accounting policy history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.merchant_account_version_id, OLD.policy_key, OLD.version,
                   OLD.provider_clearing_account_id, OLD.customer_unapplied_funds_account_id, OLD.currency)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.merchant_account_version_id, NEW.policy_key, NEW.version,
                   NEW.provider_clearing_account_id, NEW.customer_unapplied_funds_account_id, NEW.currency)
            THEN
                RAISE EXCEPTION 'Receipt accounting policy identity is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_receipt_policy_protected
        BEFORE UPDATE OR DELETE ON financial_core_receiptaccountingpolicyversion
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_receipt_policy();

        CREATE OR REPLACE FUNCTION financial_core_protect_finalization_work()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Commercial-finalization work history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.payment_id, OLD.finalizer_version, OLD.deterministic_identity,
                   OLD.max_attempts, OLD.correlation_id, OLD.causation_id)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.payment_id, NEW.finalizer_version, NEW.deterministic_identity,
                   NEW.max_attempts, NEW.correlation_id, NEW.causation_id)
            THEN
                RAISE EXCEPTION 'Commercial-finalization work identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('completed', 'canceled') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'Terminal finalization work cannot be reopened' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_finalization_work_protected
        BEFORE UPDATE OR DELETE ON financial_core_commercialfinalizationworkitem
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_finalization_work();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'Successful PaymentAttempt cannot be inserted directly' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentAttempt cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.payment_id, OLD.sequence, OLD.requested_amount, OLD.currency,
                   OLD.tender_type, OLD.provider, OLD.merchant_account_ref, OLD.capability_version_id,
                   OLD.merchant_account_version_id, OLD.idempotency_key, OLD.request_hash)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.payment_id, NEW.sequence, NEW.requested_amount, NEW.currency,
                   NEW.tender_type, NEW.provider, NEW.merchant_account_ref, NEW.capability_version_id,
                   NEW.merchant_account_version_id, NEW.idempotency_key, NEW.request_hash)
            THEN
                RAISE EXCEPTION 'PaymentAttempt identity and requested terms are immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('succeeded', 'definitive_failed') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'terminal PaymentAttempt cannot be reopened' USING ERRCODE = '23514';
            END IF;
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded'
               AND NOT EXISTS (SELECT 1 FROM financial_core_financialallocation WHERE attempt_id = NEW.id)
            THEN
                RAISE EXCEPTION 'Successful PaymentAttempt requires FinancialAllocation' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_protect_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'Successful PaymentTransaction cannot be inserted directly' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentTransaction provider operation cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.attempt_id, OLD.sequence, OLD.operation_type, OLD.parent_id,
                   OLD.provider, OLD.merchant_account_ref, OLD.capability_version_id,
                   OLD.merchant_account_version_id, OLD.adapter_contract_version, OLD.merchant_reference,
                   OLD.amount, OLD.currency, OLD.provider_amount, OLD.provider_unit,
                   OLD.provider_conversion_policy_version, OLD.provider_idempotency_reference,
                   OLD.request_fingerprint, OLD.correlation_id, OLD.causation_id, OLD.idempotency_key)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.attempt_id, NEW.sequence, NEW.operation_type, NEW.parent_id,
                   NEW.provider, NEW.merchant_account_ref, NEW.capability_version_id,
                   NEW.merchant_account_version_id, NEW.adapter_contract_version, NEW.merchant_reference,
                   NEW.amount, NEW.currency, NEW.provider_amount, NEW.provider_unit,
                   NEW.provider_conversion_policy_version, NEW.provider_idempotency_reference,
                   NEW.request_fingerprint, NEW.correlation_id, NEW.causation_id, NEW.idempotency_key)
            THEN
                RAISE EXCEPTION 'PaymentTransaction provider identity and money terms are immutable' USING ERRCODE = '55000';
            END IF;
            IF (OLD.provider_authority IS NOT NULL AND OLD.provider_authority <> ''
                    AND NEW.provider_authority IS DISTINCT FROM OLD.provider_authority)
                OR (OLD.provider_reference IS NOT NULL AND OLD.provider_reference <> ''
                    AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
                OR (OLD.evidence_hash <> '' AND NEW.evidence_hash IS DISTINCT FROM OLD.evidence_hash)
                OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at)
            THEN
                RAISE EXCEPTION 'PaymentTransaction provider evidence is write-once' USING ERRCODE = '55000';
            END IF;
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded'
               AND NOT EXISTS (SELECT 1 FROM financial_core_financialallocation WHERE transaction_id = NEW.id)
            THEN
                RAISE EXCEPTION 'Successful PaymentTransaction requires FinancialAllocation' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_allocation()
        RETURNS trigger AS $$
        DECLARE
            tx_attempt_id bigint;
            tx_account_id bigint;
            tx_reference varchar;
            tx_amount numeric;
            tx_currency varchar;
            attempt_payment_id bigint;
            ver_tx_id bigint;
            ver_account_id bigint;
            ver_reference varchar;
            ver_amount numeric;
            ver_currency varchar;
            ver_outcome varchar;
            ver_effect varchar;
            ver_finality varchar;
            ver_state varchar;
            ver_basis varchar;
            journal_source_type varchar;
            journal_source_id varchar;
            policy_account_id bigint;
            clearing_type varchar;
            clearing_currency varchar;
            clearing_status varchar;
            liability_type varchar;
            liability_currency varchar;
            liability_status varchar;
        BEGIN
            SELECT attempt_id, merchant_account_version_id, provider_reference, amount, currency
              INTO tx_attempt_id, tx_account_id, tx_reference, tx_amount, tx_currency
              FROM financial_core_paymenttransaction WHERE id = NEW.transaction_id;
            SELECT payment_id INTO attempt_payment_id
              FROM financial_core_paymentattempt WHERE id = NEW.attempt_id;
            SELECT transaction_id, merchant_account_version_id, provider_reference,
                   canonical_allocation_amount, canonical_currency, normalized_outcome,
                   normalized_financial_effect, finality, application_state, evidence_basis
              INTO ver_tx_id, ver_account_id, ver_reference, ver_amount, ver_currency,
                   ver_outcome, ver_effect, ver_finality, ver_state, ver_basis
              FROM financial_core_verification WHERE id = NEW.verification_id;
            SELECT source_type, source_id INTO journal_source_type, journal_source_id
              FROM financial_core_journalentry WHERE id = NEW.journal_entry_id;
            SELECT merchant_account_version_id INTO policy_account_id
              FROM financial_core_receiptaccountingpolicyversion WHERE id = NEW.accounting_policy_version_id;
            SELECT clearing.account_type, clearing.currency, clearing.status,
                   liability.account_type, liability.currency, liability.status
              INTO clearing_type, clearing_currency, clearing_status,
                   liability_type, liability_currency, liability_status
              FROM financial_core_receiptaccountingpolicyversion policy
              JOIN financial_core_financialaccount clearing
                ON clearing.id = policy.provider_clearing_account_id
              JOIN financial_core_financialaccount liability
                ON liability.id = policy.customer_unapplied_funds_account_id
             WHERE policy.id = NEW.accounting_policy_version_id;

            IF tx_attempt_id IS DISTINCT FROM NEW.attempt_id
               OR attempt_payment_id IS DISTINCT FROM NEW.payment_id
               OR ver_tx_id IS DISTINCT FROM NEW.transaction_id
               OR tx_account_id IS DISTINCT FROM NEW.merchant_account_version_id
               OR ver_account_id IS DISTINCT FROM NEW.merchant_account_version_id
               OR policy_account_id IS DISTINCT FROM NEW.merchant_account_version_id
               OR tx_reference IS DISTINCT FROM NEW.provider_reference
               OR ver_reference IS DISTINCT FROM NEW.provider_reference
               OR tx_amount IS DISTINCT FROM NEW.amount
               OR ver_amount IS DISTINCT FROM NEW.amount
               OR tx_currency IS DISTINCT FROM NEW.currency
               OR ver_currency IS DISTINCT FROM NEW.currency
            THEN
                RAISE EXCEPTION 'Financial allocation ownership or money mismatch' USING ERRCODE = '23514';
            END IF;
            IF ver_outcome <> 'confirmed_success' OR ver_effect <> 'paid' OR ver_finality <> 'final'
               OR ver_state <> 'applied_blocking_success'
               OR ver_basis NOT IN ('server_to_server', 'authenticated_settlement')
            THEN
                RAISE EXCEPTION 'Financial allocation requires eligible authenticated success evidence'
                    USING ERRCODE = '23514';
            END IF;
            IF journal_source_type <> 'provider_receipt' OR journal_source_id <> NEW.public_id::text THEN
                RAISE EXCEPTION 'Financial allocation Journal source mismatch' USING ERRCODE = '23514';
            END IF;
            IF clearing_type <> 'asset' OR clearing_currency <> 'IRR' OR clearing_status <> 'active'
               OR liability_type <> 'liability' OR liability_currency <> 'IRR' OR liability_status <> 'active'
            THEN
                RAISE EXCEPTION 'Financial allocation receipt accounts are invalid' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_allocation_valid
        BEFORE INSERT ON financial_core_financialallocation
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_allocation();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_provider_receipt()
        RETURNS trigger AS $$
        DECLARE
            target_entry_id bigint;
            allocation_amount numeric;
            allocation_currency varchar;
            allocation_policy_id bigint;
            clearing_id bigint;
            liability_id bigint;
        BEGIN
            target_entry_id := NEW.id;
            IF NEW.source_type <> 'provider_receipt' THEN
                IF NEW.source_type = 'provider_verified_funds' THEN
                    RAISE EXCEPTION 'Unsupported provider receipt source type' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            SELECT amount, currency, accounting_policy_version_id
              INTO allocation_amount, allocation_currency, allocation_policy_id
              FROM financial_core_financialallocation
             WHERE journal_entry_id = target_entry_id AND public_id::text = NEW.source_id;
            IF allocation_amount IS NULL THEN
                RAISE EXCEPTION 'Provider receipt Journal requires one immutable allocation' USING ERRCODE = '23514';
            END IF;
            SELECT provider_clearing_account_id, customer_unapplied_funds_account_id
              INTO clearing_id, liability_id
              FROM financial_core_receiptaccountingpolicyversion WHERE id = allocation_policy_id;
            IF allocation_currency <> 'IRR'
               OR (SELECT COUNT(*) FROM financial_core_journalposting WHERE entry_id = target_entry_id) <> 2
               OR (SELECT COALESCE(SUM(amount), 0) FROM financial_core_journalposting
                    WHERE entry_id = target_entry_id AND direction = 'debit'
                      AND currency = 'IRR' AND account_id = clearing_id) <> allocation_amount
               OR (SELECT COALESCE(SUM(amount), 0) FROM financial_core_journalposting
                    WHERE entry_id = target_entry_id AND direction = 'credit'
                      AND currency = 'IRR' AND account_id = liability_id) <> allocation_amount
            THEN
                RAISE EXCEPTION 'Provider receipt Journal does not match allocation and policy'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER financial_core_provider_receipt_valid
        AFTER INSERT ON financial_core_journalentry
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_provider_receipt();
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
            IF payment_status = 'paid_pending_finalization' AND payment_confirmed <> payment_due THEN
                RAISE EXCEPTION 'Paid-pending-finalization Payment requires exact funding' USING ERRCODE = '23514';
            END IF;
            IF allocation_total > 0 AND payment_status IN ('open', 'processing', 'canceled') THEN
                RAISE EXCEPTION 'Applied provider funds cannot be reopened or canceled' USING ERRCODE = '23514';
            END IF;
            IF EXISTS (
                SELECT 1
                  FROM financial_core_financialallocation fa
                  JOIN financial_core_paymenttransaction tx ON tx.id = fa.transaction_id
                  JOIN financial_core_paymentattempt pa ON pa.id = fa.attempt_id
                 WHERE fa.payment_id = target_payment_id
                   AND (tx.status <> 'succeeded' OR pa.status <> 'succeeded')
            ) THEN
                RAISE EXCEPTION 'Applied allocation requires successful Attempt and Transaction'
                    USING ERRCODE = '23514';
            END IF;
            IF payment_status = 'paid' THEN
                RAISE EXCEPTION 'Provider Execution Phase 1 cannot transition Payment to paid' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER financial_core_allocation_payment_projection
        AFTER INSERT ON financial_core_financialallocation
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_payment_allocation_projection();

        CREATE CONSTRAINT TRIGGER financial_core_payment_allocation_projection
        AFTER UPDATE OF confirmed_amount, collection_status ON financial_core_payment
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_payment_allocation_projection();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_require_allocation_for_success()
        RETURNS trigger AS $$
        BEGIN
            IF TG_TABLE_NAME = 'financial_core_paymenttransaction'
               AND NEW.status = 'succeeded' AND OLD.status <> NEW.status
               AND NOT EXISTS (SELECT 1 FROM financial_core_financialallocation WHERE transaction_id = NEW.id)
            THEN
                RAISE EXCEPTION 'Successful PaymentTransaction requires FinancialAllocation' USING ERRCODE = '23514';
            END IF;
            IF TG_TABLE_NAME = 'financial_core_paymentattempt'
               AND NEW.status = 'succeeded' AND OLD.status <> NEW.status
               AND NOT EXISTS (SELECT 1 FROM financial_core_financialallocation WHERE attempt_id = NEW.id)
            THEN
                RAISE EXCEPTION 'Successful PaymentAttempt requires FinancialAllocation' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_transaction_success_allocation
        BEFORE UPDATE OF status ON financial_core_paymenttransaction
        FOR EACH ROW EXECUTE FUNCTION financial_core_require_allocation_for_success();

        CREATE TRIGGER financial_core_attempt_success_allocation
        BEFORE UPDATE OF status ON financial_core_paymentattempt
        FOR EACH ROW EXECUTE FUNCTION financial_core_require_allocation_for_success();
        """
    )


def drop_verified_funds_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS financial_core_attempt_success_allocation ON financial_core_paymentattempt;
        DROP TRIGGER IF EXISTS financial_core_transaction_success_allocation ON financial_core_paymenttransaction;
        DROP TRIGGER IF EXISTS financial_core_payment_allocation_projection ON financial_core_payment;
        DROP TRIGGER IF EXISTS financial_core_allocation_payment_projection ON financial_core_financialallocation;
        DROP TRIGGER IF EXISTS financial_core_provider_receipt_valid ON financial_core_journalentry;
        DROP TRIGGER IF EXISTS financial_core_allocation_valid ON financial_core_financialallocation;
        DROP TRIGGER IF EXISTS financial_core_finalization_work_protected ON financial_core_commercialfinalizationworkitem;
        DROP TRIGGER IF EXISTS financial_core_receipt_policy_protected ON financial_core_receiptaccountingpolicyversion;
        DROP TRIGGER IF EXISTS financial_core_financialallocation_append_only ON financial_core_financialallocation;
        DROP FUNCTION IF EXISTS financial_core_require_allocation_for_success();
        DROP FUNCTION IF EXISTS financial_core_validate_payment_allocation_projection();
        DROP FUNCTION IF EXISTS financial_core_validate_provider_receipt();
        DROP FUNCTION IF EXISTS financial_core_validate_allocation();
        DROP FUNCTION IF EXISTS financial_core_protect_finalization_work();
        DROP FUNCTION IF EXISTS financial_core_protect_receipt_policy();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'C2A cannot create a successful PaymentAttempt' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'PaymentAttempt cannot be deleted' USING ERRCODE = '55000'; END IF;
            IF ROW(OLD.public_id, OLD.payment_id, OLD.sequence, OLD.requested_amount, OLD.currency,
                   OLD.tender_type, OLD.provider, OLD.merchant_account_ref, OLD.capability_version_id,
                   OLD.merchant_account_version_id, OLD.idempotency_key, OLD.request_hash)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.payment_id, NEW.sequence, NEW.requested_amount, NEW.currency,
                   NEW.tender_type, NEW.provider, NEW.merchant_account_ref, NEW.capability_version_id,
                   NEW.merchant_account_version_id, NEW.idempotency_key, NEW.request_hash)
            THEN RAISE EXCEPTION 'PaymentAttempt identity and requested terms are immutable' USING ERRCODE = '55000'; END IF;
            IF OLD.status IN ('succeeded', 'definitive_failed') AND NEW.status <> OLD.status
            THEN RAISE EXCEPTION 'terminal PaymentAttempt cannot be reopened' USING ERRCODE = '23514'; END IF;
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded'
            THEN RAISE EXCEPTION 'C2A cannot enter successful PaymentAttempt state' USING ERRCODE = '23514'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_protect_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'C2A cannot create a successful PaymentTransaction' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'PaymentTransaction provider operation cannot be deleted' USING ERRCODE = '55000'; END IF;
            IF ROW(OLD.public_id, OLD.attempt_id, OLD.sequence, OLD.operation_type, OLD.parent_id,
                   OLD.provider, OLD.merchant_account_ref, OLD.capability_version_id,
                   OLD.merchant_account_version_id, OLD.adapter_contract_version, OLD.merchant_reference,
                   OLD.amount, OLD.currency, OLD.provider_amount, OLD.provider_unit,
                   OLD.provider_conversion_policy_version, OLD.provider_idempotency_reference,
                   OLD.request_fingerprint, OLD.correlation_id, OLD.causation_id, OLD.idempotency_key)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.attempt_id, NEW.sequence, NEW.operation_type, NEW.parent_id,
                   NEW.provider, NEW.merchant_account_ref, NEW.capability_version_id,
                   NEW.merchant_account_version_id, NEW.adapter_contract_version, NEW.merchant_reference,
                   NEW.amount, NEW.currency, NEW.provider_amount, NEW.provider_unit,
                   NEW.provider_conversion_policy_version, NEW.provider_idempotency_reference,
                   NEW.request_fingerprint, NEW.correlation_id, NEW.causation_id, NEW.idempotency_key)
            THEN RAISE EXCEPTION 'PaymentTransaction provider identity and money terms are immutable' USING ERRCODE = '55000'; END IF;
            IF (OLD.provider_authority IS NOT NULL AND OLD.provider_authority <> '' AND NEW.provider_authority IS DISTINCT FROM OLD.provider_authority)
                OR (OLD.provider_reference IS NOT NULL AND OLD.provider_reference <> '' AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
                OR (OLD.evidence_hash <> '' AND NEW.evidence_hash IS DISTINCT FROM OLD.evidence_hash)
                OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at)
            THEN RAISE EXCEPTION 'PaymentTransaction provider evidence is write-once' USING ERRCODE = '55000'; END IF;
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded'
            THEN RAISE EXCEPTION 'C2A cannot enter successful PaymentTransaction state' USING ERRCODE = '23514'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_c2b1_no_provider_receipt_journal
        BEFORE INSERT ON financial_core_journalentry
        FOR EACH ROW EXECUTE FUNCTION financial_core_c2b1_no_provider_receipt_journal();
        """
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0010_verified_funds_recognition")]
    operations = [migrations.RunPython(create_verified_funds_guards, drop_verified_funds_guards)]
