from django.urls import path, register_converter
from django.urls.converters import SlugConverter

from cheatgame.general.apis import StoryAdminApi, StoryDetailApi, StoryListApi, SliderAdminApi, SliderListApi, \
    SliderDetailApi, BannerAdminApi, BannerApi, BannerListApi, BlogAdminApi, BlogAiDraftAdminApi, BlogDetailApi, BlogListApi, \
    BlogDetailUserApi, BlogCategoryAdminApi, BlogCategoryDetailApi, UploadFileS3ApiView, HomePageReportApi, \
    CreateMessageAdminApi, MessageDetailAdminApi, CreateUserMessageList, MessageListUserOutPutSerializer, \
    UserMessageListApi, UserMessageSeenApi, MessageListApi, CommonQuestionAdminApi, CommonQuestionDetialAdminApi, \
    CommonQuestionListApi, BlogCommentCreateApi, BlogCommentDetailApi


class CustomSlugConverter(SlugConverter):
    regex = '[-\w]+'


register_converter(CustomSlugConverter, 'custom_slug')
urlpatterns = [
    path("create-story/", StoryAdminApi.as_view(), name="create-story-admin"),
    path("story/<int:id>/", StoryDetailApi.as_view(), name="story-detail"),
    path("story-list/", StoryListApi.as_view(), name="story-list"),
    path("create-slider/", SliderAdminApi.as_view(), name="create-slider-admin"),
    path("slider-list/", SliderListApi.as_view(), name="slider-list"),
    path("slider-detail/<int:id>/", SliderDetailApi.as_view(), name="slider-detail"),
    path("create-banner/", BannerAdminApi.as_view(), name="banner-create-admin"),
    path("banner/<int:id>/", BannerApi.as_view(), name="banner-change"),
    path("banner-list/", BannerListApi.as_view(), name="banner-list"),
    path("create-blog/", BlogAdminApi.as_view(), name="create-blog-admin"),
    path("admin/blog-ai-draft/", BlogAiDraftAdminApi.as_view(), name="blog-ai-draft-admin"),
    path("blog-detail/<int:id>/", BlogDetailApi.as_view(), name="blog-detail-admin"),
    path("blog-list/", BlogListApi.as_view(), name="blog-list"),
    path("blog-detail/<custom_slug:slug>/", BlogDetailUserApi.as_view(), name="blog-detail"),
    path("blog-category/", BlogCategoryAdminApi.as_view(), name="blog-category-admin"),
    path("blog-category/<int:id>/", BlogCategoryDetailApi.as_view(), name="blog-category-admin"),
    path("leave-comment-blog/" , BlogCommentCreateApi.as_view() , name="leave-comment"),
    path("blog-comment-detail/<int:id>/" , BlogCommentDetailApi.as_view(), name="blog-comment-detail"),
    path("upload-file/", UploadFileS3ApiView.as_view(), name="upload-file-admin"),
    path("home-page-report/", HomePageReportApi.as_view(), name="home-page-report"),
    path("create-message/" , CreateMessageAdminApi.as_view() , name= "create-message-admin"),
    path("message-detail/<int:id>/" , MessageDetailAdminApi.as_view() , name ="message-detail-api"),
    path("message-list-admin/" , MessageListApi.as_view() , name= "message-list-admin"),
    path("create-user-message/" , CreateUserMessageList.as_view() , name="create-user-message"),
    path("message-list-user/" , UserMessageListApi.as_view() ,name= "user message list"),
    path("seen-message-report/<int:id>/" , UserMessageSeenApi.as_view() , name="seen message report"),
    path("create-common-question/" , CommonQuestionAdminApi.as_view() , name="create-common-question"),
    path("common-question-detail/<int:id>/" , CommonQuestionDetialAdminApi.as_view() , name= "common-question-detail"),
    path("common-question-list/" , CommonQuestionListApi.as_view() , name= "common-quesiton-list"),



]
