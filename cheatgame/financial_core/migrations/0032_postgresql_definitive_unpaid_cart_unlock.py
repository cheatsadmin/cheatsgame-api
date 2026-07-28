from django.db import migrations


FORWARD_SQL = r"""
CREATE OR REPLACE FUNCTION financial_core_check_api08_cart_projection()
RETURNS trigger AS $$
DECLARE
    finalization_id bigint;
    definitive_unpaid_termination boolean;
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
        SELECT EXISTS (
            SELECT 1
              FROM shop_checkout checkout
              JOIN shop_order payment_order
                ON payment_order.checkout_id = checkout.id
              JOIN financial_core_payment payment
                ON payment.order_id = payment_order.id
             WHERE checkout.id = OLD.active_checkout_id
               AND checkout.status = 'canceled'
               AND payment_order.payment_status = 2
               AND payment.collection_status = 'open'
               AND payment.confirmed_amount = 0
               AND NOT EXISTS (
                    SELECT 1
                      FROM financial_core_financialallocation allocation
                     WHERE allocation.payment_id = payment.id
               )
               AND EXISTS (
                    SELECT 1
                      FROM financial_core_paymentattempt attempt
                      JOIN financial_core_paymenttransaction transaction_obj
                        ON transaction_obj.attempt_id = attempt.id
                     WHERE attempt.payment_id = payment.id
                       AND attempt.status = 'definitive_failed'
                       AND transaction_obj.status IN ('declined', 'canceled', 'expired')
               )
               AND EXISTS (
                    SELECT 1
                      FROM digital_products_digitalinventoryreservation reservation
                     WHERE reservation.order_id = payment_order.id
               )
               AND NOT EXISTS (
                    SELECT 1
                      FROM digital_products_digitalinventoryreservation reservation
                     WHERE reservation.order_id = payment_order.id
                       AND reservation.state IN ('active', 'payment_hold', 'held_for_review')
               )
        ) INTO definitive_unpaid_termination;

        IF NOT definitive_unpaid_termination THEN
            SELECT f.id INTO finalization_id
              FROM financial_core_commercialfinalization f
              JOIN shop_order o ON o.id = f.order_id
             WHERE o.checkout_id = OLD.active_checkout_id;
            IF finalization_id IS NULL THEN
                RAISE EXCEPTION 'Cart unlock requires API-08 finalization or definitive-unpaid termination'
                    USING ERRCODE = '23514';
            END IF;
            PERFORM financial_core_validate_api08_finalization(finalization_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


REVERSE_SQL = r"""
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
"""


def install_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD_SQL)


def restore_guard(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE_SQL)


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0031_postgresql_recovered_digital_reservation_guard"),
        ("digital_products", "0013_digitalinventoryreservation_recovery_authorization_and_more"),
    ]

    operations = [
        migrations.RunPython(install_guard, restore_guard),
    ]
