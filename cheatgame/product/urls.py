from django.urls import path, register_converter
from django.urls.converters import SlugConverter

from cheatgame.product.apis.attachment import AttachmentAdminApi, AttachmentDetailApi, AttachmentListProductApi
from cheatgame.product.apis.category import CategoryAdminApi, ProductCategoryAdminApi, CategoryListApi, \
    CategoryDetailApi, ProductCategoryDetailApi, CategoryListAdminApi
from cheatgame.product.apis.feature import FeatureAdminApi, ProductFeatureAdminApi, FeatureDetailAdminApi, \
    ProductFeatureDetailApi, FeatureListAdminApi
from cheatgame.product.apis.image import ImageAdminApi, ImageDetailAdminApi
from cheatgame.product.apis.label import LabelAdminApi, ProductLabelAdminApi, LabelDetailAdminApi, \
    ProductLabelDetailAdminApi, LabelListApi, CosoleLabelListApi, CapacityLabelListApi, LabelListAdminApi
from cheatgame.product.apis.product import ProductAdminApi, ProudctApi, ProductNoteAdminApi, \
    ProductNoteDetailApi, ProductDetailApi, ProductDetailAdminApi
from cheatgame.product.apis.question import QuestionApi, QuestionDetailAdminApi, QuestionListAPIView
from cheatgame.product.apis.rating import ReviewListAPIView
from cheatgame.product.apis.reviews import ReviewsCreateAPIView, ReviewDetailAdminAPIView
from cheatgame.product.apis.suggestion import SuggestionProductAdminApi, SuggestionProductDetailApi
from cheatgame.shop.apis.cart import IsBoughtProductAPIView


class CustomSlugConverter(SlugConverter):
    regex = '[-\w]+'


register_converter(CustomSlugConverter, 'custom_slug')
urlpatterns = [

    path("product/", ProductAdminApi.as_view(), name="product-admin"),
    path("product-deatil/<int:id>/" , ProductDetailAdminApi.as_view() , name="product-detail-admin-api"),
    path("product-detail/<custom_slug:slug>/", ProductDetailApi.as_view(), name="product-detail"),
    path("get-product/", ProudctApi.as_view(), name="product-customer"),
    path("image/", ImageAdminApi.as_view(), name="image-admin"),
    path("image-detail/<int:id>/", ImageDetailAdminApi.as_view(), name="image-detail-admin"),
    path("question/", QuestionApi.as_view(), name="question-admin"),
    path("question-detail/<int:id>/", QuestionDetailAdminApi.as_view(), name="question-admin"),
    path("category/<int:id>/", CategoryDetailApi.as_view(), name="category-detail-admin"),
    path("category/", CategoryAdminApi.as_view(), name="category-create-admin"),
    path("category-list/<int:category_type>/", CategoryListApi.as_view(), name="category-list"),
    path("category-list-admin/" , CategoryListAdminApi.as_view() , name="category-list-admin"),
    path("product-category/", ProductCategoryAdminApi.as_view(), name="product-category-create-admin"),
    path("product-category/<int:id>/", ProductCategoryDetailApi.as_view(), name="product-category-admin"),
    path("feature-create/", FeatureAdminApi.as_view(), name="feature-create-admin"),
    path("feature-detail/<int:id>/", FeatureDetailAdminApi.as_view(), name="feature-detail-admin"),
    path("feature-list-admin/" , FeatureListAdminApi.as_view() , name="feature-list-admin"),
    path("product-feature/", ProductFeatureAdminApi.as_view(), name="product-feature-admin"),
    path("product-feature-detail/<int:id>/", ProductFeatureDetailApi.as_view(), name="product-feature-detail-admin"),
    path("attachment-create/", AttachmentAdminApi.as_view(), name="attachment-create-admin"),
    path("attachment/<int:id>/", AttachmentDetailApi.as_view(), name="attachment-admin"),
    path("attachment-list/<int:product_id>/", AttachmentListProductApi.as_view(), name="attachment-list-product"),
    path("label/", LabelAdminApi.as_view(), name="label-admin"),
    path("label-list-admin/" , LabelListAdminApi.as_view() , name="label-list-admin"),
    path("label-detail/<int:id>/", LabelDetailAdminApi.as_view(), name="label-detail-admin"),
    path("product-label/", ProductLabelAdminApi.as_view(), name="product-label-admin"),
    path("brand-list/", LabelListApi.as_view(), name="brands-list"),
    path("console-label-list/", CosoleLabelListApi.as_view(), name="console-label-list"),
    path("capacity-label-list/", CapacityLabelListApi.as_view(), name="capacity-label-list"),
    path("product-label-detail/<int:id>/", ProductLabelDetailAdminApi.as_view(), name="product-label-detail-admin"),
    path("suggestion-product/", SuggestionProductAdminApi.as_view(), name="suggestion-product-admin"),
    path("suggestion-product-detail/<int:id>/", SuggestionProductDetailApi.as_view(),
         name="suggestion-product-detail-admin"),
    path("product-note/", ProductNoteAdminApi.as_view(), name="product-note-create"),
    path("product-note-detail/<int:id>/", ProductNoteDetailApi.as_view(), name="product-note-detail"),
    path("is-bought-product/" , IsBoughtProductAPIView.as_view() ,name="is-bought-product"),
    path("product-review/" ,ReviewsCreateAPIView.as_view() , name="product-review-create"),
    path("review-detail-admin/<int:id>/" , ReviewDetailAdminAPIView.as_view() , name="review-detail-admin"),
    path("question-list-admin/" , QuestionListAPIView.as_view() , name="question-list-admin"),
    path("review-list-admin/" , ReviewListAPIView.as_view() , name="review-list-admin"),




]
