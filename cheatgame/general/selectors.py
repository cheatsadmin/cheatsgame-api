from django.db.models import QuerySet

from cheatgame.general.filters import BlogFilter
from cheatgame.general.models import Story, Slider, Banner, Blog, BlogStatus, Message, UserMessage, CommonQuestion, Comment
from cheatgame.users.models import BaseUser


def get_stories(*, include_inactive: bool = False) -> QuerySet[Story]:
    qs = Story.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("sort_order", "-id")


def get_sliders(*, include_inactive: bool = False) -> QuerySet[Slider]:
    qs = Slider.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("sort_order", "-id")


def get_banners(*, include_inactive: bool = False) -> QuerySet[Banner]:
    qs = Banner.objects.all()
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return qs.order_by("sort_order", "-id")


def blog_list(*, filters=None, include_drafts: bool = False) -> QuerySet[Blog]:
    filters = filters or {}
    qs = Blog.objects.all()
    if not include_drafts:
        qs = qs.filter(status=BlogStatus.PUBLISHED)
    if include_drafts:
        qs = qs.order_by("-created_at", "-id")
    return BlogFilter(filters, qs).qs


def get_blog(slug: str, *, include_drafts: bool = False) -> Blog:
    qs = Blog.objects.prefetch_related('categories')
    if not include_drafts:
        qs = qs.filter(status=BlogStatus.PUBLISHED)
    return qs.get(slug=slug)

def get_message_list() -> QuerySet[Message]:
    return Message.objects.all()

def get_user_message_list(* , user:BaseUser) ->QuerySet[UserMessage]:
    return UserMessage.objects.filter(user = user)

def get_common_question_list() -> QuerySet[CommonQuestion]:
    return CommonQuestion.objects.all()


def get_comment_list_blog(* , blog:Blog) -> QuerySet[Comment]:
    return Comment.objects.filter(blog=blog)

