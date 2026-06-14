from django.contrib import admin
from cheatgame.product.models import Product, Image, Question, Category, Feature, Attachment, Label, \
    SuggestionProduct, ProductCategory, ValuesList, ProductLabel, ProductNote , Reviews


class ImageInLine(admin.TabularInline):
    model = Image


class ProductCategoryInLine(admin.TabularInline):
    model = ProductCategory


class ValuesListInLine(admin.TabularInline):
    model = ValuesList


class AttachmentInLine(admin.TabularInline):
    model = Attachment


class ProductLabelInLine(admin.TabularInline):
    model = ProductLabel


class NoteInLine(admin.TabularInline):
    model = ProductNote


# class SuggestionProductInLine(admin.TabularInline):
#     model = SuggestionProduct


class ProductAdmin(admin.ModelAdmin):
    fields = ("product_type", "title", "slug", "main_image", "price", "off_price", "discount_end_time", "order_limit",
              "description", "included_products", "quantity")
    search_fields = ("title", "slug")
    readonly_fields = ("slug", )
    list_display = (
        "title", "main_image", "product_type", "price", "off_price"
    )
    list_filter = ("price", "product_type")
    inlines = (
        ImageInLine, ProductCategoryInLine, ValuesListInLine,
        AttachmentInLine, ProductLabelInLine,
        NoteInLine
    )


admin.site.register(Product, ProductAdmin)


@admin.register(Image)
class ImageAdmin(admin.ModelAdmin):
    fields = ("product", "file",)
    search_fields = ("product",)
    list_display = (
        "product",
    )
    list_filter = ("product",)


@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    fields = ("product", "question", "sender", "answer", "answered", "accepted")
    search_fields = ("accepted", "product" "sender", "answered")
    readonly_fields = ("question",)
    list_display = (
        "product", "question", "sender", "answer", "answered", "accepted"
    )
    list_filter = ("product", "sender", "answered", "accepted")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    fields = ("category_type", "name", "slug", "parent")
    search_fields = ("name", "slug")
    readonly_fields = ("slug", "parent")
    list_display = (
        "category_type", "name", "slug", "parent"
    )
    list_filter = ("parent", "category_type")


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    fields = ("name", "feature_type", "category")
    search_fields = ("name",)
    list_display = (
        "name", "feature_type", "category"
    )
    list_filter = ("feature_type", "category")


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    fields = ("attachment_type", "title", "price", "is_force_attachment", "product" , "description")
    search_fields = ("title",)
    readonly_fields = ("price",)
    list_display = (
        "attachment_type", "title", "price", "is_force_attachment", "product"
    )
    list_filter = ("attachment_type", "product")


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    fields = ("label_type", "name",)
    search_fields = ("name",)

    list_display = (
        "name", "label_type",
    )
    list_filter = ("label_type",)





@admin.register(SuggestionProduct)
class SuggestionProductAdmin(admin.ModelAdmin):
    fields = ("product", "suggested")
    list_display = (
        "product",
        "suggested"
    )
    list_filter = ("product", "suggested")

@admin.register(Reviews)
class ReviewAdmin(admin.ModelAdmin):
    fields = ("user" , "product" , "comment" , "rating" , "status" , "accepted")
    list_display = ("user" , "product" , "comment" , "rating" , "status" , "accepted")
    list_filter = ("status" , "accepted")
    readonly_fields = ("accepted" ,)
