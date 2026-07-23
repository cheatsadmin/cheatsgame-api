from rest_framework import serializers

from cheatgame.digital_products.models import DigitalOfferCapacity
from cheatgame.product.models import NativeConsole


class DigitalApiErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()
    fields = serializers.DictField(required=False)


class PublicDigitalGameFilterSerializer(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")
    console = serializers.ChoiceField(required=False, choices=NativeConsole.choices, default="")
    capacity = serializers.ChoiceField(required=False, choices=DigitalOfferCapacity.choices, default="")
    ordering = serializers.ChoiceField(
        required=False,
        choices=(("newest", "newest"), ("title", "title"), ("minimum_price", "minimum_price")),
        default="newest",
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50)
    offset = serializers.IntegerField(required=False, min_value=0)

    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {field: "This query parameter is not accepted." for field in unknown}
            )
        return attrs


class CodeLabelSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()


class PublicDigitalOfferSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    customer_console = serializers.ChoiceField(choices=NativeConsole.choices)
    customer_console_label = serializers.CharField()
    capacity = serializers.ChoiceField(choices=DigitalOfferCapacity.choices)
    capacity_label = serializers.CharField()
    delivered_version_label = serializers.CharField()
    native_console = serializers.ChoiceField(choices=NativeConsole.choices)
    native_console_label = serializers.CharField()
    compatibility_code = serializers.CharField()
    compatibility_disclosure = serializers.CharField()
    capacity_code = serializers.CharField()
    capacity_disclosure = serializers.CharField()
    price = serializers.DecimalField(max_digits=15, decimal_places=0)
    currency = serializers.CharField()
    availability = serializers.ChoiceField(choices=("AVAILABLE", "SOLD_OUT"))
    is_available = serializers.BooleanField()
    allowed_fulfillment_methods = CodeLabelSerializer(many=True)


class PublicDigitalGameListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    slug = serializers.CharField()
    main_image = serializers.CharField(allow_blank=True)
    short_description = serializers.CharField(allow_blank=True)
    purchase_flow = serializers.CharField()
    supported_customer_consoles = serializers.ListField(child=serializers.CharField())
    available_capacities = serializers.ListField(child=serializers.CharField())
    starting_price = serializers.DecimalField(max_digits=15, decimal_places=0)
    currency = serializers.CharField()
    availability = serializers.ChoiceField(choices=("AVAILABLE", "SOLD_OUT"))
    is_available = serializers.BooleanField()
    has_native_ps5_offer = serializers.BooleanField()
    has_ps4_compatible_ps5_offer = serializers.BooleanField()
    updated_at = serializers.DateTimeField()


class PublicDigitalGameDetailSerializer(PublicDigitalGameListItemSerializer):
    seo_title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    offers = PublicDigitalOfferSerializer(many=True)


class PaginatedPublicDigitalGameSerializer(serializers.Serializer):
    limit = serializers.IntegerField()
    offset = serializers.IntegerField()
    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = PublicDigitalGameListItemSerializer(many=True)
