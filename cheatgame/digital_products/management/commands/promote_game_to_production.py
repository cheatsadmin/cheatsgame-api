import hashlib
import json
from io import StringIO
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin
from urllib.request import Request, urlopen

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from cheatgame.digital_products.catalog_expansion import (
    PRODUCTION_READY,
    validate_game_for_production,
)
from cheatgame.digital_products.models import DigitalOffer, InventoryPool
from cheatgame.product.models import DeliveredVersion, Product, ProductSlugHistory


MANIFEST_NAME = "manifest.json"


def _sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CommandError("Promotion bundle manifest is unreadable or invalid JSON.") from exc


def _catalog_counts():
    return {
        "products": Product.objects.count(),
        "versions": DeliveredVersion.objects.count(),
        "offers": DigitalOffer.objects.count(),
        "inventory_pools": InventoryPool.objects.count(),
        "slug_history": ProductSlugHistory.objects.count(),
    }


class Command(BaseCommand):
    help = (
        "Prepare a checksum-bound single-game promotion bundle, or apply one "
        "idempotently to the current environment."
    )

    def add_arguments(self, parser):
        parser.add_argument("product_id", nargs="?", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--bundle-dir")
        parser.add_argument("--manifest-sha256")
        parser.add_argument("--site-url")
        parser.add_argument("--api-url")
        parser.add_argument("--pretty", action="store_true")

    def handle(self, *args, **options):
        del args
        if options["dry_run"] == options["apply"]:
            raise CommandError("Choose exactly one of --dry-run or --apply.")
        bundle_dir = Path(options["bundle_dir"]).resolve() if options["bundle_dir"] else None
        if options["dry_run"]:
            if options["product_id"] is None:
                raise CommandError("A Product ID is required for --dry-run.")
            result = self._prepare(options["product_id"], bundle_dir=bundle_dir)
        else:
            if options["product_id"] is not None:
                raise CommandError("Product ID belongs to source --dry-run, not target --apply.")
            if bundle_dir is None:
                raise CommandError("--bundle-dir is required for --apply.")
            if not options["manifest_sha256"]:
                raise CommandError("--manifest-sha256 is required for --apply.")
            result = self._apply(
                bundle_dir,
                manifest_sha256=options["manifest_sha256"],
                site_url=options["site_url"],
                api_url=options["api_url"],
            )
        self.stdout.write(
            json.dumps(
                result,
                ensure_ascii=False,
                indent=2 if options["pretty"] else None,
                sort_keys=True,
            )
        )

    def _prepare(self, product_id, *, bundle_dir):
        try:
            validation = validate_game_for_production(product_id)
        except Product.DoesNotExist as exc:
            raise CommandError(str(exc)) from exc

        raw = StringIO()
        call_command("catalog_promotion_manifest", stdout=raw)
        catalog = json.loads(raw.getvalue())
        record = next(
            (item for item in catalog["products"] if item["source_product_id"] == product_id),
            None,
        )
        if record is None:
            raise CommandError(f"Product {product_id} was absent from the catalog manifest.")
        record["classification"] = validation["classification"]
        record["review_reasons"] = validation["blockers"]
        record["validation"] = validation

        selected_links = [
            item
            for item in catalog.get("product_category_links", [])
            if item["product_slug"] == record["slug"]
        ]
        required_category_slugs = {
            slug for item in selected_links for slug in item["category_slugs"]
        }
        categories_by_slug = {
            item["slug"]: item for item in catalog.get("categories", [])
        }
        pending = list(required_category_slugs)
        while pending:
            category = categories_by_slug.get(pending.pop())
            if category and category.get("parent_slug") not in required_category_slugs:
                parent = category.get("parent_slug")
                if parent:
                    required_category_slugs.add(parent)
                    pending.append(parent)

        media = []
        for kind, descriptor in validation["media_objects"].items():
            item = dict(descriptor, kind=kind)
            if item.get("sha256"):
                item["bundle_name"] = f"{item['sha256']}{Path(item['storage_key']).suffix.lower()}"
            media.append(item)
        payload = {
            "schema": "cheatsg.catalog-promotion.v1",
            "workflow_schema": "cheatsg.digital-game-expansion.v1",
            "policy": "Only this validated PRODUCTION_READY Digital Game may be applied.",
            "categories": [
                category
                for category in catalog.get("categories", [])
                if category["slug"] in required_category_slugs
            ],
            "product_category_links": selected_links,
            "counts": {validation["classification"]: 1},
            "products": [record],
            "media": media,
        }
        result = {
            "mode": "dry-run",
            "database_mutated": False,
            "storage_mutated": False,
            "classification": validation["classification"],
            "blockers": validation["blockers"],
            "owner_decisions": validation["owner_decisions"],
            "intended_deltas": self._intended_deltas(record),
            "plan": {
                "product": {
                    key: record[key]
                    for key in (
                        "source_product_id",
                        "title",
                        "slug",
                        "legacy_slugs",
                        "publication_state",
                        "digital_authority",
                        "seo_title",
                        "meta_description",
                    )
                },
                "release": record.get("release"),
                "versions": record.get("versions") or [],
                "category_links": selected_links,
            },
            "media": media,
        }
        if bundle_dir is not None:
            bundle_dir.mkdir(parents=True, exist_ok=True)
            media_dir = bundle_dir / "media"
            media_dir.mkdir(exist_ok=True)
            for item in media:
                if not item.get("sha256") or item.get("error"):
                    continue
                with default_storage.open(item["storage_key"], "rb") as source:
                    content = source.read()
                if _sha256(content) != item["sha256"]:
                    raise CommandError(
                        f"Media changed during bundle preparation: {item['storage_key']}"
                    )
                target = media_dir / item["bundle_name"]
                target.write_bytes(content)
            (bundle_dir / MANIFEST_NAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            result["bundle"] = {
                "path": str(bundle_dir),
                "manifest_sha256": _sha256(
                    (bundle_dir / MANIFEST_NAME).read_bytes()
                ),
                "files": len([item for item in media if item.get("sha256")]),
            }
        return result

    @staticmethod
    def _intended_deltas(record):
        versions = record.get("versions") or []
        offers = [offer for version in versions for offer in version.get("offers") or []]
        pools = {offer["inventory"]["source_pool_id"] for offer in offers}
        return {
            "products": 1,
            "versions": len(versions),
            "offers": len(offers),
            "inventory_pools": len(pools),
            "slug_history": len(record.get("legacy_slugs") or []),
        }

    def _apply(self, bundle_dir, *, manifest_sha256, site_url, api_url):
        manifest_path = bundle_dir / MANIFEST_NAME
        try:
            actual_manifest_sha256 = _sha256(manifest_path.read_bytes())
        except OSError as exc:
            raise CommandError("Promotion bundle manifest is missing.") from exc
        if actual_manifest_sha256 != manifest_sha256:
            raise CommandError("Promotion bundle manifest checksum mismatch.")
        payload = _load_json(manifest_path)
        if payload.get("workflow_schema") != "cheatsg.digital-game-expansion.v1":
            raise CommandError("Unsupported Digital Game promotion workflow schema.")
        records = payload.get("products") or []
        if len(records) != 1 or records[0].get("classification") != PRODUCTION_READY:
            raise CommandError("Apply requires exactly one PRODUCTION_READY Digital Game.")
        if records[0].get("validation", {}).get("classification") != PRODUCTION_READY:
            raise CommandError("Embedded source validation is not PRODUCTION_READY.")
        self._apply_media(bundle_dir, payload.get("media") or [])
        before = _catalog_counts()
        import_output = StringIO()
        call_command(
            "import_production_catalog",
            str(manifest_path),
            apply=True,
            stdout=import_output,
        )
        after = _catalog_counts()
        result = {
            "mode": "apply",
            "classification": PRODUCTION_READY,
            "slug": records[0]["slug"],
            "counts_before": before,
            "counts_after": after,
            "count_delta": {key: after[key] - before[key] for key in before},
            "import_result": import_output.getvalue().strip(),
            "media": {"verified": len(payload.get("media") or [])},
        }
        if site_url or api_url:
            if not site_url or not api_url:
                raise CommandError("Live verification requires both --site-url and --api-url.")
            result["live_verification"] = self._verify_live(
                records[0], site_url=site_url, api_url=api_url
            )
        return result

    @staticmethod
    def _apply_media(bundle_dir, media):
        media_dir = bundle_dir / "media"
        seen_keys = set()
        for item in media:
            key = str(item.get("storage_key") or "")
            bundle_name = str(item.get("bundle_name") or "")
            expected = str(item.get("sha256") or "")
            path = PurePosixPath(key)
            if (
                not key
                or path.is_absolute()
                or ".." in path.parts
                or key in seen_keys
                or not expected
                or Path(bundle_name).name != bundle_name
            ):
                raise CommandError("Promotion media descriptor is unsafe or incomplete.")
            seen_keys.add(key)
            source_path = media_dir / bundle_name
            try:
                payload = source_path.read_bytes()
            except OSError as exc:
                raise CommandError(f"Promotion media is missing for {key}.") from exc
            if _sha256(payload) != expected or len(payload) != int(item["byte_size"]):
                raise CommandError(f"Promotion media checksum mismatch for {key}.")
            if default_storage.exists(key):
                with default_storage.open(key, "rb") as existing:
                    current = existing.read()
                if _sha256(current) != expected:
                    raise CommandError(f"Existing Production media conflicts at {key}.")
                continue
            saved_name = default_storage.save(key, ContentFile(payload))
            if saved_name != key:
                raise CommandError(f"Storage changed the deterministic key for {key}.")

    @staticmethod
    def _fetch(url):
        try:
            request = Request(url, headers={"User-Agent": "CheatsGameCatalogVerifier/1.0"})
            with urlopen(request, timeout=20) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")
        except (OSError, URLError) as exc:
            raise CommandError(f"Live verification could not reach {url}.") from exc

    def _verify_live(self, record, *, site_url, api_url):
        slug = record["slug"]
        encoded_slug = quote(slug, safe="")
        detail_url = urljoin(site_url.rstrip("/") + "/", f"DigitalGame/{encoded_slug}/")
        api_detail_url = urljoin(
            api_url.rstrip("/") + "/",
            f"api/digital-products/catalog/games/{encoded_slug}/",
        )
        sitemap_url = urljoin(site_url.rstrip("/") + "/", "sitemap.xml")
        detail_status, detail_html = self._fetch(detail_url)
        api_status, api_payload = self._fetch(api_detail_url)
        sitemap_status, sitemap = self._fetch(sitemap_url)
        decoded_sitemap = unquote(sitemap)
        canonical = f"{site_url.rstrip('/')}/DigitalGame/{slug}/"
        legacy = record.get("legacy_slugs") or []
        checks = {
            "storefront_detail_200": detail_status == 200,
            "backend_detail_200": api_status == 200,
            "title_in_ssr": record["title"] in detail_html,
            "canonical_in_ssr": canonical in detail_html,
            "structured_data_present": "application/ld+json" in detail_html,
            "sitemap_200": sitemap_status == 200,
            "canonical_in_sitemap": canonical in sitemap,
            "legacy_absent_from_sitemap": all(
                alias not in decoded_sitemap for alias in legacy
            ),
            "backend_projection_matches_slug": slug in api_payload,
        }
        if not all(checks.values()):
            raise CommandError(
                "Live per-game verification failed: "
                + ", ".join(key for key, passed in checks.items() if not passed)
            )
        return checks
