from enum import IntEnum

from django.utils.text import slugify

from cheatgame.common.models import BaseModel
from django.db import models

from mptt.models import TreeForeignKey, MPTTModel


class ProductType(IntEnum):
    PACKAGE = 1
    GAME = 2
    PHYSCIAL = 3
    GIFTCART = 4

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class ProductStatus(models.TextChoices):
    DRAFT = "draft", "DRAFT"
    PUBLISHED = "published", "PUBLISHED"
    HIDDEN = "hidden", "HIDDEN"


class ProductOrderBy(IntEnum):
    EXPENSIVE = 1
    INEXPENSIVE = 2
    NEWEST = 3
    FAVOURITE = 4

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class FeatureType(IntEnum):
    BOOL = 1
    STRING = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class CategoryType(IntEnum):
    PRODUCT = 1
    FEATURE = 2
    BRAND = 3
    BLOG = 4
    SERVICE = 5
    GAME = 7
    GIFTCART = 8

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class AttachmentType(IntEnum):
    GUARANTEE = 1
    INSURANCE = 2
    CAPACITY = 3

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class DeliveryOption(IntEnum):
    INPERSON = 1
    POST = 2
    MOTOR = 3
    USERHOME = 4

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class LabelType(IntEnum):
    BRAND = 1
    GENERAL = 2
    CONSOLE = 3
    CAPACITY = 4

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class RatingChoices(IntEnum):
    VERYBAD = 1
    BAD = 2
    SOSO = 3
    GOOD = 4
    VERYGOOD = 5

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class ReviewStatus(models.TextChoices):
    PENDING = "pending", "PENDING"
    APPROVED = "approved", "APPROVED"
    REJECTED = "rejected", "REJECTED"


class DirectionType(IntEnum):
    SEND = 1
    RECIEVE = 2

    @classmethod
    def choices(cls):
        return [(key.value, key.name) for key in cls]


class Product(BaseModel):
    product_type = models.IntegerField(
        choices=ProductType.choices(),
        default=ProductType.PHYSCIAL,
    )
    title = models.CharField(
        max_length=100
    )
    slug = models.SlugField(db_index=True, unique=True, allow_unicode=True, max_length=120, blank=True)
    status = models.CharField(
        max_length=20,
        choices=ProductStatus.choices,
        default=ProductStatus.PUBLISHED,
        db_index=True,
    )
    seo_title = models.CharField(max_length=120, blank=True, default="")
    meta_description = models.CharField(max_length=300, blank=True, default="")
    main_image = models.FileField(
        upload_to="product/main_images/"
    )
    price = models.DecimalField(
        max_digits=15,
        decimal_places=0,
    )
    off_price = models.DecimalField(
        max_digits=15,
        decimal_places=0,
    )
    quantity = models.IntegerField(default=1)
    discount_end_time = models.DateTimeField(
        null=True, blank=True
    )
    description = models.FileField()
    included_products = models.ManyToManyField(
        'self',
        symmetrical=False,
        blank=True,
        related_name='included_in_packages'
    )
    order_limit = models.IntegerField(
        null=True,
        blank=True,
    )
    device_model = models.CharField(max_length=100, null=True, blank=True)
    score = models.DecimalField(max_digits=4 , decimal_places=2, default=4.8)

    def __str__(self):
        return self.title

    def generate_unique_slug(self) -> str:
        base_slug = slugify(self.title, allow_unicode=True) or "product"
        base_slug = base_slug[:110]
        slug = base_slug
        counter = 2
        queryset = self.__class__.objects.all()
        if self.pk:
            queryset = queryset.exclude(pk=self.pk)

        while queryset.filter(slug=slug).exists():
            suffix = f"-{counter}"
            slug = f"{base_slug[:120 - len(suffix)]}{suffix}"
            counter += 1
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self.generate_unique_slug()
        super(Product, self).save(*args, **kwargs)


class GiftCartData(BaseModel):
    product = models.ForeignKey("product.Product", on_delete=models.SET_NULL , null=True , blank=True)
    code = models.CharField(max_length=20)
    order = models.OneToOneField("shop.Order" , on_delete=models.CASCADE , null=True , blank=True)

    def __str__(self):
        return f"{self.product.title}-code"


class Image(BaseModel):
    product = models.ForeignKey('Product',
                                on_delete=models.CASCADE,
                                related_name='images'
                                )
    file = models.FileField(upload_to='product_images/',
                            )


class Question(BaseModel):
    product = models.ForeignKey('Product',
                                on_delete=models.CASCADE,
                                related_name='questions'
                                )
    question = models.CharField(max_length=300)
    sender = models.ForeignKey(
        'users.BaseUser',
        on_delete=models.SET_NULL,
        related_name='asked_questions',
        null=True,
        blank=True,
    )
    answer = models.CharField(max_length=300, null=True, blank=True)
    answered = models.ForeignKey(
        "users.BaseUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='answered_questions',
    )
    accepted = models.BooleanField(default=False)

    def __str__(self):
        return self.product.title


class Category(MPTTModel):
    category_type = models.IntegerField(
        choices=CategoryType.choices(),
        default=CategoryType.PRODUCT,
        db_index=True,
    )
    name = models.CharField(
        max_length=50,
    )
    slug = models.SlugField(
        unique=True,
        db_index=True,
    )
    parent = TreeForeignKey('self',
                            on_delete=models.CASCADE,
                            null=True,
                            blank=True,
                            related_name='children',
                            )
    order_insertion_by = ['name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name, allow_unicode=True)
        super(Category, self).save(*args, **kwargs)


class ProductCategory(BaseModel):
    product = models.ForeignKey(
        'Product',
        on_delete=models.CASCADE,
        related_name='categories'
    )
    category = models.ForeignKey(
        'Category',
        on_delete=models.CASCADE,
    )

    class Meta:
        unique_together = ("product", "category")


class Feature(BaseModel):
    name = models.CharField(max_length=100)
    feature_type = models.IntegerField(
        choices=FeatureType.choices(),
        default=FeatureType.STRING
    )
    category = models.ForeignKey(
        'Category',
        related_name='features',
        on_delete=models.CASCADE,
    )

    def __str__(self):
        return self.name


class ValuesList(BaseModel):
    value = models.CharField(
        max_length=100,
    )
    product = models.ForeignKey(
        'Product',
        on_delete=models.CASCADE,
        related_name='valueslist'
    )
    feature = models.ForeignKey(
        'Feature',
        on_delete=models.CASCADE,
    )

    def __str__(self):
        return self.feature.name


class Attachment(BaseModel):
    attachment_type = models.IntegerField(
        choices=AttachmentType.choices(),
    )
    title = models.CharField(
        max_length=200,
    )
    price = models.DecimalField(
        max_digits=15,
        decimal_places=0,
    )

    is_force_attachment = models.BooleanField(
        default=False,
    )
    product = models.ForeignKey(
        'Product',
        on_delete=models.CASCADE,
        related_name='attachments'
    )

    description = models.CharField(max_length=250 , null=True , blank=True)

    def __str__(self):
        return self.title


class Label(BaseModel):
    label_type = models.IntegerField(choices=LabelType.choices(),
                                     default=LabelType.GENERAL)
    name = models.CharField(
        max_length=100,
        db_index=True,
        unique=True
    )

    def __str__(self):
        return self.name


class ProductLabel(BaseModel):
    label = models.ForeignKey('Label',
                              on_delete=models.CASCADE)
    product = models.ForeignKey("Product", on_delete=models.CASCADE,
                                related_name='labels')


class Reviews(BaseModel):
    user = models.ForeignKey(
        'users.BaseUser',
        on_delete=models.CASCADE,
    )

    product = models.ForeignKey(
        'Product',
        on_delete=models.CASCADE,
        related_name='reviews'
    )
    comment = models.CharField(
        null=True,
        blank=True,
        max_length=500,
    )
    rating = models.IntegerField(
        choices=RatingChoices.choices()
    )
    status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
        db_index=True,
    )
    accepted = models.BooleanField(default=False)

    class Meta:
        unique_together = ("product" , "user")

    def save(self, *args, **kwargs):
        self.accepted = self.status == ReviewStatus.APPROVED
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"accepted"}
        super().save(*args, **kwargs)

    def __str__(self):
        return self.user.lastname



class SuggestionProduct(models.Model):
    product = models.ForeignKey('Product', on_delete=models.CASCADE, related_name="suggestions")
    suggested = models.ForeignKey('Product', on_delete=models.CASCADE, )


class ProductNote(models.Model):
    title = models.CharField(max_length=100)
    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name='notes')

    def __str__(self):
        return self.title
