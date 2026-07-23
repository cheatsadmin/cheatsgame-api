from django.db import migrations


FORWARD_SQL = r"""
CREATE UNIQUE INDEX fin_revenue_engine_allocation_once
ON financial_core_revenuerecognition (consideration_allocation_id)
WHERE command_contract_version = 'revenue-recognition-engine-v1';

CREATE FUNCTION financial_core_validate_revenue_recognition_engine(p_recognition_id bigint)
RETURNS void AS $$
DECLARE
    target record;
    evidence_count integer;
    posting_count integer;
    earned_total numeric;
BEGIN
    SELECT r.*, o.finalization_id, o.order_id, o.commerce_authority,
           o.obligation_type, o.satisfaction_pattern, o.quantity_basis,
           a.finalization_id allocation_finalization_id, a.payment_id,
           a.recognition_policy_version_id allocation_policy_id,
           a.contract_liability_account_id allocation_liability_id,
           a.allocated_amount, a.currency allocation_currency,
           w.obligation_id work_obligation_id, w.purpose, w.evidence_set_digest work_evidence_digest,
           w.recognition_policy_version_id work_policy_id,
           w.recognition_contract_version work_contract,
           w.recognition_period_key work_period, w.cumulative_target_amount,
           w.status work_status, w.attempt_count, w.completed_at work_completed_at,
           w.safe_result,
           p.satisfaction_pattern policy_pattern, p.evidence_contract_version,
           p.progress_measurement_method, p.contract_liability_account_id policy_liability_id,
           p.revenue_account_id, p.currency policy_currency,
           p.maximum_recognition_basis,
           j.public_id journal_public_id, j.source_type, j.source_id
      INTO STRICT target
      FROM financial_core_revenuerecognition r
      JOIN financial_core_performanceobligation o ON o.id = r.obligation_id
      JOIN financial_core_considerationallocation a ON a.id = r.consideration_allocation_id
      JOIN financial_core_revenuerecognitionworkitem w ON w.id = r.work_item_id
      JOIN financial_core_recognitionpolicyversion p ON p.id = r.recognition_policy_version_id
      JOIN financial_core_journalentry j ON j.id = r.journal_entry_id
     WHERE r.id = p_recognition_id;

    IF target.command_contract_version <> 'revenue-recognition-engine-v1' THEN
        RETURN;
    END IF;

    SELECT count(*) INTO evidence_count
      FROM financial_core_satisfactionevidence e
     WHERE e.obligation_id = target.obligation_id
       AND e.source_evidence_hash = target.evidence_set_digest
       AND e.evidence_classification = 'point_in_time_satisfied'
       AND e.satisfied_quantity = target.quantity_basis
       AND e.progress_numerator IS NULL
       AND e.progress_denominator IS NULL
       AND e.corrects_id IS NULL
       AND e.contradicts_id IS NULL
       AND e.evidence_contract_version = CASE
            WHEN target.commerce_authority = 'standard_commerce'
             AND target.obligation_type = 'physical_good'
              THEN 'STANDARD_DELIVERY_COMPLETED'
            WHEN target.commerce_authority = 'digital_products'
             AND target.obligation_type = 'digital_access_installation'
              THEN 'DIGITAL_FULFILLMENT_COMPLETED'
            ELSE '__unsupported__'
       END;

    SELECT count(*) INTO posting_count
      FROM financial_core_journalposting jp WHERE jp.entry_id = target.journal_entry_id;

    SELECT COALESCE(sum(CASE WHEN effect = 'earn' THEN amount ELSE -amount END), 0)
      INTO earned_total
      FROM financial_core_revenuerecognition
     WHERE consideration_allocation_id = target.consideration_allocation_id;

    IF target.effect <> 'earn'
       OR target.corrects_id IS NOT NULL
       OR target.correction_reason <> ''
       OR target.amount <> target.allocated_amount
       OR target.cumulative_net_recognized_amount <> target.allocated_amount
       OR earned_total <> target.allocated_amount
       OR earned_total > target.allocated_amount
       OR target.currency <> 'IRR'
       OR target.allocation_currency <> 'IRR'
       OR target.obligation_id <> target.work_obligation_id
       OR target.obligation_id <> (SELECT obligation_id FROM financial_core_considerationallocation WHERE id=target.consideration_allocation_id)
       OR target.finalization_id <> target.allocation_finalization_id
       OR target.recognition_policy_version_id <> target.allocation_policy_id
       OR target.recognition_policy_version_id <> target.work_policy_id
       OR target.allocation_liability_id <> target.policy_liability_id
       OR target.satisfaction_pattern <> 'point_in_time'
       OR target.policy_pattern <> 'point_in_time'
       OR target.progress_measurement_method <> 'none'
       OR target.evidence_contract_version <> 'fulfillment-satisfaction-v1'
       OR target.maximum_recognition_basis <> 'allocated_consideration'
       OR target.purpose <> 'recognize_satisfaction'
       OR target.work_contract <> 'revenue-recognition-engine-v1'
       OR target.work_period <> 'point-in-time'
       OR target.recognition_period_key <> 'point-in-time'
       OR target.work_evidence_digest <> target.evidence_set_digest
       OR target.cumulative_target_amount <> target.allocated_amount
       OR target.work_status <> 'completed'
       OR target.attempt_count < 1
       OR target.work_completed_at IS NULL
       OR target.safe_result->>'recognition_public_id' <> target.public_id::text
       OR target.safe_result->>'journal_public_id' <> target.journal_public_id::text
       OR evidence_count <> 1
       OR (SELECT count(*) FROM financial_core_satisfactionevidence e WHERE e.obligation_id=target.obligation_id) <> 1
       OR target.source_type <> 'revenue_recognition'
       OR target.source_id <> target.public_id::text
       OR posting_count <> 2
       OR NOT EXISTS (
            SELECT 1 FROM financial_core_financialaccount a
             WHERE a.id=target.policy_liability_id AND a.account_type='liability'
               AND a.currency='IRR' AND a.status='active'
       )
       OR NOT EXISTS (
            SELECT 1 FROM financial_core_financialaccount a
             WHERE a.id=target.revenue_account_id AND a.account_type='revenue'
               AND a.currency='IRR' AND a.status='active'
       )
       OR target.policy_liability_id = target.revenue_account_id
       OR NOT EXISTS (
            SELECT 1 FROM financial_core_journalposting jp
             WHERE jp.entry_id=target.journal_entry_id AND jp.line_number=1
               AND jp.account_id=target.policy_liability_id AND jp.direction='debit'
               AND jp.amount=target.allocated_amount AND jp.currency='IRR'
       )
       OR NOT EXISTS (
            SELECT 1 FROM financial_core_journalposting jp
             WHERE jp.entry_id=target.journal_entry_id AND jp.line_number=2
               AND jp.account_id=target.revenue_account_id AND jp.direction='credit'
               AND jp.amount=target.allocated_amount AND jp.currency='IRR'
       ) THEN
        RAISE EXCEPTION 'revenue recognition engine graph is incoherent' USING ERRCODE='23514';
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION financial_core_validate_revenue_recognition_engine_trigger()
RETURNS trigger AS $$
DECLARE recognition_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'financial_core_revenuerecognition' THEN
        recognition_id := NEW.id;
    ELSIF TG_TABLE_NAME = 'financial_core_revenuerecognitionworkitem' THEN
        SELECT id INTO recognition_id FROM financial_core_revenuerecognition
         WHERE work_item_id = NEW.id AND command_contract_version='revenue-recognition-engine-v1';
        IF NEW.recognition_contract_version='revenue-recognition-engine-v1'
           AND NEW.status='completed' AND recognition_id IS NULL THEN
            RAISE EXCEPTION 'completed recognition work requires its exact recognition' USING ERRCODE='23514';
        END IF;
    ELSE
        SELECT r.id INTO recognition_id
          FROM financial_core_revenuerecognition r
         WHERE r.journal_entry_id = COALESCE(NEW.entry_id, OLD.entry_id)
           AND r.command_contract_version='revenue-recognition-engine-v1';
    END IF;
    IF recognition_id IS NOT NULL THEN
        PERFORM financial_core_validate_revenue_recognition_engine(recognition_id);
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION financial_core_protect_revenue_work_terminal()
RETURNS trigger AS $$
BEGIN
    IF OLD.recognition_contract_version='revenue-recognition-engine-v1'
       AND OLD.status IN ('completed','review_required','canceled')
       AND ROW(NEW.status,NEW.attempt_count,NEW.next_attempt_at,NEW.claim_owner,
               NEW.claim_token,NEW.claimed_at,NEW.claim_expires_at,NEW.completed_at,
               NEW.failure_classification,NEW.safe_result,NEW.version)
           IS DISTINCT FROM
           ROW(OLD.status,OLD.attempt_count,OLD.next_attempt_at,OLD.claim_owner,
               OLD.claim_token,OLD.claimed_at,OLD.claim_expires_at,OLD.completed_at,
               OLD.failure_classification,OLD.safe_result,OLD.version) THEN
        RAISE EXCEPTION 'terminal revenue recognition work is immutable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER financial_core_revenue_work_terminal_immutable
BEFORE UPDATE ON financial_core_revenuerecognitionworkitem
FOR EACH ROW EXECUTE FUNCTION financial_core_protect_revenue_work_terminal();

CREATE CONSTRAINT TRIGGER financial_core_revenue_engine_graph_guard
AFTER INSERT ON financial_core_revenuerecognition
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_revenue_recognition_engine_trigger();

CREATE CONSTRAINT TRIGGER financial_core_revenue_engine_work_guard
AFTER INSERT OR UPDATE ON financial_core_revenuerecognitionworkitem
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_revenue_recognition_engine_trigger();

CREATE CONSTRAINT TRIGGER financial_core_revenue_engine_posting_guard
AFTER INSERT OR UPDATE OR DELETE ON financial_core_journalposting
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION financial_core_validate_revenue_recognition_engine_trigger();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_revenue_engine_posting_guard ON financial_core_journalposting;
DROP TRIGGER IF EXISTS financial_core_revenue_engine_work_guard ON financial_core_revenuerecognitionworkitem;
DROP TRIGGER IF EXISTS financial_core_revenue_engine_graph_guard ON financial_core_revenuerecognition;
DROP TRIGGER IF EXISTS financial_core_revenue_work_terminal_immutable ON financial_core_revenuerecognitionworkitem;
DROP FUNCTION IF EXISTS financial_core_protect_revenue_work_terminal();
DROP FUNCTION IF EXISTS financial_core_validate_revenue_recognition_engine_trigger();
DROP FUNCTION IF EXISTS financial_core_validate_revenue_recognition_engine(bigint);
DROP INDEX IF EXISTS fin_revenue_engine_allocation_once;
"""


class Migration(migrations.Migration):
    dependencies = [("financial_core", "0026_postgresql_launch_satisfaction_evidence_guards")]
    operations = [migrations.RunSQL(FORWARD_SQL, REVERSE_SQL)]
