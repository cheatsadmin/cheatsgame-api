# Commercial Finalization v2 launch contract

Commercial Finalization v2 is a dormant, server-gated accounting contract for
new homogeneous Standard and Digital finalizations. It preserves every API-08
commercial transition and replaces only the commercial Journal destination:
customer unapplied funds are reclassified to a frozen contract liability.

The launch contract supports **included shipping only**. Standard shipping is
included in the deterministic goods-obligation graph. A policy whose
`shipping_treatment` is anything other than `included` fails closed before any
commercial effect. Distinct shipping obligations are an approved future
extension and are not implemented or activated by v2 launch scope.

Each v2 finalization freezes one point-in-time `RecognitionPolicyVersion`. Its
`policy_fingerprint` is persisted as `recognition_policy_set_digest` and is
re-derived from the immutable obligation graph by PostgreSQL at commit. Legacy
finalizations and Journals remain immutable and replay through the legacy
contract. The v2 setting defaults off; there is no public API, worker, signal,
task, or outbox consumer activation.
