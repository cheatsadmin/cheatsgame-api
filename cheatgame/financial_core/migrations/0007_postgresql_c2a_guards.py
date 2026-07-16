from django.db import migrations


APPEND_ONLY_TABLES = (
    "financial_core_paymentobligationsource",
    "financial_core_providerrequestclaim",
    "financial_core_providerrequestresult",
    "financial_core_financialoutboxmessage",
)


def create_c2a_guards(apps, schema_editor):
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
        CREATE OR REPLACE FUNCTION financial_core_protect_provider_definition()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Provider definitions cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(OLD.key, OLD.display_name) IS DISTINCT FROM ROW(NEW.key, NEW.display_name) THEN
                RAISE EXCEPTION 'Provider identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_provider_definition_protected
        BEFORE UPDATE OR DELETE ON financial_core_providerdefinition
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_provider_definition();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_provider_capability()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR ROW(OLD.*) IS DISTINCT FROM ROW(NEW.*) THEN
                RAISE EXCEPTION 'Provider capability versions are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_provider_capability_protected
        BEFORE UPDATE OR DELETE ON financial_core_providercapabilityversion
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_provider_capability();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_merchant_account_version()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Merchant-account versions cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                OLD.provider_id, OLD.capability_version_id, OLD.account_key,
                OLD.version, OLD.owner_key, OLD.credential_reference
            ) IS DISTINCT FROM ROW(
                NEW.provider_id, NEW.capability_version_id, NEW.account_key,
                NEW.version, NEW.owner_key, NEW.credential_reference
            ) THEN
                RAISE EXCEPTION 'Merchant-account version identity is immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_merchant_account_version_protected
        BEFORE UPDATE OR DELETE ON financial_core_merchantaccountversion
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_merchant_account_version();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'C2A cannot create a successful PaymentAttempt'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentAttempt cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                OLD.public_id, OLD.payment_id, OLD.sequence, OLD.requested_amount,
                OLD.currency, OLD.tender_type, OLD.provider, OLD.merchant_account_ref,
                OLD.capability_version_id, OLD.merchant_account_version_id,
                OLD.idempotency_key, OLD.request_hash
            ) IS DISTINCT FROM ROW(
                NEW.public_id, NEW.payment_id, NEW.sequence, NEW.requested_amount,
                NEW.currency, NEW.tender_type, NEW.provider, NEW.merchant_account_ref,
                NEW.capability_version_id, NEW.merchant_account_version_id,
                NEW.idempotency_key, NEW.request_hash
            ) THEN
                RAISE EXCEPTION 'PaymentAttempt identity and requested terms are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('succeeded', 'definitive_failed') AND NEW.status <> OLD.status THEN
                RAISE EXCEPTION 'terminal PaymentAttempt cannot be reopened'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded' THEN
                RAISE EXCEPTION 'C2A cannot enter successful PaymentAttempt state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER financial_core_paymentattempt_protected ON financial_core_paymentattempt;
        CREATE TRIGGER financial_core_paymentattempt_protected
        BEFORE INSERT OR UPDATE OR DELETE ON financial_core_paymentattempt
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_attempt();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.capability_version_id IS NOT NULL AND NEW.status = 'succeeded' THEN
                    RAISE EXCEPTION 'C2A cannot create a successful PaymentTransaction'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'PaymentTransaction provider operation cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF ROW(
                OLD.public_id, OLD.attempt_id, OLD.sequence, OLD.operation_type,
                OLD.parent_id, OLD.provider, OLD.merchant_account_ref,
                OLD.capability_version_id, OLD.merchant_account_version_id,
                OLD.adapter_contract_version, OLD.merchant_reference,
                OLD.amount, OLD.currency, OLD.provider_amount, OLD.provider_unit,
                OLD.provider_conversion_policy_version, OLD.provider_idempotency_reference,
                OLD.request_fingerprint, OLD.correlation_id, OLD.causation_id,
                OLD.idempotency_key
            ) IS DISTINCT FROM ROW(
                NEW.public_id, NEW.attempt_id, NEW.sequence, NEW.operation_type,
                NEW.parent_id, NEW.provider, NEW.merchant_account_ref,
                NEW.capability_version_id, NEW.merchant_account_version_id,
                NEW.adapter_contract_version, NEW.merchant_reference,
                NEW.amount, NEW.currency, NEW.provider_amount, NEW.provider_unit,
                NEW.provider_conversion_policy_version, NEW.provider_idempotency_reference,
                NEW.request_fingerprint, NEW.correlation_id, NEW.causation_id,
                NEW.idempotency_key
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
            IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded' THEN
                RAISE EXCEPTION 'C2A cannot enter successful PaymentTransaction state'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER financial_core_paymenttransaction_protected ON financial_core_paymenttransaction;
        CREATE TRIGGER financial_core_paymenttransaction_protected
        BEFORE INSERT OR UPDATE OR DELETE ON financial_core_paymenttransaction
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_transaction();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_reject_legacy_payment_owner()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM financial_core_payment p WHERE p.order_id = NEW.order_id) THEN
                RAISE EXCEPTION 'Financial Core owns this Order payment obligation'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_legacy_payment_owner_guard
        BEFORE INSERT ON shop_paymenttransaction
        FOR EACH ROW EXECUTE FUNCTION financial_core_reject_legacy_payment_owner();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_core_order()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM financial_core_payment p WHERE p.order_id = OLD.id) THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Financial Core Order cannot be deleted'
                        USING ERRCODE = '55000';
                END IF;
                IF ROW(
                    OLD.user_id, OLD.discount_id, OLD.total_price, OLD.total_price_discount,
                    OLD.schedule_id, OLD.shipping_address_id, OLD.shipping_method_id,
                    OLD.is_game, OLD.checkout_id
                ) IS DISTINCT FROM ROW(
                    NEW.user_id, NEW.discount_id, NEW.total_price, NEW.total_price_discount,
                    NEW.schedule_id, NEW.shipping_address_id, NEW.shipping_method_id,
                    NEW.is_game, NEW.checkout_id
                ) THEN
                    RAISE EXCEPTION 'Financial Core Order commercial identity is immutable'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_order_protected
        BEFORE UPDATE OR DELETE ON shop_order
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_core_order();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_core_order_item()
        RETURNS trigger AS $$
        DECLARE target_order_id bigint;
        BEGIN
            target_order_id := CASE WHEN TG_OP = 'INSERT' THEN NEW.order_id ELSE OLD.order_id END;
            IF EXISTS (SELECT 1 FROM financial_core_payment p WHERE p.order_id = target_order_id) THEN
                IF TG_OP = 'INSERT' OR TG_OP = 'DELETE' OR ROW(OLD.*) IS DISTINCT FROM ROW(NEW.*) THEN
                    RAISE EXCEPTION 'Financial Core OrderItem is immutable'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_order_item_protected
        BEFORE INSERT OR UPDATE OR DELETE ON shop_orderitem
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_core_order_item();
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_core_order_item_attachment()
        RETURNS trigger AS $$
        DECLARE target_order_item_id bigint;
        BEGIN
            target_order_item_id := CASE
                WHEN TG_OP = 'INSERT' THEN NEW.order_item_id
                ELSE OLD.order_item_id
            END;
            IF EXISTS (
                SELECT 1
                FROM shop_orderitem oi
                JOIN financial_core_payment p ON p.order_id = oi.order_id
                WHERE oi.id = target_order_item_id
            ) THEN
                RAISE EXCEPTION 'Financial Core OrderItemAttachment is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_order_item_attachment_protected
        BEFORE INSERT OR UPDATE OR DELETE ON shop_orderitemattachment
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_core_order_item_attachment();
        """
    )


def drop_c2a_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_order_item_attachment_protected "
        "ON shop_orderitemattachment;"
    )
    schema_editor.execute("DROP TRIGGER IF EXISTS financial_core_order_item_protected ON shop_orderitem;")
    schema_editor.execute("DROP TRIGGER IF EXISTS financial_core_order_protected ON shop_order;")
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_legacy_payment_owner_guard ON shop_paymenttransaction;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_merchant_account_version_protected "
        "ON financial_core_merchantaccountversion;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_provider_capability_protected "
        "ON financial_core_providercapabilityversion;"
    )
    schema_editor.execute(
        "DROP TRIGGER IF EXISTS financial_core_provider_definition_protected "
        "ON financial_core_providerdefinition;"
    )
    for table in reversed(APPEND_ONLY_TABLES):
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_core_order_item_attachment();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_core_order_item();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_core_order();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_reject_legacy_payment_owner();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_merchant_account_version();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_provider_capability();")
    schema_editor.execute("DROP FUNCTION IF EXISTS financial_core_protect_provider_definition();")
    # Restore the C1 functions for a safe reverse migration.
    schema_editor.execute(
        "DROP TRIGGER financial_core_paymentattempt_protected ON financial_core_paymentattempt;"
    )
    schema_editor.execute(
        "CREATE TRIGGER financial_core_paymentattempt_protected "
        "BEFORE UPDATE OR DELETE ON financial_core_paymentattempt "
        "FOR EACH ROW EXECUTE FUNCTION financial_core_protect_attempt();"
    )
    schema_editor.execute(
        "DROP TRIGGER financial_core_paymenttransaction_protected ON financial_core_paymenttransaction;"
    )
    schema_editor.execute(
        "CREATE TRIGGER financial_core_paymenttransaction_protected "
        "BEFORE UPDATE OR DELETE ON financial_core_paymenttransaction "
        "FOR EACH ROW EXECUTE FUNCTION financial_core_protect_transaction();"
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_attempt()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'PaymentAttempt cannot be deleted' USING ERRCODE = '55000'; END IF;
            IF ROW(OLD.public_id, OLD.payment_id, OLD.sequence, OLD.requested_amount, OLD.currency,
                   OLD.tender_type, OLD.provider, OLD.merchant_account_ref, OLD.idempotency_key, OLD.request_hash)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.payment_id, NEW.sequence, NEW.requested_amount, NEW.currency,
                   NEW.tender_type, NEW.provider, NEW.merchant_account_ref, NEW.idempotency_key, NEW.request_hash)
            THEN RAISE EXCEPTION 'PaymentAttempt identity and requested terms are immutable' USING ERRCODE = '55000'; END IF;
            IF OLD.status IN ('succeeded', 'definitive_failed') AND NEW.status <> OLD.status
            THEN RAISE EXCEPTION 'terminal PaymentAttempt cannot be reopened' USING ERRCODE = '23514'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_transaction()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'PaymentTransaction provider operation cannot be deleted' USING ERRCODE = '55000'; END IF;
            IF ROW(OLD.public_id, OLD.attempt_id, OLD.sequence, OLD.operation_type, OLD.parent_id,
                   OLD.provider, OLD.merchant_account_ref, OLD.merchant_reference, OLD.amount,
                   OLD.currency, OLD.provider_amount, OLD.provider_unit, OLD.idempotency_key)
               IS DISTINCT FROM
               ROW(NEW.public_id, NEW.attempt_id, NEW.sequence, NEW.operation_type, NEW.parent_id,
                   NEW.provider, NEW.merchant_account_ref, NEW.merchant_reference, NEW.amount,
                   NEW.currency, NEW.provider_amount, NEW.provider_unit, NEW.idempotency_key)
            THEN RAISE EXCEPTION 'PaymentTransaction provider identity and money terms are immutable' USING ERRCODE = '55000'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0006_providerrequestresult_fin_c2a_request_result_no_success")]

    operations = [migrations.RunPython(create_c2a_guards, drop_c2a_guards)]
