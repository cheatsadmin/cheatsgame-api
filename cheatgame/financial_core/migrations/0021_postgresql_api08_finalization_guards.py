from django.db import migrations


FORWARD_SQL = r"""
CREATE TRIGGER financial_core_standard_commitment_append_only
BEFORE UPDATE OR DELETE ON financial_core_standardinventorycommitment
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

CREATE TRIGGER financial_core_digital_commitment_append_only
BEFORE UPDATE OR DELETE ON financial_core_digitalinventorycommitment
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

CREATE TRIGGER financial_core_finalization_outbox_append_only
BEFORE UPDATE OR DELETE ON financial_core_financialoutboxmessage
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

CREATE OR REPLACE FUNCTION financial_core_validate_api08_finalization(p_finalization_id bigint)
RETURNS void AS $$
DECLARE
    target record;
    checkout_cart_id bigint;
    checkout_state varchar;
    cart_state varchar;
    cart_active_checkout_id bigint;
    required_resources integer;
    created_resources integer;
BEGIN
    SELECT f.*, p.collection_status, p.confirmed_amount, p.amount_due,
           o.payment_status, o.fulfillment_status, o.checkout_id
      INTO STRICT target
      FROM financial_core_commercialfinalization f
      JOIN financial_core_payment p ON p.id = f.payment_id
      JOIN shop_order o ON o.id = f.order_id
     WHERE f.id = p_finalization_id;

    SELECT c.cart_id, c.status INTO checkout_cart_id, checkout_state
      FROM shop_checkout c WHERE c.id = target.checkout_id;
    SELECT state, active_checkout_id INTO cart_state, cart_active_checkout_id
      FROM shop_cart WHERE id = checkout_cart_id;

    IF target.collection_status <> 'paid'
       OR target.confirmed_amount <> target.amount_due
       OR target.payment_status <> 3
       OR target.fulfillment_status <> 'processing'
       OR checkout_state <> 'paid'
       OR cart_state <> 'open'
       OR cart_active_checkout_id IS NOT NULL
    THEN
        RAISE EXCEPTION 'API-08 terminal commercial projection is incomplete' USING ERRCODE = '23514';
    END IF;

    IF (SELECT COUNT(*) FROM financial_core_commercialfinalizationworkitem w
         WHERE w.payment_id = target.payment_id
           AND w.finalizer_version = 'commercial-finalizer-v1-dormant'
           AND w.status = 'completed') <> 1 THEN
        RAISE EXCEPTION 'API-08 finalization requires one completed compatible work item'
            USING ERRCODE = '23514';
    END IF;

    IF (SELECT COUNT(*) FROM financial_core_financialoutboxmessage m
         WHERE m.topic = 'commercial.fulfillment.requested'
           AND m.aggregate_type = 'financial_core.commercialfinalization'
           AND m.aggregate_id = target.public_id::text) <> 1 THEN
        RAISE EXCEPTION 'API-08 finalization requires one fulfillment outbox message'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM financial_core_financialoutboxmessage m
          JOIN shop_order o ON o.id = target.order_id
         WHERE m.topic = 'commercial.fulfillment.requested'
           AND m.aggregate_type = 'financial_core.commercialfinalization'
           AND m.aggregate_id = target.public_id::text
           AND (
               m.idempotency_key <> 'outbox:commercial-fulfillment:' || target.public_id::text
                                      || ':commercial-finalizer-api08-v1'
               OR m.correlation_id <> target.correlation_id
               OR m.causation_id <> COALESCE(target.causation_id, target.public_id)
               OR m.safe_payload <> jsonb_build_object(
                   'event_type', 'commercial.fulfillment.requested',
                   'commercial_finalization_public_id', target.public_id::text,
                   'order_public_id', o.public_tracking_code::text,
                   'commerce_authority', target.commerce_authority,
                   'finalizer_contract_version', 'commercial-finalizer-api08-v1',
                   'correlation_id', target.correlation_id::text,
                   'causation_id', COALESCE(target.causation_id, target.public_id)::text
               )
           )
    ) THEN
        RAISE EXCEPTION 'API-08 fulfillment outbox identity or payload is inconsistent'
            USING ERRCODE = '23514';
    END IF;

    IF (
        SELECT COUNT(*) FROM financial_core_journalposting jp
         WHERE jp.entry_id = target.journal_entry_id
    ) <> (
        1
        + CASE WHEN target.merchandise_amount > 0 THEN 1 ELSE 0 END
        + CASE WHEN target.shipping_amount > 0 THEN 1 ELSE 0 END
    ) OR (
        SELECT COUNT(*)
          FROM financial_core_journalposting jp
          JOIN financial_core_commercialaccountingpolicyversion policy
            ON policy.id = target.accounting_policy_version_id
         WHERE jp.entry_id = target.journal_entry_id
           AND jp.account_id = policy.customer_unapplied_funds_account_id
           AND jp.direction = 'debit' AND jp.amount = target.amount AND jp.currency = 'IRR'
    ) <> 1 OR (
        SELECT COUNT(*)
          FROM financial_core_journalposting jp
          JOIN financial_core_commercialaccountingpolicyversion policy
            ON policy.id = target.accounting_policy_version_id
         WHERE jp.entry_id = target.journal_entry_id
           AND jp.account_id = policy.merchandise_revenue_account_id
           AND jp.direction = 'credit' AND jp.amount = target.merchandise_amount AND jp.currency = 'IRR'
    ) <> (CASE WHEN target.merchandise_amount > 0 THEN 1 ELSE 0 END) OR (
        SELECT COUNT(*)
          FROM financial_core_journalposting jp
          JOIN financial_core_commercialaccountingpolicyversion policy
            ON policy.id = target.accounting_policy_version_id
         WHERE jp.entry_id = target.journal_entry_id
           AND jp.account_id = policy.shipping_revenue_account_id
           AND jp.direction = 'credit' AND jp.amount = target.shipping_amount AND jp.currency = 'IRR'
    ) <> (CASE WHEN target.shipping_amount > 0 THEN 1 ELSE 0 END)
    THEN
        RAISE EXCEPTION 'API-08 commercial Journal contains an unexpected posting set'
            USING ERRCODE = '23514';
    END IF;

    IF EXISTS (
        SELECT 1 FROM financial_core_reviewcase r
         WHERE r.payment_id = target.payment_id
           AND r.reason = 'paid_pending_finalization'
           AND r.opened_by_type = 'system'
           AND r.opened_by_id IS NULL
           AND r.status IN ('open', 'investigating', 'approval_pending')
    ) THEN
        RAISE EXCEPTION 'API-08 system pending-finalization marker remains unresolved'
            USING ERRCODE = '23514';
    END IF;

    IF target.commerce_authority = 'standard_commerce' THEN
        SELECT COUNT(DISTINCT product_id) INTO required_resources
          FROM shop_stockreservation WHERE order_id = target.order_id;
        SELECT COUNT(*) INTO created_resources
          FROM financial_core_standardinventorycommitment WHERE finalization_id = target.id;
        IF created_resources <> required_resources
           OR EXISTS (SELECT 1 FROM financial_core_digitalinventorycommitment WHERE finalization_id = target.id)
           OR EXISTS (
                SELECT 1
                  FROM financial_core_standardinventorycommitment c
                  JOIN product_product p ON p.id = c.product_id
                 WHERE c.finalization_id = target.id
                   AND (c.order_id <> target.order_id OR p.quantity <> c.post_quantity
                        OR c.committed_quantity <> (
                            SELECT COALESCE(SUM(r.quantity), 0) FROM shop_stockreservation r
                             WHERE r.order_id = target.order_id AND r.product_id = c.product_id
                        ))
           )
           OR EXISTS (
                SELECT 1
                  FROM financial_core_standardfulfillmentobligation f
                  JOIN shop_orderitem oi ON oi.id = f.order_item_id
                  JOIN shop_stockreservation r ON r.id = f.reservation_id
                 WHERE f.finalization_id = target.id
                   AND (f.order_id <> target.order_id OR oi.order_id <> target.order_id
                        OR r.order_id <> target.order_id OR f.product_id <> oi.product_id
                        OR r.product_id <> oi.product_id OR f.quantity <> oi.quantity)
           )
        THEN
            RAISE EXCEPTION 'API-08 Standard commitment or obligation lineage is inconsistent'
                USING ERRCODE = '23514';
        END IF;
    ELSIF target.commerce_authority = 'digital_products' THEN
        SELECT COUNT(DISTINCT inventory_pool_id) INTO required_resources
          FROM digital_products_digitalinventoryreservation WHERE order_id = target.order_id;
        SELECT COUNT(*) INTO created_resources
          FROM financial_core_digitalinventorycommitment WHERE finalization_id = target.id;
        IF created_resources <> required_resources
           OR EXISTS (SELECT 1 FROM financial_core_standardinventorycommitment WHERE finalization_id = target.id)
           OR EXISTS (
                SELECT 1
                  FROM financial_core_digitalinventorycommitment c
                  JOIN digital_products_inventorypool p ON p.id = c.inventory_pool_id
                 WHERE c.finalization_id = target.id
                   AND (c.order_id <> target.order_id OR p.sellable_quantity <> c.post_quantity
                        OR c.committed_quantity <> (
                            SELECT COALESCE(SUM(r.quantity), 0)
                              FROM digital_products_digitalinventoryreservation r
                             WHERE r.order_id = target.order_id AND r.inventory_pool_id = c.inventory_pool_id
                        ))
           )
           OR EXISTS (
                SELECT 1
                  FROM financial_core_digitalfulfillmentobligation f
                  JOIN shop_orderitem oi ON oi.id = f.order_item_id
                  JOIN digital_products_digitalinventoryreservation r ON r.id = f.reservation_id
                 WHERE f.finalization_id = target.id
                   AND (f.order_id <> target.order_id OR oi.order_id <> target.order_id
                        OR r.order_id <> target.order_id OR f.inventory_pool_id <> r.inventory_pool_id
                        OR f.checkout_line_id <> r.checkout_line_id OR f.quantity <> 1 OR oi.quantity <> 1)
           )
        THEN
            RAISE EXCEPTION 'API-08 Digital commitment or obligation lineage is inconsistent'
                USING ERRCODE = '23514';
        END IF;
    ELSE
        RAISE EXCEPTION 'API-08 commercial authority is unsupported' USING ERRCODE = '23514';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_finalization()
RETURNS trigger AS $$
BEGIN
    PERFORM financial_core_validate_api08_finalization(NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_work()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    IF NEW.status <> 'completed' THEN RETURN NULL; END IF;
    SELECT id INTO STRICT finalization_id FROM financial_core_commercialfinalization
     WHERE payment_id = NEW.payment_id;
    PERFORM financial_core_validate_api08_finalization(finalization_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_commitment()
RETURNS trigger AS $$
BEGIN
    PERFORM financial_core_validate_api08_finalization(NEW.finalization_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_outbox()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    IF NEW.topic <> 'commercial.fulfillment.requested'
       OR NEW.aggregate_type <> 'financial_core.commercialfinalization'
    THEN
        RETURN NULL;
    END IF;
    SELECT id INTO STRICT finalization_id
      FROM financial_core_commercialfinalization
     WHERE public_id::text = NEW.aggregate_id;
    PERFORM financial_core_validate_api08_finalization(finalization_id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_protect_api08_review_resolution()
RETURNS trigger AS $$
BEGIN
    IF OLD.status <> 'resolved' AND NEW.status = 'resolved'
       AND OLD.reason = 'paid_pending_finalization'
       AND OLD.opened_by_type = 'system' AND OLD.opened_by_id IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM financial_core_commercialfinalization f WHERE f.payment_id = OLD.payment_id
       )
    THEN
        RAISE EXCEPTION 'System finalization marker requires commercial finalization'
            USING ERRCODE = '23514';
    END IF;
    IF OLD.status <> 'resolved' AND NEW.status = 'resolved'
       AND OLD.reason = 'paid_pending_finalization'
       AND OLD.opened_by_type = 'system' AND OLD.opened_by_id IS NULL
       AND NOT EXISTS (
           SELECT 1 FROM financial_core_reviewaction a
            WHERE a.review_case_id = OLD.id
              AND a.action_type = 'transition:resolved'
              AND a.actor_type = 'system'
              AND a.actor_id IS NULL
              AND a.reason_code = 'commercial_finalization_completed'
       )
    THEN
        RAISE EXCEPTION 'System finalization marker requires an exact system resolution action'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_validate_api08_standard_reservation()
RETURNS trigger AS $$
BEGIN
    IF NEW.state = 'consumed' AND NEW.order_id IS NOT NULL AND (
        SELECT COUNT(*)
          FROM financial_core_standardfulfillmentobligation o
          JOIN financial_core_commercialfinalization f ON f.id = o.finalization_id
          JOIN financial_core_standardinventorycommitment c
            ON c.finalization_id = f.id AND c.product_id = NEW.product_id
         WHERE o.reservation_id = NEW.id
           AND o.order_id = NEW.order_id
           AND f.order_id = NEW.order_id
           AND c.order_id = NEW.order_id
    ) <> 1 THEN
        RAISE EXCEPTION 'Consumed Standard reservation requires exact API-08 finalization evidence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_validate_api08_digital_reservation()
RETURNS trigger AS $$
BEGIN
    IF NEW.state = 'consumed' AND NEW.order_id IS NOT NULL AND (
        SELECT COUNT(*)
          FROM financial_core_digitalfulfillmentobligation o
          JOIN financial_core_commercialfinalization f ON f.id = o.finalization_id
          JOIN financial_core_digitalinventorycommitment c
            ON c.finalization_id = f.id AND c.inventory_pool_id = NEW.inventory_pool_id
         WHERE o.reservation_id = NEW.id
           AND o.order_id = NEW.order_id
           AND f.order_id = NEW.order_id
           AND c.order_id = NEW.order_id
    ) <> 1 THEN
        RAISE EXCEPTION 'Consumed Digital reservation requires exact API-08 finalization evidence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_protect_consumed_reservation()
RETURNS trigger AS $$
BEGIN
    IF OLD.state = 'consumed' THEN
        RAISE EXCEPTION 'Consumed reservation evidence is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_validate_api08_digital_snapshot()
RETURNS trigger AS $$
DECLARE frozen record;
BEGIN
    SELECT line.product_id AS line_product_id,
           line.commerce_authority AS line_authority,
           line.unit_payable_price AS line_unit_price,
           line.line_payable_total AS line_total,
           line.quantity AS line_quantity,
           line.snapshot AS line_snapshot,
           offer.inventory_pool_id AS offer_pool_id,
           offer.delivered_version_id AS offer_version_id,
           offer.customer_console AS offer_console,
           offer.capacity AS offer_capacity,
           offer.price AS offer_price,
           selection.offer_id AS selection_offer_id,
           selection.fulfillment_method AS selection_method,
           version.product_id AS version_product_id,
           version.native_console AS version_native_console
      INTO STRICT frozen
      FROM shop_checkoutline line
      JOIN digital_products_digitalcartselection selection
        ON selection.cart_item_id = line.source_cart_item_id
      JOIN digital_products_digitaloffer offer ON offer.id = NEW.offer_id
      JOIN product_deliveredversion version ON version.id = offer.delivered_version_id
     WHERE line.id = NEW.checkout_line_id;

    IF NEW.commerce_authority <> 'digital_products'
       OR frozen.line_authority <> 'digital_products'
       OR NEW.product_id <> frozen.line_product_id
       OR NEW.product_id <> frozen.version_product_id
       OR NEW.inventory_pool_id <> frozen.offer_pool_id
       OR NEW.offer_id <> frozen.selection_offer_id
       OR NEW.fulfillment_method <> frozen.selection_method
       OR NEW.delivered_version_id <> frozen.offer_version_id
       OR NEW.customer_console <> frozen.offer_console
       OR NEW.capacity <> frozen.offer_capacity
       OR NEW.native_console <> frozen.version_native_console
       OR NEW.unit_price <> frozen.line_unit_price
       OR NEW.line_total <> frozen.line_total
       OR NEW.unit_price <> frozen.offer_price
       OR NEW.quantity <> 1 OR frozen.line_quantity <> 1
       OR COALESCE(frozen.line_snapshot->>'commerce_authority', '') <> 'digital_products'
       OR COALESCE((frozen.line_snapshot->>'commercial_revision')::integer, 0) <= 0
       OR (NEW.capacity = 'capacity_1' AND NEW.fulfillment_method <> 'in_store')
    THEN
        RAISE EXCEPTION 'Digital Checkout commercial snapshot identity is incoherent'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_protect_api08_digital_snapshot()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Digital Checkout commercial snapshot evidence is immutable'
        USING ERRCODE = '55000';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_protect_api08_digital_line()
RETURNS trigger AS $$
BEGIN
    IF OLD.commerce_authority = 'digital_products' THEN
        RAISE EXCEPTION 'Digital CheckoutLine commercial evidence is immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_order_projection()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    IF (
        (OLD.payment_status <> 3 AND NEW.payment_status = 3)
        OR (OLD.fulfillment_status <> 'processing' AND NEW.fulfillment_status = 'processing')
    ) AND EXISTS (
        SELECT 1 FROM financial_core_payment payment WHERE payment.order_id = NEW.id
    )
    THEN
        SELECT id INTO finalization_id FROM financial_core_commercialfinalization
         WHERE order_id = NEW.id;
        IF finalization_id IS NULL THEN
            RAISE EXCEPTION 'Paid/processing Order requires API-08 finalization'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_checkout_projection()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    IF OLD.status <> 'paid' AND NEW.status = 'paid' AND EXISTS (
        SELECT 1 FROM financial_core_payment payment
        JOIN shop_order payment_order ON payment_order.id = payment.order_id
        WHERE payment_order.checkout_id = NEW.id
    ) THEN
        SELECT f.id INTO finalization_id
          FROM financial_core_commercialfinalization f
          JOIN shop_order o ON o.id = f.order_id
         WHERE o.checkout_id = NEW.id;
        IF finalization_id IS NULL THEN
            RAISE EXCEPTION 'Paid Checkout requires API-08 finalization'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_cart_projection()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    IF (
        (OLD.state <> 'open' AND NEW.state = 'open')
        OR (OLD.active_checkout_id IS NOT NULL AND NEW.active_checkout_id IS NULL)
    ) AND EXISTS (
        SELECT 1 FROM financial_core_payment payment
        JOIN shop_order payment_order ON payment_order.id = payment.order_id
        WHERE payment_order.checkout_id = OLD.active_checkout_id
    )
    THEN
        SELECT f.id INTO finalization_id
          FROM financial_core_commercialfinalization f
          JOIN shop_order o ON o.id = f.order_id
         WHERE o.checkout_id = OLD.active_checkout_id;
        IF finalization_id IS NULL THEN
            RAISE EXCEPTION 'Cart unlock requires API-08 finalization'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_product_delta()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
DECLARE matching_commitments integer;
BEGIN
    IF NEW.quantity < OLD.quantity AND EXISTS (
        SELECT 1 FROM shop_stockreservation r
         WHERE r.product_id = NEW.id AND r.order_id IS NOT NULL
           AND r.state IN ('payment_hold', 'consumed')
    ) THEN
        SELECT COUNT(*), MIN(c.finalization_id)
          INTO matching_commitments, finalization_id
          FROM financial_core_standardinventorycommitment c
         WHERE c.product_id = NEW.id
           AND c.pre_quantity = OLD.quantity
           AND c.post_quantity = NEW.quantity
           AND c.committed_quantity = OLD.quantity - NEW.quantity;
        IF matching_commitments <> 1 THEN
            RAISE EXCEPTION 'Standard inventory delta requires one exact API-08 commitment'
                USING ERRCODE = '23514';
        END IF;
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

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

CREATE OR REPLACE FUNCTION financial_core_validate_api08_standard_commitment_origin()
RETURNS trigger AS $$
DECLARE current_quantity bigint;
DECLARE reserved_quantity bigint;
BEGIN
    SELECT quantity INTO STRICT current_quantity
      FROM product_product WHERE id = NEW.product_id;
    SELECT COALESCE(SUM(quantity), 0) INTO reserved_quantity
      FROM shop_stockreservation
     WHERE order_id = NEW.order_id AND product_id = NEW.product_id
       AND state = 'payment_hold';
    IF current_quantity <> NEW.pre_quantity
       OR reserved_quantity <> NEW.committed_quantity
    THEN
        RAISE EXCEPTION 'Standard commitment must originate from current held inventory'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_validate_api08_digital_commitment_origin()
RETURNS trigger AS $$
DECLARE current_quantity bigint;
DECLARE reserved_quantity bigint;
BEGIN
    SELECT sellable_quantity INTO STRICT current_quantity
      FROM digital_products_inventorypool WHERE id = NEW.inventory_pool_id;
    SELECT COALESCE(SUM(quantity), 0) INTO reserved_quantity
      FROM digital_products_digitalinventoryreservation
     WHERE order_id = NEW.order_id AND inventory_pool_id = NEW.inventory_pool_id
       AND state = 'payment_hold';
    IF current_quantity <> NEW.pre_quantity
       OR reserved_quantity <> NEW.committed_quantity
    THEN
        RAISE EXCEPTION 'Digital commitment must originate from current held inventory'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_check_api08_journal_posting()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
    SELECT id INTO finalization_id FROM financial_core_commercialfinalization
     WHERE journal_entry_id = NEW.entry_id;
    IF finalization_id IS NOT NULL THEN
        PERFORM financial_core_validate_api08_finalization(finalization_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM digital_products_digitalcheckoutlinesnapshot snapshot
          JOIN shop_checkoutline line ON line.id = snapshot.checkout_line_id
          LEFT JOIN digital_products_digitalcartselection selection
            ON selection.cart_item_id = line.source_cart_item_id
          JOIN digital_products_digitaloffer offer ON offer.id = snapshot.offer_id
          JOIN product_deliveredversion version ON version.id = offer.delivered_version_id
         WHERE selection.id IS NULL
            OR snapshot.commerce_authority <> 'digital_products'
            OR line.commerce_authority <> 'digital_products'
            OR snapshot.product_id <> line.product_id
            OR snapshot.product_id <> version.product_id
            OR snapshot.inventory_pool_id <> offer.inventory_pool_id
            OR snapshot.offer_id <> selection.offer_id
            OR snapshot.fulfillment_method <> selection.fulfillment_method
            OR snapshot.delivered_version_id <> offer.delivered_version_id
            OR snapshot.customer_console <> offer.customer_console
            OR snapshot.capacity <> offer.capacity
            OR snapshot.native_console <> version.native_console
            OR snapshot.unit_price <> line.unit_payable_price
            OR snapshot.line_total <> line.line_payable_total
            OR snapshot.unit_price <> offer.price
            OR snapshot.quantity <> 1 OR line.quantity <> 1
            OR COALESCE(line.snapshot->>'commerce_authority', '') <> 'digital_products'
            OR COALESCE(line.snapshot->>'commercial_revision', '') !~ '^[1-9][0-9]*$'
            OR (snapshot.capacity = 'capacity_1' AND snapshot.fulfillment_method <> 'in_store')
    ) THEN
        RAISE EXCEPTION 'Existing Digital Checkout commercial snapshot evidence is incoherent'
            USING ERRCODE = '23514';
    END IF;
END;
$$;

CREATE TRIGGER financial_core_api08_digital_snapshot_insert_valid
BEFORE INSERT ON digital_products_digitalcheckoutlinesnapshot
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_api08_digital_snapshot();

CREATE TRIGGER financial_core_api08_digital_snapshot_immutable
BEFORE UPDATE OR DELETE ON digital_products_digitalcheckoutlinesnapshot
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_api08_digital_snapshot();

CREATE TRIGGER financial_core_api08_digital_line_immutable
BEFORE UPDATE OR DELETE ON shop_checkoutline
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_api08_digital_line();

CREATE CONSTRAINT TRIGGER financial_core_api08_finalization_valid
AFTER INSERT ON financial_core_commercialfinalization
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_finalization();

CREATE CONSTRAINT TRIGGER financial_core_api08_work_complete_valid
AFTER UPDATE OF status ON financial_core_commercialfinalizationworkitem
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_work();

CREATE CONSTRAINT TRIGGER financial_core_api08_standard_commitment_valid
AFTER INSERT ON financial_core_standardinventorycommitment
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_commitment();

CREATE TRIGGER financial_core_api08_standard_commitment_origin_valid
BEFORE INSERT ON financial_core_standardinventorycommitment
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_api08_standard_commitment_origin();

CREATE CONSTRAINT TRIGGER financial_core_api08_digital_commitment_valid
AFTER INSERT ON financial_core_digitalinventorycommitment
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_commitment();

CREATE TRIGGER financial_core_api08_digital_commitment_origin_valid
BEFORE INSERT ON financial_core_digitalinventorycommitment
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_api08_digital_commitment_origin();

CREATE CONSTRAINT TRIGGER financial_core_api08_outbox_valid
AFTER INSERT ON financial_core_financialoutboxmessage
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_outbox();

CREATE TRIGGER financial_core_api08_review_resolution_valid
BEFORE UPDATE OF status ON financial_core_reviewcase
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_api08_review_resolution();

CREATE CONSTRAINT TRIGGER financial_core_api08_standard_reservation_valid
AFTER INSERT OR UPDATE OF state ON shop_stockreservation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_api08_standard_reservation();

CREATE CONSTRAINT TRIGGER financial_core_api08_digital_reservation_valid
AFTER INSERT OR UPDATE OF state ON digital_products_digitalinventoryreservation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_api08_digital_reservation();

CREATE CONSTRAINT TRIGGER financial_core_api08_order_projection_valid
AFTER UPDATE OF payment_status, fulfillment_status ON shop_order
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_order_projection();

CREATE CONSTRAINT TRIGGER financial_core_api08_checkout_projection_valid
AFTER UPDATE OF status ON shop_checkout
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_checkout_projection();

CREATE CONSTRAINT TRIGGER financial_core_api08_cart_projection_valid
AFTER UPDATE OF state, active_checkout_id ON shop_cart
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_cart_projection();

CREATE CONSTRAINT TRIGGER financial_core_api08_product_delta_valid
AFTER UPDATE OF quantity ON product_product
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_product_delta();

CREATE CONSTRAINT TRIGGER financial_core_api08_pool_delta_valid
AFTER UPDATE OF sellable_quantity ON digital_products_inventorypool
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_pool_delta();

CREATE CONSTRAINT TRIGGER financial_core_api08_journal_posting_valid
AFTER INSERT ON financial_core_journalposting
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_check_api08_journal_posting();

CREATE TRIGGER financial_core_api08_standard_consumed_immutable
BEFORE UPDATE OR DELETE ON shop_stockreservation
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_consumed_reservation();

CREATE TRIGGER financial_core_api08_digital_consumed_immutable
BEFORE UPDATE OR DELETE ON digital_products_digitalinventoryreservation
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_consumed_reservation();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_api08_journal_posting_valid ON financial_core_journalposting;
DROP TRIGGER IF EXISTS financial_core_api08_digital_commitment_origin_valid ON financial_core_digitalinventorycommitment;
DROP TRIGGER IF EXISTS financial_core_api08_standard_commitment_origin_valid ON financial_core_standardinventorycommitment;
DROP TRIGGER IF EXISTS financial_core_api08_pool_delta_valid ON digital_products_inventorypool;
DROP TRIGGER IF EXISTS financial_core_api08_product_delta_valid ON product_product;
DROP TRIGGER IF EXISTS financial_core_api08_cart_projection_valid ON shop_cart;
DROP TRIGGER IF EXISTS financial_core_api08_checkout_projection_valid ON shop_checkout;
DROP TRIGGER IF EXISTS financial_core_api08_order_projection_valid ON shop_order;
DROP TRIGGER IF EXISTS financial_core_api08_digital_consumed_immutable ON digital_products_digitalinventoryreservation;
DROP TRIGGER IF EXISTS financial_core_api08_standard_consumed_immutable ON shop_stockreservation;
DROP TRIGGER IF EXISTS financial_core_api08_digital_reservation_valid ON digital_products_digitalinventoryreservation;
DROP TRIGGER IF EXISTS financial_core_api08_standard_reservation_valid ON shop_stockreservation;
DROP TRIGGER IF EXISTS financial_core_api08_review_resolution_valid ON financial_core_reviewcase;
DROP TRIGGER IF EXISTS financial_core_api08_outbox_valid ON financial_core_financialoutboxmessage;
DROP TRIGGER IF EXISTS financial_core_api08_digital_commitment_valid ON financial_core_digitalinventorycommitment;
DROP TRIGGER IF EXISTS financial_core_api08_standard_commitment_valid ON financial_core_standardinventorycommitment;
DROP TRIGGER IF EXISTS financial_core_api08_work_complete_valid ON financial_core_commercialfinalizationworkitem;
DROP TRIGGER IF EXISTS financial_core_api08_finalization_valid ON financial_core_commercialfinalization;
DROP TRIGGER IF EXISTS financial_core_api08_digital_line_immutable ON shop_checkoutline;
DROP TRIGGER IF EXISTS financial_core_api08_digital_snapshot_immutable ON digital_products_digitalcheckoutlinesnapshot;
DROP TRIGGER IF EXISTS financial_core_api08_digital_snapshot_insert_valid ON digital_products_digitalcheckoutlinesnapshot;
DROP FUNCTION IF EXISTS financial_core_check_api08_journal_posting();
DROP FUNCTION IF EXISTS financial_core_validate_api08_digital_commitment_origin();
DROP FUNCTION IF EXISTS financial_core_validate_api08_standard_commitment_origin();
DROP FUNCTION IF EXISTS financial_core_check_api08_pool_delta();
DROP FUNCTION IF EXISTS financial_core_check_api08_product_delta();
DROP FUNCTION IF EXISTS financial_core_check_api08_cart_projection();
DROP FUNCTION IF EXISTS financial_core_check_api08_checkout_projection();
DROP FUNCTION IF EXISTS financial_core_check_api08_order_projection();
DROP FUNCTION IF EXISTS financial_core_protect_api08_digital_line();
DROP FUNCTION IF EXISTS financial_core_protect_api08_digital_snapshot();
DROP FUNCTION IF EXISTS financial_core_validate_api08_digital_snapshot();
DROP FUNCTION IF EXISTS financial_core_protect_api08_review_resolution();
DROP FUNCTION IF EXISTS financial_core_protect_consumed_reservation();
DROP FUNCTION IF EXISTS financial_core_validate_api08_digital_reservation();
DROP FUNCTION IF EXISTS financial_core_validate_api08_standard_reservation();
DROP FUNCTION IF EXISTS financial_core_check_api08_outbox();
DROP FUNCTION IF EXISTS financial_core_check_api08_commitment();
DROP FUNCTION IF EXISTS financial_core_check_api08_work();
DROP FUNCTION IF EXISTS financial_core_check_api08_finalization();
DROP FUNCTION IF EXISTS financial_core_validate_api08_finalization(bigint);
DROP TRIGGER IF EXISTS financial_core_finalization_outbox_append_only ON financial_core_financialoutboxmessage;
DROP TRIGGER IF EXISTS financial_core_digital_commitment_append_only ON financial_core_digitalinventorycommitment;
DROP TRIGGER IF EXISTS financial_core_standard_commitment_append_only ON financial_core_standardinventorycommitment;
"""


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0020_api08_inventory_commitment_evidence")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
