from rest_framework import serializers


class StrictPaymentInputSerializer(serializers.Serializer):
    def validate(self, attrs):
        unknown = sorted(set(self.initial_data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({field: "This field is not accepted." for field in unknown})
        return attrs


class DigitalPaymentRequestInputSerializer(StrictPaymentInputSerializer):
    provider = serializers.RegexField(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class DigitalPaymentOutputSerializer(serializers.Serializer):
    checkout_id = serializers.UUIDField()
    checkout_status = serializers.CharField()
    order_reference = serializers.CharField(allow_blank=True)
    payment_id = serializers.UUIDField(allow_null=True)
    payment_status = serializers.CharField(allow_null=True)
    amount_due = serializers.DecimalField(max_digits=20, decimal_places=0, allow_null=True)
    currency = serializers.CharField(allow_null=True)
    attempt_id = serializers.UUIDField(allow_null=True)
    attempt_status = serializers.CharField(allow_null=True)
    transaction_id = serializers.UUIDField(allow_null=True)
    transaction_status = serializers.CharField(allow_null=True)
    provider = serializers.CharField(allow_null=True)
    customer_action_url = serializers.URLField(allow_null=True)
    can_retry = serializers.BooleanField()
    do_not_pay_again = serializers.BooleanField()
    payment_received = serializers.BooleanField()
    replayed = serializers.BooleanField()
