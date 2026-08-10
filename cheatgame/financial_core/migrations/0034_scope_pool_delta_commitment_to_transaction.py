from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION financial_core_check_api08_pool_delta()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
DECLARE matching_commitments integer;
BEGIN
    IF NEW.sellable_quantity < OLD.sellable_quantity AND EXISTS (
        SELECT 1 FROM digital_products_digitalinventoryreservation r
         WHERE r.inventory_pool_id = NEW.id AND r.order_id IS NOT NULL
           AND r.state IN ('payment_hold', 'consumed')
    ) THEN
        SELECT COUNT(*), MIN(c.finalization_id)
          INTO matching_commitments, finalization_id
          FROM financial_core_digitalinventorycommitment c
         WHERE c.inventory_pool_id = NEW.id
           AND c.pre_quantity = OLD.sellable_quantity
           AND c.post_quantity = NEW.sellable_quantity
           AND c.committed_quantity = OLD.sellable_quantity - NEW.sellable_quantity
           AND c.xmin = pg_current_xact_id()::xid;
        IF matching_commitments <> 1 THEN
            RAISE EXCEPTION 'Digital inventory delta requires one exact current-transaction API-08 commitment'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


REVERSE_SQL = r"""
CREATE OR REPLACE FUNCTION financial_core_check_api08_pool_delta()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
DECLARE matching_commitments integer;
BEGIN
    IF NEW.sellable_quantity < OLD.sellable_quantity AND EXISTS (
        SELECT 1 FROM digital_products_digitalinventoryreservation r
         WHERE r.inventory_pool_id = NEW.id AND r.order_id IS NOT NULL
           AND r.state IN ('payment_hold', 'consumed')
    ) THEN
        SELECT COUNT(*), MIN(c.finalization_id)
          INTO matching_commitments, finalization_id
          FROM financial_core_digitalinventorycommitment c
         WHERE c.inventory_pool_id = NEW.id
           AND c.pre_quantity = OLD.sellable_quantity
           AND c.post_quantity = NEW.sellable_quantity
           AND c.committed_quantity = OLD.sellable_quantity - NEW.sellable_quantity;
        IF matching_commitments <> 1 THEN
            RAISE EXCEPTION 'Digital inventory delta requires one exact API-08 commitment'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0033_preorder_deferred_fulfillment_guard")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
