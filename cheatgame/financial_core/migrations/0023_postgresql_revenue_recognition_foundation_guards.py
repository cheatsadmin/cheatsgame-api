from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION financial_core_protect_recognition_policy_version()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'recognition policy history is immutable' USING ERRCODE = '23514';
    END IF;
    IF ROW(
        NEW.public_id, NEW.policy_key, NEW.version, NEW.policy_contract_version,
        NEW.commerce_authority, NEW.obligation_type, NEW.satisfaction_pattern,
        NEW.evidence_contract_version, NEW.progress_measurement_method,
        NEW.allocation_method, NEW.principal_agent_classification,
        NEW.contract_liability_account_id, NEW.revenue_account_id, NEW.currency,
        NEW.shipping_treatment, NEW.rounding_policy, NEW.maximum_recognition_basis,
        NEW.policy_fingerprint
    ) IS DISTINCT FROM ROW(
        OLD.public_id, OLD.policy_key, OLD.version, OLD.policy_contract_version,
        OLD.commerce_authority, OLD.obligation_type, OLD.satisfaction_pattern,
        OLD.evidence_contract_version, OLD.progress_measurement_method,
        OLD.allocation_method, OLD.principal_agent_classification,
        OLD.contract_liability_account_id, OLD.revenue_account_id, OLD.currency,
        OLD.shipping_treatment, OLD.rounding_policy, OLD.maximum_recognition_basis,
        OLD.policy_fingerprint
    ) THEN
        RAISE EXCEPTION 'recognition policy versions cannot be rewritten' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_protect_recognition_work_identity()
RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'recognition work history cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF ROW(
        NEW.public_id, NEW.obligation_id, NEW.purpose, NEW.evidence_set_digest,
        NEW.recognition_policy_version_id, NEW.recognition_contract_version,
        NEW.recognition_period_key, NEW.cumulative_target_amount,
        NEW.deterministic_identity, NEW.max_attempts, NEW.correlation_id, NEW.causation_id
    ) IS DISTINCT FROM ROW(
        OLD.public_id, OLD.obligation_id, OLD.purpose, OLD.evidence_set_digest,
        OLD.recognition_policy_version_id, OLD.recognition_contract_version,
        OLD.recognition_period_key, OLD.cumulative_target_amount,
        OLD.deterministic_identity, OLD.max_attempts, OLD.correlation_id, OLD.causation_id
    ) THEN
        RAISE EXCEPTION 'recognition work identity cannot be rewritten' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION financial_core_validate_recognition_foundation_ownership()
RETURNS trigger AS $$
DECLARE
    obligation_row record;
    policy_row record;
    finalization_row record;
    fulfillment_row record;
BEGIN
    IF TG_TABLE_NAME = 'financial_core_recognitionpolicyversion' THEN
        IF NOT EXISTS (
            SELECT 1 FROM financial_core_financialaccount a
             WHERE a.id = NEW.contract_liability_account_id
               AND a.account_type = 'liability' AND a.currency = 'IRR'
        ) OR NOT EXISTS (
            SELECT 1 FROM financial_core_financialaccount a
             WHERE a.id = NEW.revenue_account_id
               AND a.account_type = 'revenue' AND a.currency = 'IRR'
        ) THEN
            RAISE EXCEPTION 'recognition policy account ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_performanceobligation' THEN
        SELECT f.order_id, f.commerce_authority INTO STRICT finalization_row
          FROM financial_core_commercialfinalization f WHERE f.id = NEW.finalization_id;
        SELECT * INTO STRICT policy_row
          FROM financial_core_recognitionpolicyversion p WHERE p.id = NEW.recognition_policy_version_id;
        IF NEW.order_id <> finalization_row.order_id
           OR NEW.commerce_authority <> finalization_row.commerce_authority
           OR NEW.commerce_authority <> policy_row.commerce_authority
           OR NEW.obligation_type <> policy_row.obligation_type
           OR NEW.satisfaction_pattern <> policy_row.satisfaction_pattern
           OR NEW.currency <> policy_row.currency THEN
            RAISE EXCEPTION 'performance obligation ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_performanceobligationcomponent' THEN
        SELECT * INTO STRICT obligation_row
          FROM financial_core_performanceobligation o WHERE o.id = NEW.obligation_id;
        IF NEW.order_id <> obligation_row.order_id
           OR (NEW.order_item_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM shop_orderitem i WHERE i.id = NEW.order_item_id AND i.order_id = NEW.order_id
           ))
           OR (NEW.checkout_line_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM shop_checkoutline l
                JOIN shop_order o ON o.checkout_id = l.checkout_id
                 WHERE l.id = NEW.checkout_line_id AND o.id = NEW.order_id
           ))
           OR (NEW.standard_fulfillment_obligation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_standardfulfillmentobligation f
                 WHERE f.id = NEW.standard_fulfillment_obligation_id
                   AND f.finalization_id = obligation_row.finalization_id
                   AND f.order_id = NEW.order_id
                   AND (NEW.order_item_id IS NULL OR f.order_item_id = NEW.order_item_id)
           ))
           OR (NEW.digital_fulfillment_obligation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_digitalfulfillmentobligation f
                 WHERE f.id = NEW.digital_fulfillment_obligation_id
                   AND f.finalization_id = obligation_row.finalization_id
                   AND f.order_id = NEW.order_id
                   AND (NEW.order_item_id IS NULL OR f.order_item_id = NEW.order_item_id)
           )) THEN
            RAISE EXCEPTION 'performance obligation component ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_considerationallocation' THEN
        SELECT * INTO STRICT obligation_row
          FROM financial_core_performanceobligation o WHERE o.id = NEW.obligation_id;
        SELECT * INTO STRICT policy_row
          FROM financial_core_recognitionpolicyversion p WHERE p.id = NEW.recognition_policy_version_id;
        IF NEW.finalization_id <> obligation_row.finalization_id
           OR NEW.recognition_policy_version_id <> obligation_row.recognition_policy_version_id
           OR NEW.payment_id <> (SELECT f.payment_id FROM financial_core_commercialfinalization f WHERE f.id = NEW.finalization_id)
           OR NEW.contract_liability_account_id <> policy_row.contract_liability_account_id
           OR NEW.allocation_method <> policy_row.allocation_method
           OR NEW.currency <> policy_row.currency THEN
            RAISE EXCEPTION 'consideration allocation ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_satisfactionevidence' THEN
        SELECT * INTO STRICT obligation_row
          FROM financial_core_performanceobligation o WHERE o.id = NEW.obligation_id;
        IF (NEW.standard_fulfillment_obligation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_standardfulfillmentobligation f
                 WHERE f.id = NEW.standard_fulfillment_obligation_id
                   AND f.finalization_id = obligation_row.finalization_id
           ))
           OR (NEW.digital_fulfillment_obligation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_digitalfulfillmentobligation f
                 WHERE f.id = NEW.digital_fulfillment_obligation_id
                   AND f.finalization_id = obligation_row.finalization_id
           ))
           OR (NEW.corrects_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_satisfactionevidence e
                 WHERE e.id = NEW.corrects_id AND e.obligation_id = NEW.obligation_id
           ))
           OR (NEW.contradicts_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_satisfactionevidence e
                 WHERE e.id = NEW.contradicts_id AND e.obligation_id = NEW.obligation_id
           )) THEN
            RAISE EXCEPTION 'satisfaction evidence ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_revenuerecognitionworkitem' THEN
        IF NOT EXISTS (
            SELECT 1 FROM financial_core_performanceobligation o
             WHERE o.id = NEW.obligation_id
               AND o.recognition_policy_version_id = NEW.recognition_policy_version_id
        ) THEN
            RAISE EXCEPTION 'recognition work ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'financial_core_revenuerecognition' THEN
        SELECT * INTO STRICT obligation_row
          FROM financial_core_performanceobligation o WHERE o.id = NEW.obligation_id;
        IF NEW.recognition_policy_version_id <> obligation_row.recognition_policy_version_id
           OR NOT EXISTS (
                SELECT 1 FROM financial_core_considerationallocation a
                 WHERE a.id = NEW.consideration_allocation_id
                   AND a.obligation_id = NEW.obligation_id
                   AND a.recognition_policy_version_id = NEW.recognition_policy_version_id
           )
           OR (NEW.work_item_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_revenuerecognitionworkitem w
                 WHERE w.id = NEW.work_item_id
                   AND w.obligation_id = NEW.obligation_id
                   AND w.recognition_policy_version_id = NEW.recognition_policy_version_id
           ))
           OR (NEW.corrects_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM financial_core_revenuerecognition r
                 WHERE r.id = NEW.corrects_id AND r.obligation_id = NEW.obligation_id
           )) THEN
            RAISE EXCEPTION 'revenue recognition ownership is incoherent' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER financial_core_rec_policy_immutable
BEFORE UPDATE OR DELETE ON financial_core_recognitionpolicyversion
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_recognition_policy_version();

CREATE TRIGGER financial_core_rec_work_identity_immutable
BEFORE UPDATE OR DELETE ON financial_core_revenuerecognitionworkitem
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_recognition_work_identity();

CREATE TRIGGER financial_core_perf_obligation_append_only
BEFORE UPDATE OR DELETE ON financial_core_performanceobligation
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();
CREATE TRIGGER financial_core_perf_component_append_only
BEFORE UPDATE OR DELETE ON financial_core_performanceobligationcomponent
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();
CREATE TRIGGER financial_core_consideration_append_only
BEFORE UPDATE OR DELETE ON financial_core_considerationallocation
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();
CREATE TRIGGER financial_core_satisfaction_append_only
BEFORE UPDATE OR DELETE ON financial_core_satisfactionevidence
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();
CREATE TRIGGER financial_core_revenue_recognition_append_only
BEFORE UPDATE OR DELETE ON financial_core_revenuerecognition
FOR EACH ROW EXECUTE FUNCTION financial_core_reject_mutation();

CREATE TRIGGER financial_core_rec_policy_owner_valid
BEFORE INSERT OR UPDATE ON financial_core_recognitionpolicyversion
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_perf_obligation_owner_valid
BEFORE INSERT ON financial_core_performanceobligation
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_perf_component_owner_valid
BEFORE INSERT ON financial_core_performanceobligationcomponent
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_consideration_owner_valid
BEFORE INSERT ON financial_core_considerationallocation
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_satisfaction_owner_valid
BEFORE INSERT ON financial_core_satisfactionevidence
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_rec_work_owner_valid
BEFORE INSERT OR UPDATE ON financial_core_revenuerecognitionworkitem
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
CREATE TRIGGER financial_core_revenue_recognition_owner_valid
BEFORE INSERT ON financial_core_revenuerecognition
FOR EACH ROW EXECUTE FUNCTION financial_core_validate_recognition_foundation_ownership();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_revenue_recognition_owner_valid ON financial_core_revenuerecognition;
DROP TRIGGER IF EXISTS financial_core_rec_work_owner_valid ON financial_core_revenuerecognitionworkitem;
DROP TRIGGER IF EXISTS financial_core_satisfaction_owner_valid ON financial_core_satisfactionevidence;
DROP TRIGGER IF EXISTS financial_core_consideration_owner_valid ON financial_core_considerationallocation;
DROP TRIGGER IF EXISTS financial_core_perf_component_owner_valid ON financial_core_performanceobligationcomponent;
DROP TRIGGER IF EXISTS financial_core_perf_obligation_owner_valid ON financial_core_performanceobligation;
DROP TRIGGER IF EXISTS financial_core_rec_policy_owner_valid ON financial_core_recognitionpolicyversion;
DROP TRIGGER IF EXISTS financial_core_revenue_recognition_append_only ON financial_core_revenuerecognition;
DROP TRIGGER IF EXISTS financial_core_satisfaction_append_only ON financial_core_satisfactionevidence;
DROP TRIGGER IF EXISTS financial_core_consideration_append_only ON financial_core_considerationallocation;
DROP TRIGGER IF EXISTS financial_core_perf_component_append_only ON financial_core_performanceobligationcomponent;
DROP TRIGGER IF EXISTS financial_core_perf_obligation_append_only ON financial_core_performanceobligation;
DROP TRIGGER IF EXISTS financial_core_rec_work_identity_immutable ON financial_core_revenuerecognitionworkitem;
DROP TRIGGER IF EXISTS financial_core_rec_policy_immutable ON financial_core_recognitionpolicyversion;
DROP FUNCTION IF EXISTS financial_core_validate_recognition_foundation_ownership();
DROP FUNCTION IF EXISTS financial_core_protect_recognition_work_identity();
DROP FUNCTION IF EXISTS financial_core_protect_recognition_policy_version();
"""


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0022_revenue_recognition_foundation")]

    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
