from django.db import migrations


FORWARD_SQL = r"""
DROP TRIGGER financial_core_commercial_finalization_valid ON financial_core_commercialfinalization;
ALTER FUNCTION financial_core_validate_commercial_finalization()
RENAME TO financial_core_validate_legacy_commercial_finalization;
ALTER FUNCTION financial_core_validate_api08_finalization(bigint)
RENAME TO financial_core_validate_api08_legacy_finalization;

CREATE FUNCTION financial_core_validate_api08_finalization(p_finalization_id bigint)
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
       WHERE o.finalization_id=target.id AND
         (SELECT count(*) FROM financial_core_performanceobligationcomponent c WHERE c.obligation_id=o.id) <> 1)
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

CREATE FUNCTION financial_core_validate_commercial_finalization()
RETURNS trigger AS $$
BEGIN
  IF NEW.recognition_accounting_contract IS NULL THEN
    -- The legacy function is a trigger function and cannot be called directly;
    -- API-08's complete legacy validator provides the same frozen graph authority.
    PERFORM financial_core_validate_api08_legacy_finalization(NEW.id);
  ELSE
    PERFORM financial_core_validate_api08_finalization(NEW.id);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER financial_core_commercial_finalization_valid
AFTER INSERT ON financial_core_commercialfinalization
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_commercial_finalization();

CREATE FUNCTION financial_core_revalidate_v2_finalization_child()
RETURNS trigger AS $$
DECLARE finalization_id bigint;
BEGIN
  IF TG_TABLE_NAME = 'financial_core_performanceobligation' THEN
    finalization_id := NEW.finalization_id;
  ELSIF TG_TABLE_NAME = 'financial_core_performanceobligationcomponent' THEN
    SELECT o.finalization_id INTO STRICT finalization_id
      FROM financial_core_performanceobligation o WHERE o.id=NEW.obligation_id;
  ELSE
    finalization_id := NEW.finalization_id;
  END IF;
  IF EXISTS (SELECT 1 FROM financial_core_commercialfinalization f
              WHERE f.id=finalization_id AND f.recognition_accounting_contract IS NOT NULL) THEN
    PERFORM financial_core_validate_api08_finalization(finalization_id);
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER financial_core_v2_obligation_graph_valid
AFTER INSERT ON financial_core_performanceobligation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_revalidate_v2_finalization_child();
CREATE CONSTRAINT TRIGGER financial_core_v2_component_graph_valid
AFTER INSERT ON financial_core_performanceobligationcomponent
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_revalidate_v2_finalization_child();
CREATE CONSTRAINT TRIGGER financial_core_v2_allocation_graph_valid
AFTER INSERT ON financial_core_considerationallocation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_revalidate_v2_finalization_child();
"""

REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_v2_allocation_graph_valid ON financial_core_considerationallocation;
DROP TRIGGER IF EXISTS financial_core_v2_component_graph_valid ON financial_core_performanceobligationcomponent;
DROP TRIGGER IF EXISTS financial_core_v2_obligation_graph_valid ON financial_core_performanceobligation;
DROP FUNCTION IF EXISTS financial_core_revalidate_v2_finalization_child();
DROP TRIGGER financial_core_commercial_finalization_valid ON financial_core_commercialfinalization;
DROP FUNCTION financial_core_validate_commercial_finalization();
ALTER FUNCTION financial_core_validate_legacy_commercial_finalization()
RENAME TO financial_core_validate_commercial_finalization;
CREATE CONSTRAINT TRIGGER financial_core_commercial_finalization_valid
AFTER INSERT ON financial_core_commercialfinalization
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_commercial_finalization();
DROP FUNCTION financial_core_validate_api08_finalization(bigint);
ALTER FUNCTION financial_core_validate_api08_legacy_finalization(bigint)
RENAME TO financial_core_validate_api08_finalization;
"""

class Migration(migrations.Migration):
    dependencies = [("financial_core", "0024_commercial_finalization_v2_contract")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
