from importlib import import_module

from django.db import migrations


INSTALL_SQL = r"""
CREATE OR REPLACE FUNCTION digital_fulfillment_protect_execution_identity()
RETURNS trigger AS $$
DECLARE
    capacity_value text;
    has_method_evidence boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Digital fulfillment execution cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF OLD.obligation_id IS DISTINCT FROM NEW.obligation_id
       OR OLD.public_id IS DISTINCT FROM NEW.public_id THEN
        RAISE EXCEPTION 'Digital fulfillment commercial ownership is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'completed' AND (
        NEW.status IS DISTINCT FROM OLD.status
        OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
        OR NEW.current_fulfillment_method IS DISTINCT FROM OLD.current_fulfillment_method
        OR NEW.started_at IS DISTINCT FROM OLD.started_at
        OR NEW.assigned_operator_id IS DISTINCT FROM OLD.assigned_operator_id
    ) THEN
        RAISE EXCEPTION 'Completed Digital fulfillment is permanent' USING ERRCODE = '55000';
    END IF;
    IF OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
        RAISE EXCEPTION 'Fulfillment completion time is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'Fulfillment work-start time is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IS DISTINCT FROM NEW.status AND NOT (
        (OLD.status='queued' AND NEW.status IN ('waiting_customer','exception'))
        OR (OLD.status='waiting_customer' AND NEW.status IN ('ready_for_staff','in_progress','exception'))
        OR (OLD.status='ready_for_staff' AND NEW.status IN ('in_progress','exception'))
        OR (OLD.status='in_progress' AND NEW.status IN ('waiting_confirmation','completed','exception'))
        OR (OLD.status='waiting_confirmation' AND NEW.status IN ('completed','exception'))
        OR (OLD.status='exception' AND NEW.status IN ('queued','waiting_customer','ready_for_staff'))
    ) THEN
        RAISE EXCEPTION 'Illegal Digital fulfillment transition' USING ERRCODE = '23514';
    END IF;
    IF OLD.current_fulfillment_method IS DISTINCT FROM NEW.current_fulfillment_method THEN
        IF OLD.status NOT IN ('queued','waiting_customer') OR NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'Fulfillment method cannot change after operational work begins' USING ERRCODE = '23514';
        END IF;
        SELECT EXISTS(
            SELECT 1 FROM digital_products_fulfillmentactivity
             WHERE fulfillment_item_id=OLD.id
               AND activity_type IN ('console_received','work_started','installation_performed','remote_handling_performed')
        ) INTO has_method_evidence;
        IF has_method_evidence THEN
            RAISE EXCEPTION 'Fulfillment method conflicts with existing evidence' USING ERRCODE = '23514';
        END IF;
        SELECT snapshot.capacity INTO capacity_value
          FROM financial_core_digitalfulfillmentobligation obligation
          JOIN digital_products_digitalcheckoutlinesnapshot snapshot
            ON snapshot.checkout_line_id=obligation.checkout_line_id
         WHERE obligation.id=OLD.obligation_id;
        IF capacity_value='capacity_1' AND NEW.current_fulfillment_method <> 'in_store' THEN
            RAISE EXCEPTION 'Capacity 1 requires in-store fulfillment' USING ERRCODE = '23514';
        END IF;
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
    IF NEW.status NOT IN ('pending_fulfillment','active') THEN
        RAISE EXCEPTION 'Unsupported Digital entitlement state' USING ERRCODE = '23514';
    END IF;
    IF OLD.status='active' AND (
        NEW.status IS DISTINCT FROM OLD.status OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
    ) THEN
        RAISE EXCEPTION 'Active Digital entitlement is permanent' USING ERRCODE = '55000';
    END IF;
    IF OLD.activated_at IS NOT NULL AND NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
        RAISE EXCEPTION 'Entitlement activation time is immutable' USING ERRCODE = '55000';
    END IF;
    IF OLD.status IS DISTINCT FROM NEW.status
       AND NOT (OLD.status='pending_fulfillment' AND NEW.status='active') THEN
        RAISE EXCEPTION 'Illegal Digital entitlement transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION digital_fulfillment_validate_actor_authority()
RETURNS trigger AS $$
DECLARE
    owner_id bigint;
    assigned_id bigint;
    actor_kind integer;
    actor_active boolean;
BEGIN
    SELECT o.user_id, execution.assigned_operator_id
      INTO owner_id, assigned_id
      FROM digital_products_digitalfulfillmentitem execution
      JOIN financial_core_digitalfulfillmentobligation obligation ON obligation.id=execution.obligation_id
      JOIN shop_order o ON o.id=obligation.order_id
     WHERE execution.id=NEW.fulfillment_item_id;
    IF NEW.actor_id IS NOT NULL THEN
        SELECT user_type, is_active INTO actor_kind, actor_active
          FROM users_baseuser WHERE id=NEW.actor_id;
    END IF;
    IF NEW.actor_type='system' THEN
        IF NEW.actor_id IS NOT NULL OR NEW.actor_authority <> 'system' OR NEW.activity_type <> 'provisioned' THEN
            RAISE EXCEPTION 'Invalid system fulfillment authority' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.actor_type='customer' THEN
        IF NEW.actor_id IS DISTINCT FROM owner_id OR actor_kind <> 1 OR NOT actor_active
           OR NEW.actor_authority <> 'customer_owner'
           OR NOT (
               NEW.activity_type='customer_confirmed'
               OR (NEW.activity_type='status_changed' AND NEW.new_status='completed')
           ) THEN
            RAISE EXCEPTION 'Invalid customer fulfillment authority' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.actor_type='staff' THEN
        IF NEW.actor_id IS NULL OR actor_kind NOT IN (2,3) OR NOT actor_active THEN
            RAISE EXCEPTION 'Invalid staff fulfillment actor' USING ERRCODE = '23514';
        END IF;
        IF NOT (
            (NEW.actor_authority='assigned_operator' AND assigned_id=NEW.actor_id)
            OR (NEW.actor_authority='unassigned_staff' AND assigned_id IS NULL)
            OR (NEW.actor_authority='admin_override' AND actor_kind=2 AND assigned_id IS NOT NULL AND assigned_id<>NEW.actor_id)
        ) THEN
            RAISE EXCEPTION 'Invalid staff fulfillment authority' USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'Unknown fulfillment actor type' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION digital_fulfillment_validate_installed_identity()
RETURNS trigger AS $$
DECLARE
    frozen_product bigint;
    frozen_version bigint;
    execution_status text;
    assigned_id bigint;
    actor_kind integer;
    actor_active boolean;
    previous_row record;
    current_count integer;
BEGIN
    SELECT snapshot.product_id, snapshot.delivered_version_id, execution.status, execution.assigned_operator_id
      INTO frozen_product, frozen_version, execution_status, assigned_id
      FROM digital_products_digitalfulfillmentitem execution
      JOIN financial_core_digitalfulfillmentobligation obligation ON obligation.id=execution.obligation_id
      JOIN digital_products_digitalcheckoutlinesnapshot snapshot ON snapshot.checkout_line_id=obligation.checkout_line_id
     WHERE execution.id=NEW.fulfillment_item_id;
    IF NEW.operator_id IS NULL THEN
        RAISE EXCEPTION 'Installation evidence requires a staff actor' USING ERRCODE = '23514';
    END IF;
    SELECT user_type, is_active INTO actor_kind, actor_active FROM users_baseuser WHERE id=NEW.operator_id;
    IF actor_kind NOT IN (2,3) OR NOT actor_active OR NOT (
        (NEW.actor_authority='assigned_operator' AND assigned_id=NEW.operator_id)
        OR (NEW.actor_authority='unassigned_staff' AND assigned_id IS NULL)
        OR (NEW.actor_authority='admin_override' AND actor_kind=2 AND assigned_id IS NOT NULL AND assigned_id<>NEW.operator_id)
    ) THEN
        RAISE EXCEPTION 'Installation evidence actor is unauthorized' USING ERRCODE = '23514';
    END IF;
    IF NEW.classification='purchased' THEN
        IF execution_status='completed' THEN
            RAISE EXCEPTION 'Purchased evidence cannot change after completion' USING ERRCODE = '55000';
        END IF;
        IF NEW.game_id IS DISTINCT FROM frozen_product OR NEW.delivered_version_id IS DISTINCT FROM frozen_version THEN
            RAISE EXCEPTION 'Purchased installation must match frozen snapshot' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.corrects_id IS NOT NULL THEN
        SELECT * INTO previous_row FROM digital_products_installedgamerecord WHERE id=NEW.corrects_id;
        IF NOT FOUND OR previous_row.fulfillment_item_id<>NEW.fulfillment_item_id
           OR previous_row.classification<>NEW.classification THEN
            RAISE EXCEPTION 'Evidence supersession must remain within one execution and classification' USING ERRCODE = '23514';
        END IF;
        IF previous_row.state<>'recorded' THEN
            RAISE EXCEPTION 'Removed evidence is terminal and cannot be superseded' USING ERRCODE = '23514';
        END IF;
        IF EXISTS(SELECT 1 FROM digital_products_installedgamerecord WHERE corrects_id=NEW.corrects_id) THEN
            RAISE EXCEPTION 'Evidence already has an immutable successor' USING ERRCODE = '23514';
        END IF;
        IF NEW.corrects_id=NEW.id THEN
            RAISE EXCEPTION 'Evidence correction cycle is prohibited' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.state='removed' THEN
        RAISE EXCEPTION 'Removed evidence must supersede an existing record' USING ERRCODE = '23514';
    END IF;
    IF NEW.classification='purchased' THEN
        SELECT count(*) INTO current_count
          FROM digital_products_installedgamerecord candidate
         WHERE candidate.fulfillment_item_id=NEW.fulfillment_item_id
           AND candidate.classification='purchased'
           AND candidate.state='recorded'
           AND NOT EXISTS(
               SELECT 1 FROM digital_products_installedgamerecord successor
                WHERE successor.corrects_id=candidate.id
           );
        IF NEW.state='recorded' THEN
            current_count := current_count + 1;
        END IF;
        IF NEW.corrects_id IS NOT NULL THEN
            IF previous_row.state='recorded' THEN
                current_count := current_count - 1;
            END IF;
        END IF;
        IF current_count > 1 THEN
            RAISE EXCEPTION 'Only one effective current purchased evidence is permitted' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION digital_fulfillment_validate_graph_for_item(item_id bigint)
RETURNS void AS $$
DECLARE
    execution record;
    owner_id bigint;
    entitlement_row record;
    purchased_row record;
    purchased_id bigint;
    purchased_count integer;
    initial_count integer;
    has_status_projection boolean;
    has_work boolean;
    has_console boolean;
    has_remote boolean;
    has_customer_completion boolean;
    has_staff_completion boolean;
BEGIN
    SELECT * INTO execution FROM digital_products_digitalfulfillmentitem WHERE id=item_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT o.user_id INTO owner_id
      FROM financial_core_digitalfulfillmentobligation obligation
      JOIN shop_order o ON o.id=obligation.order_id
     WHERE obligation.id=execution.obligation_id;
    SELECT * INTO entitlement_row FROM digital_products_entitlement
     WHERE obligation_id=execution.obligation_id;
    IF NOT FOUND OR entitlement_row.fulfillment_item_id<>execution.id OR entitlement_row.customer_id<>owner_id THEN
        RAISE EXCEPTION 'Digital operational ownership graph is incomplete or contradictory' USING ERRCODE = '23514';
    END IF;
    SELECT count(*) INTO initial_count FROM digital_products_fulfillmentactivity
     WHERE fulfillment_item_id=execution.id AND activity_type='provisioned'
       AND actor_type='system' AND actor_authority='system';
    IF initial_count<>1 THEN
        RAISE EXCEPTION 'Digital execution requires exactly one provisioning activity' USING ERRCODE = '23514';
    END IF;
    IF execution.status<>'queued' THEN
        SELECT EXISTS(
            SELECT 1 FROM digital_products_fulfillmentactivity
             WHERE fulfillment_item_id=execution.id AND activity_type='status_changed'
               AND new_status=execution.status
               AND actor_authority IN ('assigned_operator','unassigned_staff','admin_override','customer_owner')
        ) INTO has_status_projection;
        IF NOT has_status_projection THEN
            RAISE EXCEPTION 'Execution state lacks an authorized transition activity' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF execution.status='completed' THEN
        IF entitlement_row.status<>'active' OR entitlement_row.activated_at IS NULL THEN
            RAISE EXCEPTION 'Completed fulfillment requires its active entitlement' USING ERRCODE = '23514';
        END IF;
        SELECT count(*), min(candidate.id) INTO purchased_count, purchased_id
          FROM digital_products_installedgamerecord candidate
         WHERE candidate.fulfillment_item_id=execution.id
           AND candidate.classification='purchased' AND candidate.state='recorded'
           AND NOT EXISTS(
               SELECT 1 FROM digital_products_installedgamerecord successor WHERE successor.corrects_id=candidate.id
           );
        IF purchased_count=1 THEN
            SELECT * INTO purchased_row FROM digital_products_installedgamerecord WHERE id=purchased_id;
        END IF;
        SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='work_started' AND actor_type='staff' AND actor_authority IN ('assigned_operator','unassigned_staff','admin_override')) INTO has_work;
        SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='console_received' AND actor_type='staff' AND actor_authority IN ('assigned_operator','unassigned_staff','admin_override')) INTO has_console;
        SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='remote_handling_performed' AND actor_type='staff' AND actor_authority IN ('assigned_operator','unassigned_staff','admin_override')) INTO has_remote;
        SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='customer_confirmed' AND actor_type='customer' AND actor_authority='customer_owner') INTO has_customer_completion;
        SELECT EXISTS(SELECT 1 FROM digital_products_fulfillmentactivity WHERE fulfillment_item_id=execution.id AND activity_type='staff_verified' AND actor_type='staff' AND actor_authority IN ('assigned_operator','unassigned_staff','admin_override')) INTO has_staff_completion;
        IF purchased_count<>1 OR NOT has_work OR NOT (has_customer_completion OR has_staff_completion) THEN
            RAISE EXCEPTION 'Completed fulfillment lacks authorized current evidence' USING ERRCODE = '23514';
        END IF;
        IF execution.current_fulfillment_method='in_store' THEN
            IF NOT has_console OR purchased_row.completion_source<>'staff_installed' OR NOT has_staff_completion THEN
                RAISE EXCEPTION 'In-store completion lacks authorized evidence' USING ERRCODE = '23514';
            END IF;
        ELSIF execution.current_fulfillment_method='remote' THEN
            IF NOT has_remote OR purchased_row.completion_source<>'staff_verified_remote'
               OR NOT (has_customer_completion OR has_staff_completion) THEN
                RAISE EXCEPTION 'Remote completion lacks authorized evidence' USING ERRCODE = '23514';
            END IF;
        ELSE
            RAISE EXCEPTION 'Unknown fulfillment method' USING ERRCODE = '23514';
        END IF;
    ELSIF entitlement_row.status='active' THEN
        RAISE EXCEPTION 'Active entitlement requires completed fulfillment' USING ERRCODE = '23514';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION digital_fulfillment_validate_activity_graph_trigger()
RETURNS trigger AS $$
BEGIN
    PERFORM digital_fulfillment_validate_graph_for_item(NEW.fulfillment_item_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION digital_fulfillment_validate_installed_graph_trigger()
RETURNS trigger AS $$
BEGIN
    PERFORM digital_fulfillment_validate_graph_for_item(NEW.fulfillment_item_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS digital_fulfillment_activity_authority_guard ON digital_products_fulfillmentactivity;
CREATE TRIGGER digital_fulfillment_activity_authority_guard
BEFORE INSERT ON digital_products_fulfillmentactivity
FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_actor_authority();

DROP TRIGGER IF EXISTS digital_fulfillment_installed_identity_guard ON digital_products_installedgamerecord;
CREATE TRIGGER digital_fulfillment_installed_identity_guard
BEFORE INSERT ON digital_products_installedgamerecord
FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_installed_identity();

DROP TRIGGER IF EXISTS digital_fulfillment_activity_graph_guard ON digital_products_fulfillmentactivity;
CREATE CONSTRAINT TRIGGER digital_fulfillment_activity_graph_guard
AFTER INSERT ON digital_products_fulfillmentactivity
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_activity_graph_trigger();

DROP TRIGGER IF EXISTS digital_fulfillment_installed_graph_guard ON digital_products_installedgamerecord;
CREATE CONSTRAINT TRIGGER digital_fulfillment_installed_graph_guard
AFTER INSERT ON digital_products_installedgamerecord
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION digital_fulfillment_validate_installed_graph_trigger();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS digital_fulfillment_installed_graph_guard ON digital_products_installedgamerecord;
DROP TRIGGER IF EXISTS digital_fulfillment_activity_graph_guard ON digital_products_fulfillmentactivity;
DROP TRIGGER IF EXISTS digital_fulfillment_activity_authority_guard ON digital_products_fulfillmentactivity;
DROP FUNCTION IF EXISTS digital_fulfillment_validate_installed_graph_trigger();
DROP FUNCTION IF EXISTS digital_fulfillment_validate_activity_graph_trigger();
DROP FUNCTION IF EXISTS digital_fulfillment_validate_actor_authority();
"""


def restore_previous_guards(apps, schema_editor):
    """Restore the exact 0007 guard set before 0010 reverses its columns."""
    if schema_editor.connection.vendor != "postgresql":
        return
    previous = import_module(
        "cheatgame.digital_products.migrations.0007_postgresql_fulfillment_ownership_guards"
    )
    previous.remove_guards(apps, schema_editor)
    previous.install_guards(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("digital_products", "0010_fulfillment_integrity_schema"),
        ("financial_core", "0015_postgresql_finalized_projection_guards"),
    ]
    operations = [
        migrations.RunSQL(INSTALL_SQL, REVERSE_SQL),
        migrations.RunPython(migrations.RunPython.noop, restore_previous_guards),
    ]
