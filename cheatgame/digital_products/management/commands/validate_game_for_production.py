import json

from django.core.management.base import BaseCommand, CommandError

from cheatgame.digital_products.catalog_expansion import validate_game_for_production
from cheatgame.product.models import Product


class Command(BaseCommand):
    help = "Read-only state/content/SEO/media/commerce readiness for one Digital Game."

    def add_arguments(self, parser):
        parser.add_argument("product_id", type=int)
        parser.add_argument("--pretty", action="store_true")

    def handle(self, *args, **options):
        del args
        try:
            result = validate_game_for_production(options["product_id"])
        except Product.DoesNotExist as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2 if options["pretty"] else None,
                sort_keys=True,
            )
        )
