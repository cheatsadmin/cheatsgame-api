from rest_framework import serializers

from cheatgame.digital_products.models import (
    DigitalCartFulfillmentMethod,
    DigitalOfferCapacity,
)
from cheatgame.product.models import NativeConsole


class StrictDigitalCartInputSerializer(serializers.Serializer):
    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {field: "This field is not accepted." for field in unknown}
            )
        return attrs


class AddDigitalCartItemInputSerializer(StrictDigitalCartInputSerializer):
    offer_id = serializers.IntegerField(min_value=1)
    fulfillment_method = serializers.ChoiceField(
        choices=DigitalCartFulfillmentMethod.choices
    )


class ChangeDigitalFulfillmentMethodInputSerializer(StrictDigitalCartInputSerializer):
    fulfillment_method = serializers.ChoiceField(
        choices=DigitalCartFulfillmentMethod.choices
    )


class DigitalCodeLabelSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()


class DigitalCartGameSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    slug = serializers.CharField()
    title = serializers.CharField()
    main_image = serializers.CharField(allow_blank=True)


class DigitalCartSelectionOutputSerializer(serializers.Serializer):
    offer_id = serializers.IntegerField()
    game = DigitalCartGameSerializer()
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
    fulfillment_method = DigitalCodeLabelSerializer()
    unit_price = serializers.DecimalField(max_digits=16, decimal_places=0)
    line_total = serializers.DecimalField(max_digits=16, decimal_places=0)
    currency = serializers.CharField()
    availability = serializers.ChoiceField(choices=("AVAILABLE", "SOLD_OUT"))
    is_available = serializers.BooleanField()


class DigitalCartItemOutputSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    commerce_authority = serializers.ChoiceField(
        choices=("STANDARD_COMMERCE", "DIGITAL_PRODUCTS")
    )
    price = serializers.DecimalField(max_digits=16, decimal_places=0)
    quantity = serializers.IntegerField()
    digital_selection = DigitalCartSelectionOutputSerializer()


class RemoveDigitalCartItemOutputSerializer(serializers.Serializer):
    removed = serializers.BooleanField()
    cart_item_id = serializers.IntegerField()
