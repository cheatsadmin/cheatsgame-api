from django.db import migrations


def create_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE TRIGGER financial_core_commercial_finalization_append_only
        BEFORE UPDATE OR DELETE ON financial_core_commercialfinalization
        FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

        CREATE TRIGGER financial_core_standard_fulfillment_append_only
        BEFORE UPDATE OR DELETE ON financial_core_standardfulfillmentobligation
        FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

        CREATE TRIGGER financial_core_digital_fulfillment_append_only
        BEFORE UPDATE OR DELETE ON financial_core_digitalfulfillmentobligation
        FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

        CREATE OR REPLACE FUNCTION financial_core_protect_commercial_policy()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Commercial accounting policy history cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.public_id, OLD.policy_key, OLD.version, OLD.commerce_authority,
                   OLD.customer_unapplied_funds_account_id, OLD.merchandise_revenue_account_id,
                   OLD.shipping_revenue_account_id, OLD.currency)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.policy_key, NEW.version, NEW.commerce_authority,
                   NEW.customer_unapplied_funds_account_id, NEW.merchandise_revenue_account_id,
                   NEW.shipping_revenue_account_id, NEW.currency)
            THEN
                RAISE EXCEPTION 'Commercial accounting policy identity is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_commercial_policy_protected
        BEFORE UPDATE OR DELETE ON financial_core_commercialaccountingpolicyversion
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_commercial_policy();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_validate_commercial_finalization()
        RETURNS trigger AS $$
        DECLARE
            payment_order_id bigint;
            payment_due numeric;
            payment_confirmed numeric;
            payment_currency varchar;
            payment_collection_status varchar;
            order_checkout_id bigint;
            order_payment_status integer;
            order_fulfillment_status varchar;
            checkout_status varchar;
            policy_authority varchar;
            policy_currency varchar;
            liability_id bigint;
            merchandise_id bigint;
            shipping_id bigint;
            journal_source_type varchar;
            journal_source_id varchar;
            standard_required integer;
            standard_created integer;
            digital_required integer;
            digital_created integer;
        BEGIN
            SELECT p.order_id, p.amount_due, p.confirmed_amount, p.currency, p.collection_status
              INTO payment_order_id, payment_due, payment_confirmed, payment_currency, payment_collection_status
              FROM financial_core_payment p WHERE p.id = NEW.payment_id;
            SELECT o.checkout_id, o.payment_status, o.fulfillment_status
              INTO order_checkout_id, order_payment_status, order_fulfillment_status
              FROM shop_order o WHERE o.id = NEW.order_id;
            SELECT c.status INTO checkout_status FROM shop_checkout c WHERE c.id = order_checkout_id;
            SELECT commerce_authority, currency, customer_unapplied_funds_account_id,
                   merchandise_revenue_account_id, shipping_revenue_account_id
              INTO policy_authority, policy_currency, liability_id, merchandise_id, shipping_id
              FROM financial_core_commercialaccountingpolicyversion
             WHERE id = NEW.accounting_policy_version_id;
            SELECT source_type, source_id INTO journal_source_type, journal_source_id
              FROM financial_core_journalentry WHERE id = NEW.journal_entry_id;

            IF payment_order_id IS DISTINCT FROM NEW.order_id
               OR payment_due IS DISTINCT FROM NEW.amount
               OR payment_confirmed IS DISTINCT FROM NEW.amount
               OR payment_currency <> 'IRR' OR NEW.currency <> 'IRR'
               OR payment_collection_status <> 'paid'
               OR order_payment_status <> 3
               OR order_fulfillment_status <> 'processing'
               OR checkout_status <> 'paid'
               OR policy_authority IS DISTINCT FROM NEW.commerce_authority
               OR policy_currency <> 'IRR'
               OR journal_source_type <> 'commercial_reclassification'
               OR journal_source_id <> NEW.public_id::text
            THEN
                RAISE EXCEPTION 'Commercial finalization aggregate projection is inconsistent' USING ERRCODE = '23514';
            END IF;

            IF (SELECT COUNT(*) FROM financial_core_journalposting WHERE entry_id = NEW.journal_entry_id) < 2
               OR (SELECT COALESCE(SUM(amount), 0) FROM financial_core_journalposting
                    WHERE entry_id = NEW.journal_entry_id AND account_id = liability_id
                      AND direction = 'debit' AND currency = 'IRR') <> NEW.amount
               OR (SELECT COALESCE(SUM(amount), 0) FROM financial_core_journalposting
                    WHERE entry_id = NEW.journal_entry_id AND account_id = merchandise_id
                      AND direction = 'credit' AND currency = 'IRR') <> NEW.merchandise_amount
               OR (SELECT COALESCE(SUM(amount), 0) FROM financial_core_journalposting
                    WHERE entry_id = NEW.journal_entry_id AND account_id = shipping_id
                      AND direction = 'credit' AND currency = 'IRR') <> NEW.shipping_amount
            THEN
                RAISE EXCEPTION 'Commercial reclassification Journal does not match policy and finalization'
                    USING ERRCODE = '23514';
            END IF;

            SELECT COUNT(*) INTO standard_required FROM shop_orderitem WHERE order_id = NEW.order_id;
            SELECT COUNT(*) INTO standard_created FROM financial_core_standardfulfillmentobligation
             WHERE finalization_id = NEW.id;
            SELECT COUNT(*) INTO digital_created FROM financial_core_digitalfulfillmentobligation
             WHERE finalization_id = NEW.id;
            IF NEW.commerce_authority = 'standard_commerce' THEN
                IF standard_created <> standard_required OR digital_created <> 0
                   OR EXISTS (
                       SELECT 1 FROM shop_stockreservation r
                        WHERE r.order_id = NEW.order_id AND r.state <> 'consumed'
                   )
                   OR EXISTS (
                       SELECT 1 FROM shop_stockreservation r
                        WHERE r.order_id = NEW.order_id
                          AND (SELECT COALESCE(SUM(f.quantity), 0)
                                 FROM financial_core_standardfulfillmentobligation f
                                WHERE f.reservation_id = r.id) <> r.quantity
                   )
                THEN
                    RAISE EXCEPTION 'Standard fulfillment or reservation projection is incomplete'
                        USING ERRCODE = '23514';
                END IF;
            ELSE
                SELECT COUNT(*) INTO digital_required
                  FROM digital_products_digitalinventoryreservation WHERE order_id = NEW.order_id;
                IF digital_created <> digital_required OR standard_created <> 0
                   OR digital_required <> standard_required
                   OR EXISTS (
                       SELECT 1 FROM digital_products_digitalinventoryreservation r
                        WHERE r.order_id = NEW.order_id AND r.state <> 'consumed'
                   )
                THEN
                    RAISE EXCEPTION 'Digital fulfillment or reservation projection is incomplete'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER financial_core_commercial_finalization_valid
        AFTER INSERT ON financial_core_commercialfinalization
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION financial_core_validate_commercial_finalization();
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
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_paid_requires_finalization()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.collection_status = 'paid' AND OLD.collection_status <> 'paid'
               AND NOT EXISTS (
                   SELECT 1 FROM financial_core_commercialfinalization
                    WHERE payment_id = NEW.id AND order_id = NEW.order_id
               )
            THEN
                RAISE EXCEPTION 'Payment cannot become paid without commercial finalization' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_payment_paid_requires_finalization
        BEFORE UPDATE OF collection_status ON financial_core_payment
        FOR EACH ROW EXECUTE FUNCTION financial_core_paid_requires_finalization();
        """
    )


def drop_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS financial_core_payment_paid_requires_finalization ON financial_core_payment;
        DROP TRIGGER IF EXISTS financial_core_commercial_finalization_valid ON financial_core_commercialfinalization;
        DROP TRIGGER IF EXISTS financial_core_commercial_policy_protected ON financial_core_commercialaccountingpolicyversion;
        DROP TRIGGER IF EXISTS financial_core_digital_fulfillment_append_only ON financial_core_digitalfulfillmentobligation;
        DROP TRIGGER IF EXISTS financial_core_standard_fulfillment_append_only ON financial_core_standardfulfillmentobligation;
        DROP TRIGGER IF EXISTS financial_core_commercial_finalization_append_only ON financial_core_commercialfinalization;
        DROP FUNCTION IF EXISTS financial_core_paid_requires_finalization();
        DROP FUNCTION IF EXISTS financial_core_validate_commercial_finalization();
        DROP FUNCTION IF EXISTS financial_core_protect_commercial_policy();
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
            IF TG_TABLE_NAME = 'financial_core_financialallocation' THEN target_payment_id := NEW.payment_id;
            ELSE target_payment_id := NEW.id; END IF;
            SELECT amount_due, confirmed_amount, collection_status
              INTO payment_due, payment_confirmed, payment_status FROM financial_core_payment WHERE id = target_payment_id;
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
            IF payment_status = 'paid' THEN
                RAISE EXCEPTION 'Provider Execution Phase 1 cannot transition Payment to paid' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0012_commercialaccountingpolicyversion_and_more")]
    operations = [migrations.RunPython(create_guards, drop_guards)]
