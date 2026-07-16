from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('financial_core', '0002_postgresql_financial_guards'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='financialaccount',
            constraint=models.CheckConstraint(check=models.Q(('account_type__in', ['asset', 'liability', 'equity', 'revenue', 'expense'])), name='fin_account_type_valid'),
        ),
        migrations.AddConstraint(
            model_name='financialaccount',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['active', 'frozen', 'closed'])), name='fin_account_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.CheckConstraint(check=models.Q(('actor_type__in', ['customer', 'system', 'provider', 'admin', 'support', 'reconciliation'])), name='fin_event_actor_type_valid'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['in_progress', 'completed', 'failed'])), name='fin_idempotency_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('completed_at__isnull', True), ('status', 'in_progress')), models.Q(('completed_at__isnull', False), ('status__in', ('completed', 'failed'))), _connector='OR'), name='fin_idempotency_completed_at_consistent'),
        ),
        migrations.AddConstraint(
            model_name='journalposting',
            constraint=models.CheckConstraint(check=models.Q(('direction__in', ['debit', 'credit'])), name='fin_posting_direction_valid'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('collection_status__in', ['open', 'processing', 'partially_paid', 'paid_pending_finalization', 'paid', 'review', 'canceled'])), name='fin_payment_collection_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('refund_status__in', ['not_refunded', 'partially_refunded', 'refunded', 'refund_review'])), name='fin_payment_refund_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('tender_type__in', ['external_provider', 'gift_card', 'installment', 'internal_adjustment'])), name='fin_attempt_tender_valid'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['created', 'requires_customer_action', 'processing', 'succeeded', 'definitive_failed', 'outcome_unknown', 'review'])), name='fin_attempt_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('operation_type__in', ['sale', 'authorize', 'capture', 'void', 'refund', 'chargeback'])), name='fin_tx_operation_valid'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['created', 'requesting', 'pending_customer', 'pending_provider', 'callback_received', 'verifying', 'succeeded', 'declined', 'canceled', 'expired', 'outcome_unknown', 'review'])), name='fin_tx_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('completed_at__isnull', False), ('status__in', ('succeeded', 'declined', 'canceled', 'expired'))), models.Q(('completed_at__isnull', True), ('status__in', ('created', 'requesting', 'pending_customer', 'pending_provider', 'callback_received', 'verifying', 'outcome_unknown', 'review'))), _connector='OR'), name='fin_tx_completed_at_consistent'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.CheckConstraint(check=models.Q(('severity__in', ['low', 'medium', 'high', 'critical'])), name='fin_recon_severity_valid'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['open', 'reviewing', 'resolved', 'accepted'])), name='fin_recon_finding_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('resolved_at__isnull', False), ('status__in', ('resolved', 'accepted'))), models.Q(('resolved_at__isnull', True), ('status__in', ('open', 'reviewing'))), _connector='OR'), name='fin_recon_finding_time_consistent'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationrun',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['created', 'running', 'completed', 'failed'])), name='fin_recon_run_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationrun',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('completed_at__isnull', True), ('started_at__isnull', True), ('status', 'created')), models.Q(('completed_at__isnull', True), ('started_at__isnull', False), ('status', 'running')), models.Q(('completed_at__isnull', False), ('started_at__isnull', False), ('status__in', ('completed', 'failed'))), _connector='OR'), name='fin_recon_run_times_consistent'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('reason__in', ['provider_state_unclear', 'paid_finalization_pending', 'amount_mismatch', 'currency_mismatch', 'duplicate_provider_reference', 'late_payment', 'inventory_conflict', 'reconciliation_mismatch', 'fraud_risk', 'invariant_violation'])), name='fin_review_reason_valid'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('severity__in', ['low', 'medium', 'high', 'critical'])), name='fin_review_severity_valid'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['open', 'investigating', 'approval_pending', 'resolved', 'canceled'])), name='fin_review_status_valid'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('opened_by_type__in', ['customer', 'system', 'provider', 'admin', 'support', 'reconciliation'])), name='fin_review_actor_type_valid'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('resolved_at__isnull', False), ('status', 'resolved'), models.Q(('resolution_code', ''), _negated=True)), models.Q(models.Q(('status', 'resolved'), _negated=True), ('resolution_code', ''), ('resolved_at__isnull', True)), _connector='OR'), name='fin_review_resolution_consistent'),
        ),
    ]
