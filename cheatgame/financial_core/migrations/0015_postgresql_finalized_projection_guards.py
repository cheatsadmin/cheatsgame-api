from django.db import migrations


def install_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        CREATE OR REPLACE FUNCTION financial_core_protect_core_order()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM financial_core_payment p WHERE p.order_id = OLD.id) THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Financial Core Order cannot be deleted' USING ERRCODE = '55000';
                END IF;
                IF ROW(
                    OLD.user_id, OLD.discount_id, OLD.total_price, OLD.total_price_discount,
                    OLD.schedule_id, OLD.shipping_address_id, OLD.shipping_method_id,
                    OLD.is_game, OLD.checkout_id
                ) IS DISTINCT FROM ROW(
                    NEW.user_id, NEW.discount_id, NEW.total_price, NEW.total_price_discount,
                    NEW.schedule_id, NEW.shipping_address_id, NEW.shipping_method_id,
                    NEW.is_game, NEW.checkout_id
                ) THEN
                    RAISE EXCEPTION 'Financial Core Order commercial identity is immutable' USING ERRCODE = '55000';
                END IF;
            END IF;

            IF EXISTS (
                SELECT 1 FROM financial_core_commercialfinalization f WHERE f.order_id = OLD.id
            ) THEN
                IF NEW.payment_status <> 3 THEN
                    RAISE EXCEPTION 'Finalized Order payment projection must remain paid' USING ERRCODE = '55000';
                END IF;
                IF NOT (
                    (OLD.fulfillment_status = 'not_started' AND NEW.fulfillment_status = 'processing'
                     AND NEW.payment_status = 3)
                    OR
                    (OLD.fulfillment_status = 'processing' AND NEW.fulfillment_status IN ('processing', 'sending', 'delivered'))
                    OR (OLD.fulfillment_status = 'sending' AND NEW.fulfillment_status IN ('sending', 'delivered'))
                    OR (OLD.fulfillment_status = 'delivered' AND NEW.fulfillment_status = 'delivered')
                ) THEN
                    RAISE EXCEPTION 'Finalized Order fulfillment projection cannot move backward' USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION financial_core_protect_finalized_checkout()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM shop_order o
                  JOIN financial_core_commercialfinalization f ON f.order_id = o.id
                 WHERE o.checkout_id = OLD.id
            ) AND NEW.status <> 'paid' THEN
                RAISE EXCEPTION 'Finalized Checkout projection must remain paid' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER financial_core_finalized_checkout_protected
        BEFORE UPDATE OF status ON shop_checkout
        FOR EACH ROW EXECUTE FUNCTION financial_core_protect_finalized_checkout();
        """
    )


def remove_guards(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DROP TRIGGER IF EXISTS financial_core_finalized_checkout_protected ON shop_checkout;
        DROP FUNCTION IF EXISTS financial_core_protect_finalized_checkout();

        CREATE OR REPLACE FUNCTION financial_core_protect_core_order()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM financial_core_payment p WHERE p.order_id = OLD.id) THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'Financial Core Order cannot be deleted' USING ERRCODE = '55000';
                END IF;
                IF ROW(
                    OLD.user_id, OLD.discount_id, OLD.total_price, OLD.total_price_discount,
                    OLD.schedule_id, OLD.shipping_address_id, OLD.shipping_method_id,
                    OLD.is_game, OLD.checkout_id
                ) IS DISTINCT FROM ROW(
                    NEW.user_id, NEW.discount_id, NEW.total_price, NEW.total_price_discount,
                    NEW.schedule_id, NEW.shipping_address_id, NEW.shipping_method_id,
                    NEW.is_game, NEW.checkout_id
                ) THEN
                    RAISE EXCEPTION 'Financial Core Order commercial identity is immutable' USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;
        """
    )


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0014_commercialaccountingpolicyversion_fin_commercial_policy_accounts_distinct"),
    ]
    operations = [migrations.RunPython(install_guards, remove_guards)]
