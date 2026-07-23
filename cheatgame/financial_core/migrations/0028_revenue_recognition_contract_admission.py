from django.db import migrations, models


FORWARD_SQL = r"""
CREATE FUNCTION financial_core_enforce_revenue_engine_contract()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'financial_core_revenuerecognitionworkitem' THEN
        IF NEW.recognition_contract_version <> 'revenue-recognition-engine-v1' THEN
            RAISE EXCEPTION 'revenue recognition work contract is not approved'
                USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.command_contract_version <> 'revenue-recognition-engine-v1' THEN
        RAISE EXCEPTION 'revenue recognition contract is not approved'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER financial_core_revenue_work_contract_admitted
BEFORE INSERT OR UPDATE ON financial_core_revenuerecognitionworkitem
FOR EACH ROW EXECUTE FUNCTION financial_core_enforce_revenue_engine_contract();

CREATE TRIGGER financial_core_revenue_contract_admitted
BEFORE INSERT ON financial_core_revenuerecognition
FOR EACH ROW EXECUTE FUNCTION financial_core_enforce_revenue_engine_contract();
"""


REVERSE_SQL = r"""
DROP TRIGGER IF EXISTS financial_core_revenue_contract_admitted
ON financial_core_revenuerecognition;
DROP TRIGGER IF EXISTS financial_core_revenue_work_contract_admitted
ON financial_core_revenuerecognitionworkitem;
DROP FUNCTION IF EXISTS financial_core_enforce_revenue_engine_contract();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0027_postgresql_revenue_recognition_engine_guards"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="revenuerecognitionworkitem",
            constraint=models.CheckConstraint(
                check=models.Q(
                    recognition_contract_version="revenue-recognition-engine-v1"
                ),
                name="fin_rec_work_engine_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="revenuerecognition",
            constraint=models.CheckConstraint(
                check=models.Q(
                    command_contract_version="revenue-recognition-engine-v1"
                ),
                name="fin_revenue_engine_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="revenuerecognition",
            constraint=models.UniqueConstraint(
                fields=("consideration_allocation",),
                name="fin_revenue_allocation_once",
            ),
        ),
        migrations.RunSQL(FORWARD_SQL, REVERSE_SQL),
    ]
