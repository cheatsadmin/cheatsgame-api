from django.db import migrations


def _replace_guard(schema_editor, *, forward):
    if schema_editor.connection.vendor != "postgresql":
        return
    if forward:
        replacements = (
            (
                "FROM digital_products_digitalinventoryreservation WHERE order_id = target.order_id;",
                "FROM digital_products_digitalinventoryreservation "
                "WHERE order_id = target.order_id AND state = 'consumed';",
            ),
            (
                "WHERE r.order_id = target.order_id AND r.inventory_pool_id = c.inventory_pool_id",
                "WHERE r.order_id = target.order_id "
                "AND r.inventory_pool_id = c.inventory_pool_id AND r.state = 'consumed'",
            ),
        )
    else:
        replacements = tuple((new, old) for old, new in (
            (
                "FROM digital_products_digitalinventoryreservation WHERE order_id = target.order_id;",
                "FROM digital_products_digitalinventoryreservation "
                "WHERE order_id = target.order_id AND state = 'consumed';",
            ),
            (
                "WHERE r.order_id = target.order_id AND r.inventory_pool_id = c.inventory_pool_id",
                "WHERE r.order_id = target.order_id "
                "AND r.inventory_pool_id = c.inventory_pool_id AND r.state = 'consumed'",
            ),
        ))
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_get_functiondef(
                'financial_core_validate_api08_legacy_finalization(bigint)'::regprocedure
            )
            """
        )
        definition = cursor.fetchone()[0]
        for old, new in replacements:
            if old not in definition:
                raise RuntimeError(
                    "The API-08 legacy finalization guard does not match the expected prior definition."
                )
            definition = definition.replace(old, new, 1)
        cursor.execute(definition)


def install_guard(apps, schema_editor):
    _replace_guard(schema_editor, forward=True)


def restore_guard(apps, schema_editor):
    _replace_guard(schema_editor, forward=False)


class Migration(migrations.Migration):
    dependencies = [
        ("financial_core", "0030_postgresql_exceptional_recognition_guard"),
        ("digital_products", "0013_digitalinventoryreservation_recovery_authorization_and_more"),
    ]

    operations = [
        migrations.RunPython(install_guard, restore_guard),
    ]
