from django.conf import settings
from django.core.checks import Error, register


@register()
def zarinpal_configuration_check(app_configs, **kwargs):
    del app_configs, kwargs
    if not getattr(settings, "FINANCIAL_ZARINPAL_ENABLED", False):
        return []
    try:
        from cheatgame.financial_core.services.zarinpal import ZarinpalAdapter

        ZarinpalAdapter.from_settings()
    except Exception as exc:
        return [
            Error(
                "Financial Core Zarinpal configuration is invalid.",
                hint=str(exc),
                id="financial_core.E001",
            )
        ]
    return []
