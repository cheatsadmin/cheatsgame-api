from django.contrib import admin

from cheatgame.general.models import BlogCategory, Blog, Story, Slider, Banner, Message, UserMessage


class BlogCategoryInLine(admin.TabularInline):
    model = BlogCategory


# class SuggestionProductInLine(admin.TabularInline):
#     model = SuggestionProduct


class BlogAdmin(admin.ModelAdmin):
    fields = ("title", "slug", "status", "seo_title", "meta_description", "content", "picture")
    search_fields = ("title", "slug", "seo_title")
    list_filter = ("status",)
    list_display = (
        "title", "slug", "status", "updated_at", "content", "picture"
    )
    inlines = (
        BlogCategoryInLine,
    )


admin.site.register(Blog, BlogAdmin)


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    fields = ("title", "link", "picture", "content_picture",)
    search_fields = ("title",)
    list_display = (
        "title", "link", "picture", "content_picture"
    )


@admin.register(Slider)
class SliderAdmin(admin.ModelAdmin):
    fields = ("laptop_picture", "link", "middle_picture", "mobile_picture")

    list_display = (
        "laptop_picture", "link", "middle_picture", "mobile_picture",
    )


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    fields = ("link", "location", "picture")
    list_display = (
        "link", "location", "picture"
    )

@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    fields = ("title"  , "passage")
    list_display = ("title" , "passage")
    
@admin.register(UserMessage)
class UserMessageAdmin(admin.ModelAdmin):
    fields = ("message"  , "user" , "is_seen")
    list_display = ("message" , "user" , "is_seen")
