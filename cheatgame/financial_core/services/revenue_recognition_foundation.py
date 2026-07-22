"""Dormant, side-effect-free identity helpers for Revenue Recognition v1.

Sprint 1 deliberately exposes no command that can create obligations, consume
fulfillment evidence, or post revenue.  These helpers only give later commands
one stable, domain-separated identity contract.
"""

from uuid import UUID, uuid5

from cheatgame.financial_core.services.idempotency import canonical_request_hash


REVENUE_RECOGNITION_FOUNDATION_CONTRACT = "revenue-recognition-foundation-v1"
REVENUE_RECOGNITION_NAMESPACE = UUID("78db78e9-aad4-4bc8-b66a-49e5e54b2cc2")


def foundation_fingerprint(*, identity_type, identity):
    if not identity_type or not isinstance(identity, dict) or not identity:
        raise ValueError("A non-empty identity type and immutable identity are required.")
    return canonical_request_hash(
        {
            "contract": REVENUE_RECOGNITION_FOUNDATION_CONTRACT,
            "identity_type": identity_type,
            "identity": identity,
        }
    )


def deterministic_foundation_uuid(*, identity_type, identity):
    fingerprint = foundation_fingerprint(identity_type=identity_type, identity=identity)
    return uuid5(REVENUE_RECOGNITION_NAMESPACE, f"{identity_type}:{fingerprint}")
