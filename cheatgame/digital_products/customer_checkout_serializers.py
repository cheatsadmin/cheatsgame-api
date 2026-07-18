from rest_framework import serializers

from cheatgame.digital_products.models import DigitalOfferCapacity
from cheatgame.product.models import NativeConsole


class StrictCheckoutInputSerializer(serializers.Serializer):
    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({field: "This field is not accepted." for field in unknown})
        return attrs


class PrepareDigitalCheckoutInputSerializer(StrictCheckoutInputSerializer):
    checkout_uuid = serializers.UUIDField()


class EmptyDigitalCheckoutInputSerializer(StrictCheckoutInputSerializer):
    pass


class CheckoutCodeLabelSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()


class DigitalCheckoutGameSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    slug = serializers.CharField(allow_blank=True)
    title = serializers.CharField()


class DigitalCheckoutLineSerializer(serializers.Serializer):
    offer_id = serializers.IntegerField()
    game = DigitalCheckoutGameSerializer()
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
    fulfillment_method = CheckoutCodeLabelSerializer()
    unit_price = serializers.DecimalField(max_digits=16, decimal_places=0)
    quantity = serializers.IntegerField()
    line_total = serializers.DecimalField(max_digits=16, decimal_places=0)
    currency = serializers.CharField()


class DigitalCheckoutTotalsSerializer(serializers.Serializer):
    subtotal = serializers.DecimalField(max_digits=16, decimal_places=0)
    discount = serializers.DecimalField(max_digits=16, decimal_places=0)
    total = serializers.DecimalField(max_digits=16, decimal_places=0)


class DigitalCheckoutOutputSerializer(serializers.Serializer):
    public_id = serializers.UUIDField()
    status = serializers.CharField()
    commerce_authority = serializers.CharField()
    commercial_revision = serializers.IntegerField(min_value=1)
    created_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    maximum_expires_at = serializers.DateTimeField()
    is_commercially_ready = serializers.BooleanField()
    is_payment_ready = serializers.BooleanField()
    readiness_code = serializers.CharField()
    can_cancel = serializers.BooleanField()
    currency = serializers.CharField()
    lines = DigitalCheckoutLineSerializer(many=True)
    totals = DigitalCheckoutTotalsSerializer()
