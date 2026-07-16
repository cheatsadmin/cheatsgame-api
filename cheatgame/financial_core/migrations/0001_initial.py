from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.db.models.expressions
import django.utils.timezone
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('digital_products', '0004_digitalinventoryreservation'),
        ('shop', '0019_checkoutline_commerce_authority'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FinancialAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('key', models.CharField(max_length=128, unique=True)),
                ('name', models.CharField(max_length=200)),
                ('account_type', models.CharField(choices=[('asset', 'ASSET'), ('liability', 'LIABILITY'), ('equity', 'EQUITY'), ('revenue', 'REVENUE'), ('expense', 'EXPENSE')], max_length=16)),
                ('currency', models.CharField(default='IRR', max_length=3)),
                ('status', models.CharField(choices=[('active', 'ACTIVE'), ('frozen', 'FROZEN'), ('closed', 'CLOSED')], db_index=True, default='active', max_length=16)),
            ],
        ),
        migrations.CreateModel(
            name='FinancialEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('aggregate_type', models.CharField(max_length=64)),
                ('aggregate_id', models.CharField(max_length=128)),
                ('aggregate_version', models.PositiveIntegerField()),
                ('event_type', models.CharField(db_index=True, max_length=128)),
                ('actor_type', models.CharField(choices=[('customer', 'CUSTOMER'), ('system', 'SYSTEM'), ('provider', 'PROVIDER'), ('admin', 'ADMIN'), ('support', 'SUPPORT'), ('reconciliation', 'RECONCILIATION')], max_length=20)),
                ('actor_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('idempotency_key', models.CharField(max_length=200, unique=True)),
                ('correlation_id', models.UUIDField(db_index=True, default=uuid.uuid4)),
                ('causation_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('occurred_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='IdempotencyRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('scope', models.CharField(max_length=200)),
                ('key', models.CharField(max_length=200)),
                ('request_hash', models.CharField(max_length=64)),
                ('status', models.CharField(choices=[('in_progress', 'IN_PROGRESS'), ('completed', 'COMPLETED'), ('failed', 'FAILED')], db_index=True, default='in_progress', max_length=16)),
                ('result_type', models.CharField(blank=True, max_length=64)),
                ('result_id', models.CharField(blank=True, max_length=128)),
                ('safe_response', models.JSONField(blank=True, default=dict)),
                ('error_code', models.CharField(blank=True, max_length=100)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='JournalEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('source_type', models.CharField(max_length=64)),
                ('source_id', models.CharField(max_length=128)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('correlation_id', models.UUIDField(db_index=True, default=uuid.uuid4)),
                ('occurred_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('posted_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('description', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='JournalPosting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('line_number', models.PositiveIntegerField()),
                ('direction', models.CharField(choices=[('debit', 'DEBIT'), ('credit', 'CREDIT')], max_length=8)),
                ('amount', models.DecimalField(decimal_places=0, max_digits=20)),
                ('currency', models.CharField(default='IRR', max_length=3)),
                ('memo', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('amount_due', models.DecimalField(decimal_places=0, max_digits=20)),
                ('confirmed_amount', models.DecimalField(decimal_places=0, default=0, max_digits=20)),
                ('refunded_amount', models.DecimalField(decimal_places=0, default=0, max_digits=20)),
                ('currency', models.CharField(default='IRR', max_length=3)),
                ('collection_status', models.CharField(choices=[('open', 'OPEN'), ('processing', 'PROCESSING'), ('partially_paid', 'PARTIALLY_PAID'), ('paid_pending_finalization', 'PAID_PENDING_FINALIZATION'), ('paid', 'PAID'), ('review', 'REVIEW'), ('canceled', 'CANCELED')], db_index=True, default='open', max_length=32)),
                ('refund_status', models.CharField(choices=[('not_refunded', 'NOT_REFUNDED'), ('partially_refunded', 'PARTIALLY_REFUNDED'), ('refunded', 'REFUNDED'), ('refund_review', 'REFUND_REVIEW')], db_index=True, default='not_refunded', max_length=24)),
                ('version', models.PositiveIntegerField(default=1)),
            ],
        ),
        migrations.CreateModel(
            name='PaymentAttempt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('sequence', models.PositiveIntegerField()),
                ('requested_amount', models.DecimalField(decimal_places=0, max_digits=20)),
                ('currency', models.CharField(default='IRR', max_length=3)),
                ('tender_type', models.CharField(choices=[('external_provider', 'EXTERNAL_PROVIDER'), ('gift_card', 'GIFT_CARD'), ('installment', 'INSTALLMENT'), ('internal_adjustment', 'INTERNAL_ADJUSTMENT')], max_length=32)),
                ('provider', models.CharField(blank=True, max_length=64)),
                ('merchant_account_ref', models.CharField(blank=True, max_length=128)),
                ('status', models.CharField(choices=[('created', 'CREATED'), ('requires_customer_action', 'REQUIRES_CUSTOMER_ACTION'), ('processing', 'PROCESSING'), ('succeeded', 'SUCCEEDED'), ('definitive_failed', 'DEFINITIVE_FAILED'), ('outcome_unknown', 'OUTCOME_UNKNOWN'), ('review', 'REVIEW')], db_index=True, default='created', max_length=32)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('request_hash', models.CharField(max_length=64)),
                ('claim_token', models.UUIDField(blank=True, null=True)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('version', models.PositiveIntegerField(default=1)),
            ],
        ),
        migrations.CreateModel(
            name='PaymentTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('sequence', models.PositiveIntegerField()),
                ('operation_type', models.CharField(choices=[('sale', 'SALE'), ('authorize', 'AUTHORIZE'), ('capture', 'CAPTURE'), ('void', 'VOID'), ('refund', 'REFUND'), ('chargeback', 'CHARGEBACK')], max_length=16)),
                ('provider', models.CharField(max_length=64)),
                ('merchant_account_ref', models.CharField(max_length=128)),
                ('merchant_reference', models.CharField(max_length=128)),
                ('amount', models.DecimalField(decimal_places=0, max_digits=20)),
                ('currency', models.CharField(default='IRR', max_length=3)),
                ('provider_amount', models.DecimalField(decimal_places=0, max_digits=20)),
                ('provider_unit', models.CharField(max_length=16)),
                ('status', models.CharField(choices=[('created', 'CREATED'), ('requesting', 'REQUESTING'), ('pending_customer', 'PENDING_CUSTOMER'), ('pending_provider', 'PENDING_PROVIDER'), ('callback_received', 'CALLBACK_RECEIVED'), ('verifying', 'VERIFYING'), ('succeeded', 'SUCCEEDED'), ('declined', 'DECLINED'), ('canceled', 'CANCELED'), ('expired', 'EXPIRED'), ('outcome_unknown', 'OUTCOME_UNKNOWN'), ('review', 'REVIEW')], db_index=True, default='created', max_length=32)),
                ('provider_authority', models.CharField(blank=True, max_length=128, null=True)),
                ('provider_reference', models.CharField(blank=True, max_length=128, null=True)),
                ('evidence_hash', models.CharField(blank=True, max_length=64)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('claim_token', models.UUIDField(blank=True, null=True)),
                ('claimed_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('version', models.PositiveIntegerField(default=1)),
            ],
        ),
        migrations.CreateModel(
            name='ReconciliationFinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('finding_key', models.CharField(max_length=200)),
                ('finding_type', models.CharField(db_index=True, max_length=100)),
                ('severity', models.CharField(choices=[('low', 'LOW'), ('medium', 'MEDIUM'), ('high', 'HIGH'), ('critical', 'CRITICAL')], db_index=True, max_length=16)),
                ('status', models.CharField(choices=[('open', 'OPEN'), ('reviewing', 'REVIEWING'), ('resolved', 'RESOLVED'), ('accepted', 'ACCEPTED')], db_index=True, default='open', max_length=16)),
                ('expected', models.JSONField(blank=True, default=dict)),
                ('actual', models.JSONField(blank=True, default=dict)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='ReconciliationRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('run_type', models.CharField(max_length=64)),
                ('period_start', models.DateTimeField()),
                ('period_end', models.DateTimeField()),
                ('status', models.CharField(choices=[('created', 'CREATED'), ('running', 'RUNNING'), ('completed', 'COMPLETED'), ('failed', 'FAILED')], db_index=True, default='created', max_length=16)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('records_scanned', models.PositiveBigIntegerField(default=0)),
                ('findings_count', models.PositiveBigIntegerField(default=0)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='ReviewCase',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('reason', models.CharField(choices=[('provider_state_unclear', 'PROVIDER_STATE_UNCLEAR'), ('paid_finalization_pending', 'PAID_FINALIZATION_PENDING'), ('amount_mismatch', 'AMOUNT_MISMATCH'), ('currency_mismatch', 'CURRENCY_MISMATCH'), ('duplicate_provider_reference', 'DUPLICATE_PROVIDER_REFERENCE'), ('late_payment', 'LATE_PAYMENT'), ('inventory_conflict', 'INVENTORY_CONFLICT'), ('reconciliation_mismatch', 'RECONCILIATION_MISMATCH'), ('fraud_risk', 'FRAUD_RISK'), ('invariant_violation', 'INVARIANT_VIOLATION')], db_index=True, max_length=48)),
                ('severity', models.CharField(choices=[('low', 'LOW'), ('medium', 'MEDIUM'), ('high', 'HIGH'), ('critical', 'CRITICAL')], db_index=True, max_length=16)),
                ('status', models.CharField(choices=[('open', 'OPEN'), ('investigating', 'INVESTIGATING'), ('approval_pending', 'APPROVAL_PENDING'), ('resolved', 'RESOLVED'), ('canceled', 'CANCELED')], db_index=True, default='open', max_length=24)),
                ('opened_by_type', models.CharField(choices=[('customer', 'CUSTOMER'), ('system', 'SYSTEM'), ('provider', 'PROVIDER'), ('admin', 'ADMIN'), ('support', 'SUPPORT'), ('reconciliation', 'RECONCILIATION')], max_length=20)),
                ('summary', models.CharField(max_length=1000)),
                ('resolution_code', models.CharField(blank=True, max_length=64)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('version', models.PositiveIntegerField(default=1)),
                ('assigned_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='assigned_financial_review_cases', to=settings.AUTH_USER_MODEL)),
                ('attempt', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='review_cases', to='financial_core.paymentattempt')),
                ('opened_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='opened_financial_review_cases', to=settings.AUTH_USER_MODEL)),
                ('order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='financial_review_cases', to='shop.order')),
                ('payment', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='review_cases', to='financial_core.payment')),
                ('transaction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='review_cases', to='financial_core.paymenttransaction')),
            ],
        ),
        migrations.CreateModel(
            name='ReviewAction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action_type', models.CharField(max_length=64)),
                ('reason_code', models.CharField(max_length=64)),
                ('note', models.CharField(blank=True, max_length=1000)),
                ('requires_approval', models.BooleanField(default=False)),
                ('idempotency_key', models.UUIDField(editable=False, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='financial_review_actions', to=settings.AUTH_USER_MODEL)),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='approved_financial_review_actions', to=settings.AUTH_USER_MODEL)),
                ('review_case', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='actions', to='financial_core.reviewcase')),
            ],
        ),
        migrations.AddIndex(
            model_name='reconciliationrun',
            index=models.Index(fields=['run_type', 'period_start'], name='fin_recon_type_period'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationrun',
            constraint=models.CheckConstraint(check=models.Q(('period_end__gt', django.db.models.expressions.F('period_start'))), name='fin_recon_period_valid'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationrun',
            constraint=models.CheckConstraint(check=models.Q(('run_type', ''), _negated=True), name='fin_recon_type_nonempty'),
        ),
        migrations.AddField(
            model_name='reconciliationfinding',
            name='payment',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reconciliation_findings', to='financial_core.payment'),
        ),
        migrations.AddField(
            model_name='reconciliationfinding',
            name='review_case',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reconciliation_findings', to='financial_core.reviewcase'),
        ),
        migrations.AddField(
            model_name='reconciliationfinding',
            name='run',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='findings', to='financial_core.reconciliationrun'),
        ),
        migrations.AddField(
            model_name='reconciliationfinding',
            name='transaction',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reconciliation_findings', to='financial_core.paymenttransaction'),
        ),
        migrations.AddField(
            model_name='paymenttransaction',
            name='attempt',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='transactions', to='financial_core.paymentattempt'),
        ),
        migrations.AddField(
            model_name='paymenttransaction',
            name='parent',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='child_transactions', to='financial_core.paymenttransaction'),
        ),
        migrations.AddField(
            model_name='paymentattempt',
            name='payment',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attempts', to='financial_core.payment'),
        ),
        migrations.AddField(
            model_name='payment',
            name='order',
            field=models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='financial_payment', to='shop.order'),
        ),
        migrations.AddField(
            model_name='journalposting',
            name='account',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='postings', to='financial_core.financialaccount'),
        ),
        migrations.AddField(
            model_name='journalposting',
            name='entry',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='postings', to='financial_core.journalentry'),
        ),
        migrations.AddIndex(
            model_name='journalentry',
            index=models.Index(fields=['source_type', 'occurred_at'], name='fin_journal_source_time'),
        ),
        migrations.AddConstraint(
            model_name='journalentry',
            constraint=models.UniqueConstraint(fields=('source_type', 'source_id'), name='fin_journal_source_uniq'),
        ),
        migrations.AddConstraint(
            model_name='journalentry',
            constraint=models.CheckConstraint(check=models.Q(('source_type', ''), _negated=True), name='fin_journal_source_type_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='journalentry',
            constraint=models.CheckConstraint(check=models.Q(('source_id', ''), _negated=True), name='fin_journal_source_id_nonempty'),
        ),
        migrations.AddIndex(
            model_name='idempotencyrecord',
            index=models.Index(fields=['status', 'created_at'], name='fin_idempotency_status_time'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.UniqueConstraint(fields=('scope', 'key'), name='fin_idempotency_scope_key_uniq'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.CheckConstraint(check=models.Q(('scope', ''), _negated=True), name='fin_idempotency_scope_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.CheckConstraint(check=models.Q(('key', ''), _negated=True), name='fin_idempotency_key_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='idempotencyrecord',
            constraint=models.CheckConstraint(check=models.Q(('request_hash', ''), _negated=True), name='fin_idempotency_hash_nonempty'),
        ),
        migrations.AddIndex(
            model_name='financialevent',
            index=models.Index(fields=['aggregate_type', 'aggregate_id', 'created_at'], name='fin_event_timeline'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.UniqueConstraint(fields=('aggregate_type', 'aggregate_id', 'aggregate_version'), name='fin_event_aggregate_version_uniq'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.CheckConstraint(check=models.Q(('aggregate_version__gt', 0)), name='fin_event_version_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.CheckConstraint(check=models.Q(('aggregate_type', ''), _negated=True), name='fin_event_aggregate_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.CheckConstraint(check=models.Q(('aggregate_id', ''), _negated=True), name='fin_event_id_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='financialevent',
            constraint=models.CheckConstraint(check=models.Q(('event_type', ''), _negated=True), name='fin_event_type_nonempty'),
        ),
        migrations.AddIndex(
            model_name='financialaccount',
            index=models.Index(fields=['account_type', 'currency'], name='fin_account_type_currency'),
        ),
        migrations.AddConstraint(
            model_name='financialaccount',
            constraint=models.CheckConstraint(check=models.Q(('key', ''), _negated=True), name='fin_account_key_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='financialaccount',
            constraint=models.CheckConstraint(check=models.Q(('currency', 'IRR')), name='fin_account_currency_irr'),
        ),
        migrations.AddIndex(
            model_name='reviewcase',
            index=models.Index(fields=['status', 'severity', 'created_at'], name='fin_review_queue'),
        ),
        migrations.AddIndex(
            model_name='reviewcase',
            index=models.Index(fields=['assigned_to', 'status'], name='fin_review_assignee_status'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('order__isnull', False), ('payment__isnull', False), ('attempt__isnull', False), ('transaction__isnull', False), _connector='OR'), name='fin_review_aggregate_required'),
        ),
        migrations.AddConstraint(
            model_name='reviewcase',
            constraint=models.CheckConstraint(check=models.Q(('summary', ''), _negated=True), name='fin_review_summary_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='reviewaction',
            constraint=models.CheckConstraint(check=models.Q(('action_type', ''), _negated=True), name='fin_review_action_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='reviewaction',
            constraint=models.CheckConstraint(check=models.Q(('reason_code', ''), _negated=True), name='fin_review_reason_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='reviewaction',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('approved_by__isnull', True), ('requires_approval', False)), ('requires_approval', True), _connector='OR'), name='fin_review_approval_consistent'),
        ),
        migrations.AddConstraint(
            model_name='reviewaction',
            constraint=models.CheckConstraint(check=models.Q(('approved_by__isnull', True), models.Q(('approved_by', models.F('actor')), _negated=True), _connector='OR'), name='fin_review_maker_checker_distinct'),
        ),
        migrations.AddIndex(
            model_name='reconciliationfinding',
            index=models.Index(fields=['status', 'severity', 'created_at'], name='fin_recon_finding_queue'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.UniqueConstraint(fields=('run', 'finding_key'), name='fin_recon_finding_key_uniq'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.CheckConstraint(check=models.Q(('finding_key', ''), _negated=True), name='fin_recon_key_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='reconciliationfinding',
            constraint=models.CheckConstraint(check=models.Q(('finding_type', ''), _negated=True), name='fin_recon_finding_type_nonempty'),
        ),
        migrations.AddIndex(
            model_name='paymenttransaction',
            index=models.Index(fields=['attempt', 'status'], name='fin_tx_attempt_status'),
        ),
        migrations.AddIndex(
            model_name='paymenttransaction',
            index=models.Index(fields=['provider', 'status', 'created_at'], name='fin_tx_provider_status_time'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.UniqueConstraint(fields=('attempt', 'sequence'), name='fin_tx_attempt_sequence_uniq'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.UniqueConstraint(fields=('provider', 'merchant_account_ref', 'merchant_reference'), name='fin_tx_merchant_reference_uniq'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.UniqueConstraint(condition=models.Q(('provider_authority__isnull', False)), fields=('provider', 'merchant_account_ref', 'provider_authority'), name='fin_tx_provider_authority_uniq'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.UniqueConstraint(condition=models.Q(('provider_reference__isnull', False)), fields=('provider', 'merchant_account_ref', 'provider_reference'), name='fin_tx_provider_reference_uniq'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('sequence__gt', 0)), name='fin_tx_sequence_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='fin_tx_amount_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('provider_amount__gt', 0)), name='fin_tx_provider_amount_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('currency', 'IRR')), name='fin_tx_currency_irr'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('provider_unit', 'IRR')), name='fin_tx_provider_unit_irr'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('provider', ''), _negated=True), name='fin_tx_provider_nonempty'),
        ),
        migrations.AddConstraint(
            model_name='paymenttransaction',
            constraint=models.CheckConstraint(check=models.Q(('merchant_reference', ''), _negated=True), name='fin_tx_merchant_ref_nonempty'),
        ),
        migrations.AddIndex(
            model_name='paymentattempt',
            index=models.Index(fields=['payment', 'status'], name='fin_attempt_payment_status'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.UniqueConstraint(fields=('payment', 'sequence'), name='fin_attempt_payment_sequence_uniq'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('sequence__gt', 0)), name='fin_attempt_sequence_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('requested_amount__gt', 0)), name='fin_attempt_amount_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('currency', 'IRR')), name='fin_attempt_currency_irr'),
        ),
        migrations.AddConstraint(
            model_name='paymentattempt',
            constraint=models.CheckConstraint(check=models.Q(('request_hash', ''), _negated=True), name='fin_attempt_hash_nonempty'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=['collection_status', 'created_at'], name='fin_pay_collect_created'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=['refund_status', 'created_at'], name='fin_pay_refund_created'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('amount_due__gt', 0)), name='fin_payment_amount_due_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('confirmed_amount__gte', 0)), name='fin_payment_confirmed_gte_zero'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('refunded_amount__gte', 0)), name='fin_payment_refunded_gte_zero'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('confirmed_amount__lte', django.db.models.expressions.F('amount_due'))), name='fin_payment_confirmed_lte_due'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('refunded_amount__lte', django.db.models.expressions.F('confirmed_amount'))), name='fin_payment_refunded_lte_confirmed'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(('currency', 'IRR')), name='fin_payment_currency_irr'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(check=models.Q(models.Q(('collection_status__in', ('paid_pending_finalization', 'paid')), _negated=True), ('confirmed_amount', django.db.models.expressions.F('amount_due')), _connector='OR'), name='fin_payment_paid_amount_complete'),
        ),
        migrations.AddIndex(
            model_name='journalposting',
            index=models.Index(fields=['account', 'created_at'], name='fin_posting_account_time'),
        ),
        migrations.AddConstraint(
            model_name='journalposting',
            constraint=models.UniqueConstraint(fields=('entry', 'line_number'), name='fin_posting_entry_line_uniq'),
        ),
        migrations.AddConstraint(
            model_name='journalposting',
            constraint=models.CheckConstraint(check=models.Q(('line_number__gt', 0)), name='fin_posting_line_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='journalposting',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='fin_posting_amount_gt_zero'),
        ),
        migrations.AddConstraint(
            model_name='journalposting',
            constraint=models.CheckConstraint(check=models.Q(('currency', 'IRR')), name='fin_posting_currency_irr'),
        ),
    ]
