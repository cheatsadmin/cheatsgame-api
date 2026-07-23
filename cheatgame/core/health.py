import logging

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse
from django.views.decorators.http import require_GET


logger = logging.getLogger(__name__)


@require_GET
def liveness(request):
    return JsonResponse({"status": "alive"})


@require_GET
def readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(targets):
            return JsonResponse({"status": "not_ready", "reason": "schema_not_ready"}, status=503)
    except Exception:
        logger.exception("Readiness check failed.")
        return JsonResponse(
            {"status": "not_ready", "reason": "database_or_schema_unavailable"},
            status=503,
        )
    return JsonResponse({"status": "ready"})
