import json

from django.core.management.base import BaseCommand

from cheatgame.general.models import Banner, Blog, CommonQuestion, Slider, Story


def _file_key(value):
    return value.name if value else ""


class Command(BaseCommand):
    help = "Emit a read-only manifest containing only public CMS content."

    def add_arguments(self, parser):
        parser.add_argument("--pretty", action="store_true")

    def handle(self, *args, **options):
        del args
        payload = {
            "schema": "cheatsg.public-content-promotion.v1",
            "policy": (
                "Contains public CMS configuration only; contact forms, comments, "
                "customer messages and identities are intentionally excluded."
            ),
            "stories": [
                {
                    "source_id": item.pk,
                    "picture": _file_key(item.picture),
                    "content_picture": _file_key(item.content_picture),
                    "link": item.link,
                    "title": item.title,
                    "is_active": item.is_active,
                    "sort_order": item.sort_order,
                    "alt_text": item.alt_text,
                }
                for item in Story.objects.order_by("pk")
            ],
            "sliders": [
                {
                    "source_id": item.pk,
                    "laptop_picture": _file_key(item.laptop_picture),
                    "middle_picture": _file_key(item.middle_picture),
                    "mobile_picture": _file_key(item.mobile_picture),
                    "link": item.link,
                    "is_active": item.is_active,
                    "sort_order": item.sort_order,
                    "alt_text": item.alt_text,
                    "hero_eyebrow": item.hero_eyebrow,
                    "hero_headline": item.hero_headline,
                    "hero_highlight": item.hero_highlight,
                    "hero_subtitle": item.hero_subtitle,
                    "hero_primary_label": item.hero_primary_label,
                    "hero_primary_link": item.hero_primary_link,
                    "hero_secondary_label": item.hero_secondary_label,
                    "hero_secondary_link": item.hero_secondary_link,
                    "hero_artwork_image": _file_key(item.hero_artwork_image),
                }
                for item in Slider.objects.order_by("pk")
            ],
            "banners": [
                {
                    "source_id": item.pk,
                    "picture": _file_key(item.picture),
                    "link": item.link,
                    "location": item.location,
                    "is_active": item.is_active,
                    "sort_order": item.sort_order,
                    "alt_text": item.alt_text,
                }
                for item in Banner.objects.order_by("pk")
            ],
            "blogs": [
                {
                    "source_id": item.pk,
                    "title": item.title,
                    "slug": item.slug,
                    "content": _file_key(item.content),
                    "picture": _file_key(item.picture),
                    "status": item.status,
                    "seo_title": item.seo_title,
                    "meta_description": item.meta_description,
                }
                for item in Blog.objects.order_by("pk")
            ],
            "common_questions": [
                {
                    "source_id": item.pk,
                    "question_location": item.question_location,
                    "question": item.question,
                    "answer": item.answer,
                }
                for item in CommonQuestion.objects.order_by("pk")
            ],
        }
        self.stdout.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2 if options["pretty"] else None,
                sort_keys=True,
            )
        )
