from django.db import migrations


FORWARD_SQL = r"""
CREATE UNIQUE INDEX fin_satisfaction_standard_launch_once
ON financial_core_satisfactionevidence (standard_fulfillment_obligation_id)
WHERE evidence_contract_version = 'STANDARD_DELIVERY_COMPLETED';

CREATE UNIQUE INDEX fin_satisfaction_digital_launch_once
ON financial_core_satisfactionevidence (digital_fulfillment_obligation_id)
WHERE evidence_contract_version = 'DIGITAL_FULFILLMENT_COMPLETED';

CREATE FUNCTION financial_core_validate_launch_satisfaction_evidence()
RETURNS trigger AS $$
DECLARE
    obligation_row record;
    fulfillment_row record;
    execution_row record;
    snapshot_row record;
    component_count integer;
    purchased_count integer;
BEGIN
    IF NEW.evidence_contract_version NOT IN (
        'STANDARD_DELIVERY_COMPLETED', 'DIGITAL_FULFILLMENT_COMPLETED'
    ) THEN
        RETURN NEW;
    END IF;

    SELECT * INTO STRICT obligation_row
      FROM financial_core_performanceobligation WHERE id = NEW.obligation_id;
    IF NEW.evidence_classification <> 'point_in_time_satisfied'
       OR NEW.evidence_authority <> 'staff'
       OR NEW.actor_type <> 'admin'
       OR NEW.actor_id IS NULL
       OR NEW.source_event_version <> 1
       OR NEW.progress_numerator IS NOT NULL
       OR NEW.progress_denominator IS NOT NULL
       OR NEW.corrects_id IS NOT NULL
       OR NEW.contradicts_id IS NOT NULL
       OR NEW.satisfied_quantity <> obligation_row.quantity_basis
       OR NOT EXISTS (
            SELECT 1 FROM users_baseuser u
             WHERE u.id = NEW.actor_id AND u.is_active AND u.user_type IN (2, 3)
       ) THEN
        RAISE EXCEPTION 'launch satisfaction authority is incoherent' USING ERRCODE = '23514';
    END IF;

    IF NEW.evidence_contract_version = 'STANDARD_DELIVERY_COMPLETED' THEN
        SELECT * INTO STRICT fulfillment_row
          FROM financial_core_standardfulfillmentobligation
         WHERE id = NEW.standard_fulfillment_obligation_id;
        SELECT count(*) INTO component_count
          FROM financial_core_performanceobligationcomponent c
         WHERE c.obligation_id = NEW.obligation_id
           AND c.standard_fulfillment_obligation_id = fulfillment_row.id
           AND c.digital_fulfillment_obligation_id IS NULL
           AND c.order_id = fulfillment_row.order_id
           AND c.order_item_id = fulfillment_row.order_item_id
           AND c.quantity = fulfillment_row.quantity;
        IF NEW.digital_fulfillment_obligation_id IS NOT NULL
           OR NEW.source_domain <> 'standard_fulfillment'
           OR NEW.source_aggregate_type <> 'standard_fulfillment_obligation'
           OR NEW.source_aggregate_id <> fulfillment_row.public_id::text
           OR NEW.source_event_id <> fulfillment_row.public_id::text
           OR obligation_row.finalization_id <> fulfillment_row.finalization_id
           OR obligation_row.order_id <> fulfillment_row.order_id
           OR obligation_row.obligation_type <> 'physical_good'
           OR obligation_row.commerce_authority <> 'standard_commerce'
           OR NEW.actor_id = (SELECT o.user_id FROM shop_order o WHERE o.id = fulfillment_row.order_id)
           OR component_count <> 1 THEN
            RAISE EXCEPTION 'standard launch satisfaction lineage is incoherent' USING ERRCODE = '23514';
        END IF;
    ELSE
        SELECT * INTO STRICT fulfillment_row
          FROM financial_core_digitalfulfillmentobligation
         WHERE id = NEW.digital_fulfillment_obligation_id;
        SELECT * INTO STRICT execution_row
          FROM digital_products_digitalfulfillmentitem
         WHERE obligation_id = fulfillment_row.id
           AND public_id::text = NEW.source_aggregate_id;
        SELECT * INTO STRICT snapshot_row
          FROM digital_products_digitalcheckoutlinesnapshot
         WHERE checkout_line_id = fulfillment_row.checkout_line_id;
        SELECT count(*) INTO component_count
          FROM financial_core_performanceobligationcomponent c
         WHERE c.obligation_id = NEW.obligation_id
           AND c.digital_fulfillment_obligation_id = fulfillment_row.id
           AND c.standard_fulfillment_obligation_id IS NULL
           AND c.order_id = fulfillment_row.order_id
           AND c.order_item_id = fulfillment_row.order_item_id
           AND c.checkout_line_id = fulfillment_row.checkout_line_id
           AND c.quantity = fulfillment_row.quantity;
        SELECT count(*) INTO purchased_count
          FROM digital_products_installedgamerecord r
         WHERE r.fulfillment_item_id = execution_row.id
           AND r.classification = 'purchased'
           AND r.state = 'recorded'
           AND r.operator_id = NEW.actor_id
           AND r.game_id = snapshot_row.product_id
           AND r.delivered_version_id = snapshot_row.delivered_version_id
           AND NOT EXISTS (
                SELECT 1 FROM digital_products_installedgamerecord successor
                 WHERE successor.corrects_id = r.id
           );
        IF NEW.standard_fulfillment_obligation_id IS NOT NULL
           OR NEW.source_domain <> 'digital_fulfillment'
           OR NEW.source_aggregate_type <> 'digital_fulfillment_item'
           OR NEW.source_event_id <> execution_row.public_id::text
           OR execution_row.status <> 'completed'
           OR execution_row.completed_at IS NULL
           OR NEW.occurred_at <> execution_row.completed_at
           OR obligation_row.finalization_id <> fulfillment_row.finalization_id
           OR obligation_row.order_id <> fulfillment_row.order_id
           OR obligation_row.obligation_type <> 'digital_access_installation'
           OR obligation_row.commerce_authority <> 'digital_products'
           OR snapshot_row.inventory_pool_id <> fulfillment_row.inventory_pool_id
           OR NOT EXISTS (
                SELECT 1 FROM digital_products_entitlement e
                 WHERE e.obligation_id = fulfillment_row.id
                   AND e.fulfillment_item_id = execution_row.id
                   AND e.customer_id = (SELECT o.user_id FROM shop_order o WHERE o.id = fulfillment_row.order_id)
                   AND e.status = 'active' AND e.activated_at IS NOT NULL
           )
           OR component_count <> 1
           OR purchased_count <> 1 THEN
            RAISE EXCEPTION 'digital launch satisfaction lineage is incoherent' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER financial_core_launch_satisfaction_guard
BEFORE INSERT ON financial_core_satisfactionevidence
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_launch_satisfaction_evidence();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_launch_satisfaction_guard
ON financial_core_satisfactionevidence;
DROP FUNCTION IF EXISTS financial_core_validate_launch_satisfaction_evidence();
DROP INDEX IF EXISTS fin_satisfaction_digital_launch_once;
DROP INDEX IF EXISTS fin_satisfaction_standard_launch_once;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0025_postgresql_commercial_finalization_v2_guards"),
        ("digital_products", "0011_postgresql_fulfillment_integrity_hardening"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
