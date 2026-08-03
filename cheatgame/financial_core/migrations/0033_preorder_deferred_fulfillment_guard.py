from django.db import migrations


FUNCTION_TEMPLATE = r"""
CREATE OR REPLACE FUNCTION financial_core_validate_api08_finalization(p_finalization_id bigint)
RETURNS void AS $$
DECLARE target record; obligation_count integer; item_count integer;
BEGIN
  SELECT f.*, p.collection_status, p.confirmed_amount, p.amount_due,
         o.payment_status, o.fulfillment_status, o.checkout_id
    INTO STRICT target FROM financial_core_commercialfinalization f
    JOIN financial_core_payment p ON p.id=f.payment_id
    JOIN shop_order o ON o.id=f.order_id WHERE f.id=p_finalization_id;
  IF target.recognition_accounting_contract IS NULL THEN
    PERFORM financial_core_validate_api08_legacy_finalization(p_finalization_id); RETURN;
  END IF;
  IF target.recognition_accounting_contract <> 'commercial-finalizer-v2-contract-liability'
     OR target.accounting_policy_version_id IS NOT NULL
     OR target.contract_liability_account_id IS NULL
     OR target.recognition_policy_set_digest IS NULL
     OR target.collection_status <> 'paid' OR target.confirmed_amount <> target.amount_due
     OR target.payment_status <> 3 OR target.fulfillment_status <> 'processing'
  THEN RAISE EXCEPTION 'v2 terminal/accounting contract is incoherent' USING ERRCODE='23514'; END IF;
  SELECT count(*) INTO obligation_count FROM financial_core_performanceobligation WHERE finalization_id=target.id;
  SELECT count(*) INTO item_count FROM shop_orderitem WHERE order_id=target.order_id;
  IF obligation_count <> item_count OR obligation_count = 0
     OR (SELECT count(DISTINCT o.recognition_policy_version_id)
           FROM financial_core_performanceobligation o WHERE o.finalization_id=target.id) <> 1
     OR target.recognition_policy_set_digest <> (SELECT max(p.policy_fingerprint)
           FROM financial_core_performanceobligation o
           JOIN financial_core_recognitionpolicyversion p ON p.id=o.recognition_policy_version_id
          WHERE o.finalization_id=target.id)
     OR EXISTS (SELECT 1 FROM financial_core_performanceobligation o
       WHERE o.finalization_id=target.id AND (o.order_id<>target.order_id OR o.currency<>'IRR'))
     OR EXISTS (SELECT 1 FROM financial_core_performanceobligation o
       WHERE o.finalization_id=target.id AND NOT ({component_rule}))
     OR EXISTS (SELECT 1 FROM financial_core_performanceobligationcomponent c
       JOIN financial_core_performanceobligation o ON o.id=c.obligation_id
       JOIN shop_orderitem i ON i.id=c.order_item_id
       WHERE o.finalization_id=target.id AND (c.order_id<>target.order_id OR i.order_id<>target.order_id))
     OR (SELECT count(DISTINCT c.order_item_id) FROM financial_core_performanceobligationcomponent c
       JOIN financial_core_performanceobligation o ON o.id=c.obligation_id WHERE o.finalization_id=target.id) <> item_count
  THEN RAISE EXCEPTION 'v2 obligation/component graph is incomplete' USING ERRCODE='23514'; END IF;
  IF EXISTS (SELECT 1 FROM financial_core_performanceobligation o
       WHERE o.finalization_id=target.id AND
         (SELECT count(*) FROM financial_core_considerationallocation a WHERE a.obligation_id=o.id) <> 1)
     OR (SELECT coalesce(sum(a.allocated_amount),0) FROM financial_core_considerationallocation a
       WHERE a.finalization_id=target.id) <> target.amount
     OR EXISTS (SELECT 1 FROM financial_core_considerationallocation a
       JOIN financial_core_performanceobligation o ON o.id=a.obligation_id
       JOIN financial_core_recognitionpolicyversion p ON p.id=a.recognition_policy_version_id
       WHERE a.finalization_id=target.id AND (a.payment_id<>target.payment_id OR o.finalization_id<>target.id
         OR a.currency<>'IRR' OR a.contract_liability_account_id<>p.contract_liability_account_id))
  THEN RAISE EXCEPTION 'v2 consideration allocation is incoherent' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS (SELECT 1 FROM financial_core_journalentry j WHERE j.id=target.journal_entry_id
      AND j.source_type='commercial_reclassification' AND j.source_id=target.public_id::text)
     OR (SELECT count(*) FROM financial_core_journalposting WHERE entry_id=target.journal_entry_id) <> 2
     OR (SELECT coalesce(sum(amount),0) FROM financial_core_journalposting
          WHERE entry_id=target.journal_entry_id AND direction='debit' AND currency='IRR') <> target.amount
     OR (SELECT coalesce(sum(amount),0) FROM financial_core_journalposting
          WHERE entry_id=target.journal_entry_id AND direction='credit' AND currency='IRR') <> target.amount
     OR EXISTS (SELECT 1 FROM financial_core_journalposting jp JOIN financial_core_financialaccount a ON a.id=jp.account_id
          WHERE jp.entry_id=target.journal_entry_id AND a.account_type='revenue')
     OR NOT EXISTS (SELECT 1 FROM financial_core_journalposting jp WHERE jp.entry_id=target.journal_entry_id
          AND jp.direction='credit' AND jp.account_id=target.contract_liability_account_id AND jp.amount=target.amount)
     OR (SELECT count(DISTINCT rp.customer_unapplied_funds_account_id)
           FROM financial_core_financialallocation fa
           JOIN financial_core_receiptaccountingpolicyversion rp ON rp.id=fa.accounting_policy_version_id
          WHERE fa.payment_id=target.payment_id) <> 1
     OR NOT EXISTS (SELECT 1 FROM financial_core_journalposting jp
           JOIN financial_core_financialallocation fa ON fa.payment_id=target.payment_id
           JOIN financial_core_receiptaccountingpolicyversion rp ON rp.id=fa.accounting_policy_version_id
          WHERE jp.entry_id=target.journal_entry_id AND jp.direction='debit'
            AND jp.account_id=rp.customer_unapplied_funds_account_id AND jp.amount=target.amount)
     OR (SELECT count(*) FROM financial_core_commercialfinalizationworkitem w WHERE w.payment_id=target.payment_id
          AND w.status='completed' AND w.finalizer_version='commercial-finalizer-v1-dormant') <> 1
     OR (SELECT count(*) FROM financial_core_financialoutboxmessage m WHERE m.topic='commercial.fulfillment.requested'
          AND m.aggregate_id=target.public_id::text) <> 1
  THEN RAISE EXCEPTION 'v2 Journal/work/outbox graph is incoherent' USING ERRCODE='23514'; END IF;
END;
$$ LANGUAGE plpgsql;
"""


ORIGINAL_COMPONENT_RULE = r"""
(SELECT count(*) FROM financial_core_performanceobligationcomponent c
  WHERE c.obligation_id=o.id) = 1
"""


PREORDER_COMPONENT_RULE = r"""
(SELECT count(*) FROM financial_core_performanceobligationcomponent c
  WHERE c.obligation_id=o.id) = 1
OR (
  (SELECT count(*) FROM financial_core_performanceobligationcomponent c
    WHERE c.obligation_id=o.id) = 2
  AND (SELECT count(*) FROM financial_core_performanceobligationcomponent c
    JOIN digital_products_digitalcheckoutlinesnapshot s ON s.checkout_line_id=c.checkout_line_id
    WHERE c.obligation_id=o.id
      AND c.digital_fulfillment_obligation_id IS NOT NULL
      AND c.component_type='fulfillment'
      AND s.safe_display_metadata->>'purchase_kind'='preorder') = 1
)
"""


FORWARD_SQL = FUNCTION_TEMPLATE.format(component_rule=PREORDER_COMPONENT_RULE)
REVERSE_SQL = FUNCTION_TEMPLATE.format(component_rule=ORIGINAL_COMPONENT_RULE)

FORWARD_SQL += r"""
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
    ) <> 1 AND NOT EXISTS (
        SELECT 1
          FROM digital_products_digitalcheckoutlinesnapshot s
          JOIN financial_core_commercialfinalization f ON f.order_id = NEW.order_id
          JOIN financial_core_digitalinventorycommitment c
            ON c.finalization_id = f.id AND c.inventory_pool_id = NEW.inventory_pool_id
         WHERE s.checkout_line_id = NEW.checkout_line_id
           AND s.inventory_pool_id = NEW.inventory_pool_id
           AND s.safe_display_metadata->>'purchase_kind' = 'preorder'
           AND c.order_id = NEW.order_id
    ) THEN
        RAISE EXCEPTION 'Consumed Digital reservation requires exact API-08 finalization evidence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

REVERSE_SQL += r"""
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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0032_postgresql_definitive_unpaid_cart_unlock"),
        ("digital_products", "0014_enable_preorder_product_state"),
    ]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
