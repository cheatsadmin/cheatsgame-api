from django.http import JsonResponse
from django.test import RequestFactory, SimpleTestCase
from django.urls import resolve

from cheatgame.core.crawler import CrawlerExclusionMiddleware, robots_txt


class CrawlerExclusionTests(SimpleTestCase):
    def test_backend_robots_blocks_the_entire_host(self):
        match = resolve("/robots.txt")
        self.assertIs(match.func, robots_txt)

        response = CrawlerExclusionMiddleware(robots_txt)(
            RequestFactory().get("/robots.txt")
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(response.content, b"User-agent: *\nDisallow: /\n")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_backend_health_responses_are_not_indexable(self):
        response = CrawlerExclusionMiddleware(
            lambda request: JsonResponse({"status": "ok"})
        )(RequestFactory().get("/health/live/"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
