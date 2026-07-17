from django.db import migrations


def install_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        r"""
        CREATE OR REPLACE FUNCTION digital_fulfillment_protect_execution_identity()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Digital fulfillment execution cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF OLD.obligation_id IS DISTINCT FROM NEW.obligation_id
               OR OLD.public_id IS DISTINCT FROM NEW.public_id THEN
                RAISE EXCEPTION 'Digital fulfillment commercial ownership is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_protect_entitlement_identity()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'Digital entitlement cannot be deleted' USING ERRCODE = '55000';
            END IF;
            IF OLD.obligation_id IS DISTINCT FROM NEW.obligation_id
               OR OLD.fulfillment_item_id IS DISTINCT FROM NEW.fulfillment_item_id
               OR OLD.customer_id IS DISTINCT FROM NEW.customer_id THEN
                RAISE EXCEPTION 'Digital entitlement ownership is immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'Digital fulfillment evidence is append-only' USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_validate_graph_for_item(item_id bigint)
        RETURNS void AS $$
        DECLARE
            execution record;
            owner_id bigint;
            entitlement_row record;
            purchased_count integer;
            has_work boolean;
            has_console boolean;
            has_remote boolean;
            has_completion boolean;
            has_entitlement boolean;
        BEGIN
            SELECT * INTO execution FROM digital_products_digitalfulfillmentitem WHERE id = item_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT o.user_id INTO owner_id
              FROM financial_core_digitalfulfillmentobligation obligation
              JOIN shop_order o ON o.id = obligation.order_id
             WHERE obligation.id = execution.obligation_id;
            IF owner_id IS NULL THEN
                RAISE EXCEPTION 'Digital fulfillment obligation owner is missing' USING ERRCODE = '23514';
            END IF;
            SELECT * INTO entitlement_row FROM digital_products_entitlement
             WHERE obligation_id = execution.obligation_id;
            has_entitlement := FOUND;
            IF has_entitlement AND (entitlement_row.fulfillment_item_id <> execution.id
                          OR entitlement_row.customer_id <> owner_id) THEN
                RAISE EXCEPTION 'Digital entitlement ownership graph is contradictory' USING ERRCODE = '23514';
            END IF;
            IF execution.status = 'completed' THEN
                IF NOT has_entitlement OR entitlement_row.status <> 'active' OR entitlement_row.activated_at IS NULL THEN
                    RAISE EXCEPTION 'Completed fulfillment requires its active entitlement' USING ERRCODE = '23514';
                END IF;
                SELECT count(*) INTO purchased_count FROM digital_products_installedgamerecord
                 WHERE fulfillment_item_id = execution.id AND classification = 'purchased' AND state = 'recorded';
                SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='work_started') INTO has_work;
                SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='console_received') INTO has_console;
                SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='remote_handling_performed') INTO has_remote;
                SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type IN ('customer_confirmed','staff_verified')) INTO has_completion;
                IF purchased_count <> 1 OR NOT has_work OR NOT has_completion THEN
                    RAISE EXCEPTION 'Completed fulfillment lacks required evidence' USING ERRCODE = '23514';
                END IF;
                IF execution.current_fulfillment_method='in_store' AND NOT has_console THEN
                    RAISE EXCEPTION 'In-store completion requires console receipt' USING ERRCODE = '23514';
                END IF;
                IF execution.current_fulfillment_method='remote' AND NOT has_remote THEN
                    RAISE EXCEPTION 'Remote completion requires remote handling evidence' USING ERRCODE = '23514';
                END IF;
            ELSIF has_entitlement AND entitlement_row.status = 'active' THEN
                RAISE EXCEPTION 'Active entitlement requires completed fulfillment' USING ERRCODE = '23514';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_validate_item_trigger()
        RETURNS trigger AS $$
        BEGIN
            PERFORM digital_fulfillment_validate_graph_for_item(NEW.id);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_validate_entitlement_trigger()
        RETURNS trigger AS $$
        BEGIN
            PERFORM digital_fulfillment_validate_graph_for_item(NEW.fulfillment_item_id);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION digital_fulfillment_validate_installed_identity()
        RETURNS trigger AS $$
        DECLARE
            frozen_product bigint;
            frozen_version bigint;
        BEGIN
            IF NEW.classification = 'purchased' THEN
                SELECT snapshot.product_id, snapshot.delivered_version_id
                  INTO frozen_product, frozen_version
                  FROM digital_products_digitalfulfillmentitem execution
                  JOIN financial_core_digitalfulfillmentobligation obligation ON obligation.id=execution.obligation_id
                  JOIN digital_products_digitalcheckoutlinesnapshot snapshot ON snapshot.checkout_line_id=obligation.checkout_line_id
                 WHERE execution.id=NEW.fulfillment_item_id;
                IF NEW.game_id IS DISTINCT FROM frozen_product OR NEW.delivered_version_id IS DISTINCT FROM frozen_version THEN
                    RAISE EXCEPTION 'Purchased installation must match frozen snapshot' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER digital_fulfillment_execution_identity_guard
        BEFORE UPDATE OR DELETE ON digital_products_digitalfulfillmentitem
        FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_protect_execution_identity();
        CREATE TRIGGER digital_fulfillment_entitlement_identity_guard
        BEFORE UPDATE OR DELETE ON digital_products_entitlement
        FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_protect_entitlement_identity();
        CREATE TRIGGER digital_fulfillment_activity_append_only_guard
        BEFORE UPDATE OR DELETE ON digital_products_fulfillmentactivity
        FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_append_only();
        CREATE TRIGGER digital_fulfillment_installed_append_only_guard
        BEFORE UPDATE OR DELETE ON digital_products_installedgamerecord
        FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_append_only();
        CREATE CONSTRAINT TRIGGER digital_fulfillment_item_graph_guard
        AFTER INSERT OR UPDATE ON digital_products_digitalfulfillmentitem
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_item_trigger();
        CREATE CONSTRAINT TRIGGER digital_fulfillment_entitlement_graph_guard
        AFTER INSERT OR UPDATE ON digital_products_entitlement
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_entitlement_trigger();
        CREATE CONSTRAINT TRIGGER digital_fulfillment_installed_identity_guard
        AFTER INSERT ON digital_products_installedgamerecord
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_installed_identity();
        """
    )


def remove_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS digital_fulfillment_installed_identity_guard ON digital_products_installedgamerecord;
        DROP TRIGGER IF EXISTS digital_fulfillment_entitlement_graph_guard ON digital_products_entitlement;
        DROP TRIGGER IF EXISTS digital_fulfillment_item_graph_guard ON digital_products_digitalfulfillmentitem;
        DROP TRIGGER IF EXISTS digital_fulfillment_installed_append_only_guard ON digital_products_installedgamerecord;
        DROP TRIGGER IF EXISTS digital_fulfillment_activity_append_only_guard ON digital_products_fulfillmentactivity;
        DROP TRIGGER IF EXISTS digital_fulfillment_entitlement_identity_guard ON digital_products_entitlement;
        DROP TRIGGER IF EXISTS digital_fulfillment_execution_identity_guard ON digital_products_digitalfulfillmentitem;
        DROP FUNCTION IF EXISTS digital_fulfillment_validate_installed_identity();
        DROP FUNCTION IF EXISTS digital_fulfillment_validate_entitlement_trigger();
        DROP FUNCTION IF EXISTS digital_fulfillment_validate_item_trigger();
        DROP FUNCTION IF EXISTS digital_fulfillment_validate_graph_for_item(bigint);
        DROP FUNCTION IF EXISTS digital_fulfillment_append_only();
        DROP FUNCTION IF EXISTS digital_fulfillment_protect_entitlement_identity();
        DROP FUNCTION IF EXISTS digital_fulfillment_protect_execution_identity();
        """
    )


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0006_digitalfulfillmentitem_installedgamerecord_and_more"),
        ("financial_core", "0015_postgresql_finalized_projection_guards"),
    ]
    operations = [migrations.RunPython(install_guards, remove_guards)]
