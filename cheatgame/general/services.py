import decimal

from django.db.models import QuerySet
from django.utils.text import slugify
from cheatgame.general.models import Story, Slider, Banner, Blog, BlogCategory, BlogStatus, Message, UserMessage, CommonQuestion, \
    Comment
from cheatgame.issue.models import Issue
from cheatgame.product.models import Category
from cheatgame.users.models import BaseUser


def create_story(
    *,
    title: str,
    link: str,
    content_picture: str,
    picture: str,
    is_active: bool = True,
    sort_order: int = 0,
    alt_text: str = "",
) -> Story:
    return Story.objects.create(
        title=title,
        link=link,
        content_picture=content_picture,
        picture=picture,
        is_active=is_active,
        sort_order=sort_order,
        alt_text=alt_text or "",
    )


def update_story(
    *,
    story_id: int,
    title: str,
    link: str,
    content_picture: str = None,
    picture: str = None,
    is_active: bool = True,
    sort_order: int = 0,
    alt_text: str = "",
) -> Story:
    story = Story.objects.get(id=story_id)
    story.title = title
    if content_picture is not None:
        story.content_picture = content_picture
    if picture is not None:
        story.picture = picture
    story.link = link
    story.is_active = is_active
    story.sort_order = sort_order
    story.alt_text = alt_text or ""
    story.save()
    return story


def delete_story(*, story_id: int) -> None:
    Story.objects.get(id=story_id).delete()


def create_slider(
    *,
    link: str,
    laptop_picture: str,
    middle_picture: str,
    mobile_picture: str,
    is_active: bool = True,
    sort_order: int = 0,
    alt_text: str = "",
    hero_eyebrow: str = None,
    hero_headline: str = None,
    hero_highlight: str = None,
    hero_subtitle: str = None,
    hero_primary_label: str = None,
    hero_primary_link: str = None,
    hero_secondary_label: str = None,
    hero_secondary_link: str = None,
    hero_artwork_image: str = None,
) -> Slider:
    return Slider.objects.create(
        link=link,
        laptop_picture=laptop_picture,
        middle_picture=middle_picture,
        mobile_picture=mobile_picture,
        is_active=is_active,
        sort_order=sort_order,
        alt_text=alt_text or "",
        hero_eyebrow=hero_eyebrow,
        hero_headline=hero_headline,
        hero_highlight=hero_highlight,
        hero_subtitle=hero_subtitle,
        hero_primary_label=hero_primary_label,
        hero_primary_link=hero_primary_link,
        hero_secondary_label=hero_secondary_label,
        hero_secondary_link=hero_secondary_link,
        hero_artwork_image=hero_artwork_image,
    )


def update_slider(*, slider_id: int, link: str, laptop_picture: str=None, middle_picture: str=None,
                  mobile_picture: str = None, is_active: bool = True, sort_order: int = 0,
                  alt_text: str = "", hero_eyebrow: str = None, hero_headline: str = None,
                  hero_highlight: str = None, hero_subtitle: str = None, hero_primary_label: str = None,
                  hero_primary_link: str = None, hero_secondary_label: str = None,
                  hero_secondary_link: str = None, hero_artwork_image: str = None) -> Slider:
    slider = Slider.objects.get(id=slider_id)
    slider.link = link
    if laptop_picture is not None:
        slider.laptop_picture = laptop_picture
    if middle_picture is not None:
        slider.middle_picture = middle_picture
    if mobile_picture is not None:
        slider.mobile_picture = mobile_picture
    slider.is_active = is_active
    slider.sort_order = sort_order
    slider.alt_text = alt_text or ""
    slider.hero_eyebrow = hero_eyebrow
    slider.hero_headline = hero_headline
    slider.hero_highlight = hero_highlight
    slider.hero_subtitle = hero_subtitle
    slider.hero_primary_label = hero_primary_label
    slider.hero_primary_link = hero_primary_link
    slider.hero_secondary_label = hero_secondary_label
    slider.hero_secondary_link = hero_secondary_link
    if hero_artwork_image is not None:
        slider.hero_artwork_image = hero_artwork_image
    slider.save()
    return slider


def check_issue_exists(*  , issue_id: int) -> bool:
    return Issue.objects.filter(id=issue_id).exists()
def update_issue(
    *,
    issue_id: int,
    title: str,
    min_price: decimal,
    max_price: decimal,
    picture: str = None,
    description: str = None,
    is_active: bool = True,
    sort_order: int = 0,
) -> Issue:
    issue = Issue.objects.get(id=issue_id)
    issue.title = title
    if picture is not None:
        issue.picture = picture
    if description is not None:
        issue.description = description

    issue.min_price = min_price
    issue.max_price = max_price
    issue.is_active = is_active
    issue.sort_order = sort_order
    issue.save()
    return issue

def delete_issue(* , issue_id: int) -> None:
    Issue.objects.filter(id=issue_id).delete()

def delete_slider(*, slider_id: int) -> None:
    Slider.objects.get(id=slider_id).delete()


def check_banner_exists(*, location: int) -> bool:
    if Banner.objects.filter(location=location).exists():
        return True
    return False


def create_banner(
    *,
    picture: str,
    link: str,
    location: int,
    is_active: bool = True,
    sort_order: int = 0,
    alt_text: str = "",
) -> Banner:
    return Banner.objects.create(
        picture=picture,
        link=link,
        location=location,
        is_active=is_active,
        sort_order=sort_order,
        alt_text=alt_text or "",
    )


def update_banner(
    *,
    banner_id: int,
    picture: str = None,
    link: str,
    location: int,
    is_active: bool = True,
    sort_order: int = 0,
    alt_text: str = "",
) -> Banner:
    banner = Banner.objects.get(id=banner_id)
    if picture is not None:
        banner.picture = picture
    banner.location = location
    banner.link = link
    banner.is_active = is_active
    banner.sort_order = sort_order
    banner.alt_text = alt_text or ""
    banner.save()
    return banner


def delete_banner(*, banner_id: int) -> None:
    Banner.objects.get(id=banner_id).delete()


def build_unique_blog_slug(value: str, *, exclude_blog_id: int = None) -> str:
    base_slug = slugify(value, allow_unicode=True) or "blog"
    base_slug = base_slug[:280]
    slug = base_slug
    queryset = Blog.objects.all()
    if exclude_blog_id:
        queryset = queryset.exclude(id=exclude_blog_id)
    counter = 2
    while queryset.filter(slug=slug).exists():
        suffix = f"-{counter}"
        slug = f"{base_slug[:300 - len(suffix)]}{suffix}"
        counter += 1
    return slug


def create_blog(
    *,
    title: str,
    content: str,
    picture: str,
    slug: str = "",
    status: str = BlogStatus.DRAFT,
    seo_title: str = "",
    meta_description: str = "",
) -> Blog:
    return Blog.objects.create(
        title=title,
        slug=build_unique_blog_slug(slug or title),
        content=content,
        picture=picture,
        status=status or BlogStatus.DRAFT,
        seo_title=seo_title or "",
        meta_description=meta_description or "",
    )


def update_blog(
    *,
    blog_id: int,
    title: str,
    content: str = None,
    picture: str = None,
    slug: str = None,
    status: str = None,
    seo_title: str = None,
    meta_description: str = None,
) -> Blog:
    blog = Blog.objects.get(id=blog_id)
    blog.title = title
    if content is not None:
        blog.content = content
    if picture is not None:
        blog.picture = picture
    if slug is not None:
        blog.slug = slugify(slug, allow_unicode=True) or build_unique_blog_slug(
            title,
            exclude_blog_id=blog_id,
        )
    if status is not None:
        blog.status = status
    if seo_title is not None:
        blog.seo_title = seo_title
    if meta_description is not None:
        blog.meta_description = meta_description
    blog.save()
    return blog


def delete_blog(*, blog_id: int) -> None:
    Blog.objects.get(id=blog_id).delete()


def create_blog_category(*, category: Category, blog: Blog) -> BlogCategory:
    return BlogCategory.objects.create(category=category, blog=blog)


def update_blog_category(*, blog_category_id: int, blog: Blog, category: Category) -> BlogCategory:
    blog_category = BlogCategory.objects.get(id=blog_category_id)
    blog_category.blog = blog
    blog_category.category = category
    blog_category.save()
    return blog_category


def delete_blog_category(*, blog_category_id) -> None:
    BlogCategory.objects.get(id=blog_category_id).delete()


def create_message(*, title: str, passage: str) -> Message:
    return Message.objects.create(title=title, passage=passage)


def update_message(*, message_id: int, title: str, passage: str) -> Message:
    message = Message.objects.get(id=message_id)
    message.title = title
    message.passage = passage
    message.save()
    return message


def delete_message(*, message_id: int) -> None:
    Message.objects.get(id=message_id).delete()


def create_user_message(*, user_messages: list[UserMessage]) -> QuerySet[UserMessage]:
    return UserMessage.objects.bulk_create(user_messages)


def seen_user_message(*, user_message_id: int) -> UserMessage:
    user_message = UserMessage.objects.get(id=user_message_id)
    user_message.is_seen = True
    user_message.save()
    return user_message


def create_common_question(*, question_location: int, question: str, answer: str) -> CommonQuestion:
    return CommonQuestion.objects.create(
        question_location=question_location,
        question=question,
        answer=answer

    )


def update_common_question(*, id: int, question_location: int, question: str, answer) -> CommonQuestion:
    common_question = CommonQuestion.objects.get(id=id)
    common_question.question_location = question_location
    common_question.question = question
    common_question.answer = answer
    common_question.save()
    return common_question


def delete_common_question(*, id: int) -> None:
    CommonQuestion.objects.get(id=id).delete()

def create_blog_comment(* , blog:Blog , content:str , user:BaseUser) -> Comment:
    return Comment.objects.create(
        blog=blog,
        content=content,
        user=user
    )

def check_comment_exists(* , blog:Blog, user:BaseUser) ->bool:
    return Comment.objects.filter(blog=blog, user=user).exists()

def check_commnent_exists_id(* , comment_id: int) -> bool:
    return Comment.objects.filter(id = comment_id).exists()


def update_comment(* , comment_id:int , content:str) -> Comment:
    comment = Comment.objects.get(id=comment_id)
    comment.content = content
    comment.save()
    return comment

def delete_comment(* , comment_id:int) -> None:
    comment = Comment.objects.filter(id=comment_id).delete()
