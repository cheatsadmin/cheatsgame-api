from django.http import HttpResponse


CRAWLER_EXCLUSION = "noindex, nofollow"


class CrawlerExclusionMiddleware:
    """Keep the Backend/API host out of search indexes without changing API data."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["X-Robots-Tag"] = CRAWLER_EXCLUSION
        return response


def robots_txt(request):
    return HttpResponse(
        "User-agent: *\nDisallow: /\n",
        content_type="text/plain; charset=utf-8",
    )
