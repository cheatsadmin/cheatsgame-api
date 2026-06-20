from django.http import HttpResponse
from django.db import transaction
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from cheatgame.api.mixins import ApiAuthMixin
from cheatgame.api.pagination import LimitOffsetPagination, get_paginated_response, PaginatedSerializer
from cheatgame.api.utils import inline_serializer
from cheatgame.common.utils import reformat_url, safe_file_url
from cheatgame.general.services import update_issue, check_issue_exists, delete_issue
from cheatgame.issue.filter import IssueReportFilter
from cheatgame.issue.models import (
    Issue,
    Tag,
    IssueType,
    IssueReport,
    IssueCategory,
    IssueTag,
    IssueListReport,
    RepairItem,
    RepairItemIssue,
    RepairItemType,
    IssueReportStatus,
    RepairStatusHistory,
)
from cheatgame.issue.selectors import issue_list, get_tag_list, issue_report_user, issue_report_list, \
    get_tag_list_of_issue
from cheatgame.issue.services import create_issue_report, update_issue_report, create_issue, create_issue_categories, \
    update_issue_category, delete_issue_category, create_tag, update_tag, delete_tag, create_issue_tags, \
    update_issue_tag, delete_issue_tag, get_issue
from cheatgame.product.models import CategoryType, Category
from cheatgame.product.permissions import CustomerPermission, IssueReportIsOwnerCustomer, AdminOrManagerPermission
from cheatgame.shop.models import DeliveryData, DeliveryScheduleType
from cheatgame.shop.services.delivery_schedule import DeliveryDataAlreadyUsedError, DeliverySlotFullError


ISSUE_TYPE_TO_DEVICE_TYPE = {
    IssueType.CONTROLLER.value: RepairItemType.CONTROLLER,
    IssueType.CONSOLE.value: RepairItemType.CONSOLE,
}

DEVICE_TYPE_TO_ISSUE_TYPE = {
    RepairItemType.CONTROLLER: IssueType.CONTROLLER.value,
    RepairItemType.CONSOLE: IssueType.CONSOLE.value,
}

ISSUE_TYPE_LABELS = {
    IssueType.CONTROLLER.value: "دسته",
    IssueType.CONSOLE.value: "کنسول",
}

DEFAULT_DEVICE_TAG_TITLES = {
    RepairItemType.CONTROLLER: "Controller",
    RepairItemType.CONSOLE: "Console",
}


def get_issue_type_value(issue: Issue):
    issue_types = {
        issue_tag.tag.issue_type
        for issue_tag in issue.tags.select_related("tag").all()
        if issue_tag.tag_id and issue_tag.tag.issue_type
    }
    if len(issue_types) == 1:
        return issue_types.pop()
    return None


def get_issue_device_type(issue: Issue):
    return ISSUE_TYPE_TO_DEVICE_TYPE.get(get_issue_type_value(issue))


def set_issue_device_type(issue: Issue, device_type):
    if not device_type:
        return
    issue_type = DEVICE_TYPE_TO_ISSUE_TYPE.get(device_type)
    if issue_type is None:
        return

    IssueTag.objects.filter(issue=issue).exclude(tag__issue_type=issue_type).delete()
    if IssueTag.objects.filter(issue=issue, tag__issue_type=issue_type).exists():
        return

    tag, _ = Tag.objects.get_or_create(
        title=DEFAULT_DEVICE_TAG_TITLES[device_type],
        issue_type=issue_type,
    )
    IssueTag.objects.create(issue=issue, tag=tag)


def issue_is_used(issue: Issue) -> bool:
    return (
        RepairItemIssue.objects.filter(issue=issue).exists()
        or IssueListReport.objects.filter(issue=issue).exists()
    )


class IssueListOutPutSerializer(serializers.ModelSerializer):
    picture = serializers.SerializerMethodField()
    description = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    issue_type = serializers.SerializerMethodField()
    device_type = serializers.SerializerMethodField()
    device_type_label = serializers.SerializerMethodField()
    is_used = serializers.SerializerMethodField()

    def get_picture(self, obj):
        return reformat_url(url=obj.picture.url)

    def get_description(self, obj):
        return reformat_url(url=obj.description.url)

    def get_tags(self, obj):
        return [
            {
                "id": issue_tag.id,
                "tag": issue_tag.tag_id,
                "title": issue_tag.tag.title,
                "issue_type": issue_tag.tag.issue_type,
            }
            for issue_tag in obj.tags.select_related("tag").all()
            if issue_tag.tag_id
        ]

    def get_issue_type(self, obj):
        return get_issue_type_value(obj)

    def get_device_type(self, obj):
        return get_issue_device_type(obj)

    def get_device_type_label(self, obj):
        return ISSUE_TYPE_LABELS.get(get_issue_type_value(obj), "")

    def get_is_used(self, obj):
        return issue_is_used(obj)






    class Meta:
        model = Issue
        fields = (
            "id",
            "title",
            "picture",
            "description",
            "max_price",
            "min_price",
            "tags",
            "issue_type",
            "device_type",
            "device_type_label",
            "is_active",
            "sort_order",
            "is_used",
        )


class RepairItemIssueOutPutSerializer(serializers.ModelSerializer):
    image = serializers.SerializerMethodField()

    def get_image(self, obj):
        return safe_file_url(file=obj.picture)

    class Meta:
        model = Issue
        fields = ("id", "title", "image")


class RepairItemOutPutSerializer(serializers.ModelSerializer):
    issues = serializers.SerializerMethodField()

    def get_issues(self, obj):
        issues = [repair_item_issue.issue for repair_item_issue in obj.item_issues.all()]
        return RepairItemIssueOutPutSerializer(issues, many=True).data

    class Meta:
        model = RepairItem
        fields = ("id", "item_type", "model", "customer_note", "sort_order", "issues")


class RepairItemInPutSerializer(serializers.Serializer):
    item_type = serializers.ChoiceField(
        choices=RepairItemType.choices,
        required=False,
        default=RepairItemType.UNKNOWN,
    )
    model = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    customer_note = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    sort_order = serializers.IntegerField(required=False, min_value=1)
    issue_ids = serializers.PrimaryKeyRelatedField(
        queryset=Issue.objects.all(),
        many=True,
        allow_empty=True,
    )

    def validate_issue_ids(self, value):
        if not value:
            raise serializers.ValidationError("برای هر دستگاه حداقل یک مشکل انتخاب کنید.")
        return value


ISSUE_REPORT_STATUS_LABELS = {
    IssueReportStatus.SUBMITTED.value: "ثبت شده",
    IssueReportStatus.RECEIVED.value: "دریافت شد",
    IssueReportStatus.INSPECTING.value: "در حال بررسی",
    IssueReportStatus.REPAIRING.value: "در حال تعمیر",
    IssueReportStatus.READY_FOR_DELIVERY.value: "آماده تحویل",
    IssueReportStatus.DELIVERED.value: "تحویل شد",
    IssueReportStatus.CANCELED.value: "لغو شد",
}


def get_issue_report_status_display(value):
    try:
        return IssueReportStatus(value).name
    except (TypeError, ValueError):
        return str(value) if value is not None else ""


def get_issue_report_status_label(value):
    try:
        return ISSUE_REPORT_STATUS_LABELS[IssueReportStatus(value).value]
    except (KeyError, TypeError, ValueError):
        return str(value) if value is not None else ""


class IssueReportStatusFieldsMixin:
    status_display = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    def get_status_display(self, obj):
        return get_issue_report_status_display(obj.status)

    def get_status_label(self, obj):
        return get_issue_report_status_label(obj.status)



class IssueListApi(APIView):
    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class FilterIssueSerializer(serializers.Serializer):
        categories__in = serializers.CharField(required=False, max_length=200)
        search = serializers.CharField(required=False, max_length=100)
        created_at__range = serializers.CharField(required=False, max_length=100)
        tags__in = serializers.CharField(required=False, max_length=100)
        is_active = serializers.CharField(required=False, max_length=10)

    class PaginationParameterSerializer(serializers.Serializer):
        limit = serializers.IntegerField(required=False)
        offset = serializers.IntegerField(required=False)

    class PaginatedIssueSerializer(PaginatedSerializer):
        results = IssueListOutPutSerializer(many=True)

    @extend_schema(parameters=[FilterIssueSerializer, PaginationParameterSerializer],
                   responses=PaginatedIssueSerializer)
    def get(self, request):
        filters_serializer = self.FilterIssueSerializer(data=request.query_params)
        filters_serializer.is_valid(raise_exception=True)
        try:
            query_set = issue_list(filters=filters_serializer.validated_data)
        except Exception as error:
            return Response(
                {"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)
        return get_paginated_response(
            pagination_class=self.Pagination,
            serializer_class=IssueListOutPutSerializer,
            queryset=query_set,
            view=self,
            request=request
        )


class TagListApi(APIView):
    class TagListOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Tag
            fields = ("title", "issue_type" , "id")

    class TagFilterSerializer(serializers.Serializer):
        issue_type = serializers.ChoiceField(choices=IssueType.choices())

    @extend_schema(responses=TagListOutPutSerializer, parameters=[TagFilterSerializer])
    def get(self, request):
        filters_serializer = self.TagFilterSerializer(data=request.query_params)
        filters_serializer.is_valid(raise_exception=True)
        try:
            issue_type = filters_serializer.validated_data.get("issue_type")
            tag_list = get_tag_list(issue_type=issue_type)
            return Response(self.TagListOutPutSerializer(tag_list, many=True).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueReportCreateApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class IssueReportCreateInPutSerializer(serializers.Serializer):
        explanation = serializers.CharField(max_length=1000, required=False, allow_blank=True)
        overall_explanation = serializers.CharField(max_length=1000, required=False, allow_blank=True)
        issue_list = serializers.PrimaryKeyRelatedField(
            queryset=Issue.objects.all(),
            many=True,
            required=False,
            allow_empty=False,
        )
        items = RepairItemInPutSerializer(many=True, required=False, allow_empty=False)

        def validate(self, attrs):
            has_legacy_issues = "issue_list" in attrs
            has_grouped_items = "items" in attrs
            if not has_legacy_issues and not has_grouped_items:
                raise serializers.ValidationError({
                    "issue_list": "حداقل یک مشکل تعمیر باید انتخاب شود.",
                    "items": "حداقل یک دستگاه تعمیر باید ثبت شود.",
                })
            if has_legacy_issues and has_grouped_items:
                raise serializers.ValidationError({
                    "items": "درخواست تعمیر را یا با issue_list قدیمی ارسال کنید یا با items جدید؛ هر دو همزمان مجاز نیست."
                })
            return attrs

    class IssueReportCreateOutPutSerializer(serializers.Serializer):
        issue_list_report = inline_serializer(many=True, read_only=True,
                                              fields={"id": serializers.CharField(max_length=50),
                                                      "issue": serializers.CharField(max_length=50)})
        items = RepairItemOutPutSerializer(many=True, read_only=True)
        id = serializers.IntegerField()
        user = inline_serializer(
            fields={
                "id": serializers.CharField(required=False),
                "first_name": serializers.CharField(required=False),
                "last_name": serializers.CharField(required=False)
            }
        )
        explanation = serializers.CharField(required=False)
        public_tracking_code = serializers.CharField(required=False)

    @extend_schema(request=IssueReportCreateInPutSerializer,
                   responses=IssueReportCreateOutPutSerializer)
    def post(self, request):
        serializer = self.IssueReportCreateInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        issue_report = create_issue_report(
            user=request.user,
            explanation=serializer.validated_data.get("explanation"),
            issue_list=serializer.validated_data.get("issue_list"),
            items=serializer.validated_data.get("items"),
            overall_explanation=serializer.validated_data.get("overall_explanation"),
        )
        return Response(self.IssueReportCreateOutPutSerializer(issue_report).data, status=status.HTTP_201_CREATED)
        # except Exception as e:
        #     return Response({"error": "مشکلی در گرفتن نوبت پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueReportDetailApi(ApiAuthMixin, APIView):
    permission_classes = (IssueReportIsOwnerCustomer, CustomerPermission,)

    class IssueReportDetailInPutSerializer(serializers.Serializer):
        delivery_data = serializers.PrimaryKeyRelatedField(
            queryset=DeliveryData.objects.filter(
                Q(schedule__type=DeliveryScheduleType.ISSUE.value) | Q(schedule__isnull=True)
            ))

    class IssueReportDetailOutPutSerializer(IssueReportStatusFieldsMixin, serializers.Serializer):
        issue_list_report = inline_serializer(many=True, read_only=True,
                                              fields={"id": serializers.CharField(max_length=50),
                                                      "issue": serializers.CharField(max_length=50)})
        items = RepairItemOutPutSerializer(many=True, read_only=True)
        id = serializers.IntegerField()
        user = inline_serializer(
            fields={
                "id": serializers.CharField(required=False),
                "first_name": serializers.CharField(required=False),
                "last_name": serializers.CharField(required=False)
            }
        )
        explanation = serializers.CharField(required=False)
        public_tracking_code = serializers.CharField(required=False)

        delivery_data = inline_serializer(
            allow_null=True,
            required=False,
            fields={
                "id": serializers.IntegerField(),
                "type": inline_serializer(fields={
                    "name": serializers.CharField(),
                    "delivery_type": serializers.IntegerField(),
                    "side": serializers.IntegerField()
                }),
                "schedule": inline_serializer(allow_null=True, required=False, fields={
                    "type": serializers.CharField(),
                    "start": serializers.DateTimeField(),
                    "end": serializers.DateTimeField()
                }),
                "address": inline_serializer(fields={
                    "address_detail": serializers.CharField(),
                    "postal_code": serializers.CharField(),
                })
            }
        )
        status = serializers.IntegerField(required=False)
        status_display = serializers.SerializerMethodField()
        status_label = serializers.SerializerMethodField()
        is_paid = serializers.BooleanField(required=False)
        created_at = serializers.DateTimeField(required=False)

    @extend_schema(request=IssueReportDetailInPutSerializer,
                   responses=IssueReportDetailOutPutSerializer)
    def get(self, request, id):
        issue_report = IssueReport.objects.select_related("user").prefetch_related(
            "issue_list_report__issue",
            "items__item_issues__issue",
        ).filter(id=id).first()
        if issue_report is None:
            return Response({"error": "درخواست تعمیر یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
        self.check_object_permissions(request, issue_report)
        return Response(self.IssueReportDetailOutPutSerializer(issue_report).data, status=status.HTTP_200_OK)

    @extend_schema(request=IssueReportDetailInPutSerializer,
                   responses=IssueReportDetailOutPutSerializer)
    def put(self, request, id):
        serializer = self.IssueReportDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # try:
        # TODO: check if five minute pass from created_at delivery_data is not valid.
        # TODO: add bool in delivery data instance is_used for not.
        issue_report = IssueReport.objects.filter(id=id).first()
        if issue_report is None:
            return Response({"error": "درخواست تعمیر یافت نشد."}, status=status.HTTP_400_BAD_REQUEST)
        self.check_object_permissions(request, issue_report)
        delivery_data = serializer.validated_data.get('delivery_data')
        if issue_report.delivery_data_id is not None:
            if issue_report.delivery_data_id == delivery_data.id:
                return Response(self.IssueReportDetailOutPutSerializer(issue_report).data, status=status.HTTP_200_OK)
            return Response({"error": "برای این درخواست تعمیر قبلا زمان رزرو شده است."},
                            status=status.HTTP_400_BAD_REQUEST)
        if delivery_data.address_id is not None and delivery_data.address.user_id != request.user.id:
            return Response({"error": "آدرس زمان تعمیر باید برای خود کاربر باشد."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            issue_report = update_issue_report(issue_report_id=id,
                                               user=request.user,
                                               delivery_data=delivery_data)
        except DeliverySlotFullError:
            return Response({"error": "ظرفیت این زمان تکمیل شده است."}, status=status.HTTP_400_BAD_REQUEST)
        except DeliveryDataAlreadyUsedError:
            return Response({"error": "این زمان قبلا رزرو شده است."}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError:
            return Response({"error": "برای این درخواست تعمیر قبلا زمان رزرو شده است."},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(self.IssueReportDetailOutPutSerializer(issue_report).data, status=status.HTTP_200_OK)
        # except Exception as e:
        #     return Response({"error": "مشکلی در گرفتن نوبت پیش آمده است."} , status = status.HTTP_400_BAD_REQUEST)


class GenerateHTML(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)
    class StringSerializer(serializers.Serializer):
        input_string = serializers.CharField(max_length=10000)

    @extend_schema(request=StringSerializer)
    def post(self, request):
        serializer = self.StringSerializer(data=request.data)
        if serializer.is_valid():
            html_content = serializer.validated_data['input_string']
            response = HttpResponse(html_content, content_type='text/html')
            response['Content-Disposition'] = 'attachment; filename=output.html'
            return response
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class IssueScheduleOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    type = inline_serializer(fields={
        "name": serializers.CharField(),
        "delivery_type": serializers.IntegerField(),
        "side": serializers.IntegerField()
    })
    schedule = inline_serializer(allow_null=True, required=False, fields={
        "type": serializers.CharField(),
        "start": serializers.DateTimeField(),
        "end": serializers.DateTimeField()
    })
    address = inline_serializer(fields={
        "province": serializers.CharField(),
        "city": serializers.CharField(),
        "address_detail": serializers.CharField(),
        "postal_code": serializers.CharField(),
    })


class IssueReportCustomerOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    first_name = serializers.CharField(source="firstname", required=False, allow_blank=True)
    last_name = serializers.CharField(source="lastname", required=False, allow_blank=True)
    firstname = serializers.CharField(required=False, allow_blank=True)
    lastname = serializers.CharField(required=False, allow_blank=True)
    phone_number = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_null=True, allow_blank=True)


class IssueReportDeliveryTypeOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(required=False, allow_blank=True)
    delivery_type = serializers.IntegerField(required=False)
    side = serializers.IntegerField(required=False)


class IssueReportScheduleOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    type = serializers.IntegerField(required=False)
    start = serializers.DateTimeField(required=False)
    end = serializers.DateTimeField(required=False)
    capacity = serializers.IntegerField(required=False)


class IssueReportAddressOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    province = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(required=False, allow_blank=True)
    address_detail = serializers.CharField(required=False, allow_blank=True)
    postal_code = serializers.CharField(required=False, allow_blank=True)


class IssueReportDeliveryDataOutPutSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    type = IssueReportDeliveryTypeOutPutSerializer()
    schedule = IssueReportScheduleOutPutSerializer(allow_null=True, required=False)
    address = IssueReportAddressOutPutSerializer(allow_null=True, required=False)
    is_used = serializers.BooleanField(required=False)


class IssueReportLegacyIssueOutPutSerializer(serializers.ModelSerializer):
    issue = RepairItemIssueOutPutSerializer()

    class Meta:
        model = IssueListReport
        fields = ("id", "issue")

class RepairStatusHistoryOutPutSerializer(serializers.ModelSerializer):
    old_status_display = serializers.SerializerMethodField()
    old_status_label = serializers.SerializerMethodField()
    new_status_display = serializers.SerializerMethodField()
    new_status_label = serializers.SerializerMethodField()
    changed_by = IssueReportCustomerOutPutSerializer(read_only=True)

    def get_old_status_display(self, obj):
        return get_issue_report_status_display(obj.old_status)

    def get_old_status_label(self, obj):
        return get_issue_report_status_label(obj.old_status)

    def get_new_status_display(self, obj):
        return get_issue_report_status_display(obj.new_status)

    def get_new_status_label(self, obj):
        return get_issue_report_status_label(obj.new_status)

    class Meta:
        model = RepairStatusHistory
        fields = (
            "id",
            "old_status",
            "old_status_display",
            "old_status_label",
            "new_status",
            "new_status_display",
            "new_status_label",
            "changed_by",
            "note",
            "created_at",
        )


class IssueReportListApi(ApiAuthMixin, APIView):
    permission_classes = (CustomerPermission,)

    class IssueReportListOutPutSerializer(IssueReportStatusFieldsMixin, serializers.ModelSerializer):
        delivery_data = IssueScheduleOutPutSerializer(allow_null=True, required=False)
        items = RepairItemOutPutSerializer(many=True, read_only=True)
        status_display = serializers.SerializerMethodField()
        status_label = serializers.SerializerMethodField()

        class Meta:
            model = IssueReport
            fields = (
                "id",
                "public_tracking_code",
                "user",
                "delivery_data",
                "explanation",
                "is_paid",
                "status",
                "status_display",
                "status_label",
                "items",
            )

    @extend_schema(responses=IssueReportListOutPutSerializer(many=True))
    def get(self, request):
        try:
            issue_reports = issue_report_user(user=request.user)
            return Response(self.IssueReportListOutPutSerializer(issue_reports, many=True).data,
                            status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی در دریافت لیست پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueCreateApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueCreateInPutSerializer(serializers.Serializer):
        picture = serializers.FileField()
        title = serializers.CharField(max_length=100)
        description = serializers.FileField()
        min_price = serializers.DecimalField(max_digits=15 , decimal_places=0)
        max_price = serializers.DecimalField(max_digits=15 , decimal_places=0)
        is_active = serializers.BooleanField(required=False, default=True)
        sort_order = serializers.IntegerField(required=False, min_value=0, default=0)
        device_type = serializers.ChoiceField(
            choices=((RepairItemType.CONTROLLER, "controller"), (RepairItemType.CONSOLE, "console")),
            required=False,
        )

    class IssueCreateOutPutSerializer(IssueListOutPutSerializer):
        pass

    @extend_schema(request=IssueCreateInPutSerializer, responses=IssueCreateOutPutSerializer)
    def post(self, request):
        serializer = self.IssueCreateInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            picture = request.FILES.get("picture")
            description = request.FILES.get("description")
            issue = create_issue(
                title=serializer.validated_data.get("title"),
                picture=picture,
                description=description,
                min_price=serializer.validated_data.get("min_price"),
                max_price=serializer.validated_data.get("max_price"),
                is_active=serializer.validated_data.get("is_active", True),
                sort_order=serializer.validated_data.get("sort_order", 0),
            )
            set_issue_device_type(issue, serializer.validated_data.get("device_type"))
            return Response(self.IssueCreateOutPutSerializer(issue).data, status=status.HTTP_201_CREATED)
        except Exception as e:
            return Response({"error": "مشکلی در ساخت پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)



class IssueCategoryOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueCategory
        fields = ("id" , "issue" , "category")

class IssueTagOutPutSerializer(serializers.ModelSerializer):
    class Meta:
        model = IssueTag
        fields = ("id" , "issue" , "tag")
class issueDetailUserApi(APIView):
    class IssueDetailUserOutPutSerializer(serializers.ModelSerializer):
        category_list = serializers.SerializerMethodField()
        tag_list = serializers.SerializerMethodField()
        picture = serializers.SerializerMethodField()
        description = serializers.SerializerMethodField()
        issue_type = serializers.SerializerMethodField()
        device_type = serializers.SerializerMethodField()
        device_type_label = serializers.SerializerMethodField()

        def get_picture(self, obj):
            return reformat_url(url=obj.picture.url)

        def get_description(self, obj):
            return reformat_url(url=obj.description.url)



        def get_tag_list(self, issue: Issue):
            tags = issue.tags.all()
            serializer = IssueTagOutPutSerializer(tags , many=True)
            return serializer.data

        def get_category_list(self, issue: Issue):
            categories = issue.categories.all()
            serializer = IssueCategoryOutPutSerializer(categories, many=True)
            return serializer.data

        def get_issue_type(self, issue: Issue):
            return get_issue_type_value(issue)

        def get_device_type(self, issue: Issue):
            return get_issue_device_type(issue)

        def get_device_type_label(self, issue: Issue):
            return ISSUE_TYPE_LABELS.get(get_issue_type_value(issue), "")

        class Meta:
            model = Issue
            fields = (
                "id",
                "title",
                "category_list",
                "tag_list",
                "description",
                "picture",
                "max_price",
                "min_price",
                "issue_type",
                "device_type",
                "device_type_label",
                "is_active",
                "sort_order",
            )

    @extend_schema(responses=IssueDetailUserOutPutSerializer)
    def get(self, request,id):
        try:
            if not check_issue_exists(issue_id=id):
                return Response({"error": "آیتم مورد نظر یافت نشد."} , status=status.HTTP_404_NOT_FOUND)
            issue = get_issue(issue_id= id)
            return Response(self.IssueDetailUserOutPutSerializer(issue).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response(
                {"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)



  
        
class IssueDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueDetailInPutSerializer(serializers.Serializer):
        picture = serializers.FileField(required=False)
        title = serializers.CharField(max_length=150)
        description = serializers.FileField(required=False)
        min_price = serializers.DecimalField(max_digits=15 , decimal_places=0)
        max_price = serializers.DecimalField(max_digits=15 , decimal_places=0)
        is_active = serializers.BooleanField(required=False, default=True)
        sort_order = serializers.IntegerField(required=False, min_value=0, default=0)
        device_type = serializers.ChoiceField(
            choices=((RepairItemType.CONTROLLER, "controller"), (RepairItemType.CONSOLE, "console")),
            required=False,
        )


    class IssueDetailOutPutSerializer(IssueListOutPutSerializer):
        pass


    @extend_schema(request=IssueDetailInPutSerializer, responses={status.HTTP_200_OK: IssueDetailOutPutSerializer})
    def put(self, request, id):
        serializer = self.IssueDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            if not  check_issue_exists(issue_id=id):
                return Response({"error": "مورد یافت نشد."},status=status.HTTP_404_NOT_FOUND)
            issue = update_issue(
                issue_id= id,
                title=serializer.validated_data["title"],
                max_price=serializer.validated_data["max_price"],
                min_price = serializer.validated_data["min_price"],
                description=serializer.validated_data.get("description" ,None),
                picture = serializer.validated_data.get("picture", None),
                is_active=serializer.validated_data.get("is_active", True),
                sort_order=serializer.validated_data.get("sort_order", 0),

            )
            set_issue_device_type(issue, serializer.validated_data.get("device_type"))
            return Response(self.IssueDetailOutPutSerializer(issue).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی در ویرایش issue رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: None})
    def delete(self, request, id):
        try:
            if not  check_issue_exists(issue_id=id):
                return Response({"error": "مورد یافت نشد."},status=status.HTTP_404_NOT_FOUND)
            issue = get_issue(issue_id=id)
            if issue_is_used(issue):
                return Response(
                    {"error": "این مشکل قبلاً در درخواست تعمیر استفاده شده است. به‌جای حذف، آن را غیرفعال کنید."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            delete_issue(issue_id=id)
            return Response({"message": "آیتم تعمیرات حذف شد."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است"}, status=status.HTTP_400_BAD_REQUEST)


class IssueCategoryAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueCategoryInPutSerializer(serializers.Serializer):
        issue = serializers.PrimaryKeyRelatedField(required=True, queryset=Issue.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type=CategoryType.SERVICE))

    @extend_schema(
        request=IssueCategoryInPutSerializer(many=True),
        responses={status.HTTP_201_CREATED: dict}
    )
    def post(self, request):
        serializer = self.IssueCategoryInPutSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        try:
            bulk_list = []
            for issue_category_data in serializer.validated_data:
                issue = issue_category_data['issue']
                category = issue_category_data['category']
                bulk_list.append(IssueCategory(issue=issue, category=category))
            create_issue_categories(issue_category=bulk_list)
            return Response({"message": "دسته بندی issue با موفقبت ساخته شد."}, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueCategoryDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueCategoryDetailInPutSerializer(serializers.Serializer):
        issue = serializers.PrimaryKeyRelatedField(required=True, queryset=Issue.objects.all())
        category = serializers.PrimaryKeyRelatedField(required=True, queryset=Category.objects.filter(
            category_type=CategoryType.SERVICE))

    class IssueCategoryDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = IssueCategory
            fields = ("id", "issue", "category",)

    @extend_schema(request=IssueCategoryDetailInPutSerializer,
                   responses={status.HTTP_200_OK: IssueCategoryDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.IssueCategoryDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            issue_category = update_issue_category(
                issue_category_id=id,
                issue=serializer.validated_data.get("issue"),
                category=serializer.validated_data.get("category")
            )
            return Response(self.IssueCategoryDetailOutPutSerializer(issue_category).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, reqeust, id: int):
        try:
            delete_issue_category(
                issue_category_id=id
            )
            return Response({"message": "دسته بندی issue با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueReportListOutPutSerializer(IssueReportStatusFieldsMixin, serializers.ModelSerializer):
    customer = IssueReportCustomerOutPutSerializer(source="user", read_only=True)
    user = IssueReportCustomerOutPutSerializer(read_only=True)
    delivery_data = IssueReportDeliveryDataOutPutSerializer(allow_null=True, required=False)
    items = RepairItemOutPutSerializer(many=True, read_only=True)
    item_count = serializers.SerializerMethodField()
    appointment_summary = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    status_label = serializers.SerializerMethodField()

    def get_item_count(self, obj):
        return obj.items.count()

    def get_appointment_summary(self, obj):
        schedule = getattr(getattr(obj, "delivery_data", None), "schedule", None)
        if schedule is None:
            return None
        return {
            "id": schedule.id,
            "start": schedule.start,
            "end": schedule.end,
        }

    class Meta:
        model = IssueReport
        fields = (
            "id",
            "public_tracking_code",
            "customer",
            "user",
            "delivery_data",
            "explanation",
            "is_paid",
            "status",
            "status_display",
            "status_label",
            "created_at",
            "item_count",
            "appointment_summary",
            "items",
        )


class IssueReportListAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class Pagination(LimitOffsetPagination):
        default_limit = 10

    class PaginatedUserListOutPutSerializer(PaginatedSerializer):
        results = IssueReportListOutPutSerializer(many=True)

    class IssueReportFilter(serializers.Serializer):
        created_at__range = serializers.CharField(max_length=200, required=False)
        user__phone_number = serializers.CharField(max_length=15, required=False)
        status = serializers.IntegerField(required=False)

    @extend_schema(responses=PaginatedUserListOutPutSerializer, parameters=[IssueReportFilter, ])
    def get(self, request):
        filter_serializer = self.IssueReportFilter(data=request.query_params)
        filter_serializer.is_valid(raise_exception=True)
        try:
            issue_reports = issue_report_list(filters=filter_serializer.validated_data)
            return get_paginated_response(
                pagination_class=self.Pagination,
                serializer_class=IssueReportListOutPutSerializer,
                queryset=issue_reports,
                view = self,
                request=request
            )
        except Exception as e:
            return Response({"error": "مشکلی در دریافت لیست پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueReportAdminDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueReportAdminDetailOutPutSerializer(IssueReportStatusFieldsMixin, serializers.ModelSerializer):
        customer = IssueReportCustomerOutPutSerializer(source="user", read_only=True)
        user = IssueReportCustomerOutPutSerializer(read_only=True)
        delivery_data = IssueReportDeliveryDataOutPutSerializer(allow_null=True, required=False)
        items = RepairItemOutPutSerializer(many=True, read_only=True)
        issue_list_report = IssueReportLegacyIssueOutPutSerializer(many=True, read_only=True)
        legacy_issue_list_report = IssueReportLegacyIssueOutPutSerializer(source="issue_list_report", many=True, read_only=True)
        status_history = RepairStatusHistoryOutPutSerializer(many=True, read_only=True)
        item_count = serializers.SerializerMethodField()
        status_display = serializers.SerializerMethodField()
        status_label = serializers.SerializerMethodField()

        def get_item_count(self, obj):
            return obj.items.count()

        class Meta:
            model = IssueReport
            fields = (
                "id",
                "public_tracking_code",
                "status",
                "status_display",
                "status_label",
                "created_at",
                "customer",
                "user",
                "delivery_data",
                "items",
                "issue_list_report",
                "legacy_issue_list_report",
                "status_history",
                "item_count",
                "explanation",
                "is_paid",
            )

    @extend_schema(responses=IssueReportAdminDetailOutPutSerializer)
    def get(self, request, id):
        issue_report = IssueReport.objects.select_related(
            "user",
            "delivery_data__type",
            "delivery_data__schedule",
            "delivery_data__address",
        ).prefetch_related(
            "items__item_issues__issue",
            "issue_list_report__issue",
            "status_history__changed_by",
        ).filter(id=id).first()
        if issue_report is None:
            return Response({"error": "درخواست تعمیر یافت نشد."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self.IssueReportAdminDetailOutPutSerializer(issue_report).data, status=status.HTTP_200_OK)


class IssueReportAdminStatusUpdateApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueReportStatusUpdateInPutSerializer(serializers.Serializer):
        status = serializers.CharField()
        note = serializers.CharField(max_length=1000, required=False, allow_blank=True)

        def validate_status(self, value):
            if isinstance(value, int):
                status_value = value
            else:
                normalized = str(value).strip()
                if normalized.isdigit():
                    status_value = int(normalized)
                else:
                    status_map = {status_item.name: status_item.value for status_item in IssueReportStatus}
                    status_value = status_map.get(normalized.upper())

            allowed_values = [status_item.value for status_item in IssueReportStatus]
            if status_value not in allowed_values:
                raise serializers.ValidationError("وضعیت انتخاب‌شده معتبر نیست.")
            return status_value

    @extend_schema(
        request=IssueReportStatusUpdateInPutSerializer,
        responses=IssueReportAdminDetailApi.IssueReportAdminDetailOutPutSerializer,
    )
    def patch(self, request, id):
        serializer = self.IssueReportStatusUpdateInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            issue_report = IssueReport.objects.select_for_update().filter(id=id).first()
            if issue_report is None:
                return Response({"error": "درخواست تعمیر یافت نشد."}, status=status.HTTP_404_NOT_FOUND)

            old_status = issue_report.status
            new_status = serializer.validated_data["status"]
            note = serializer.validated_data.get("note", "")

            if old_status != new_status:
                issue_report.status = new_status
                issue_report.save(update_fields=["status"])
                RepairStatusHistory.objects.create(
                    issue_report=issue_report,
                    old_status=old_status,
                    new_status=new_status,
                    changed_by=request.user,
                    note=note,
                )

        issue_report = IssueReport.objects.select_related(
            "user",
            "delivery_data__type",
            "delivery_data__schedule",
            "delivery_data__address",
        ).prefetch_related(
            "items__item_issues__issue",
            "issue_list_report__issue",
            "status_history__changed_by",
        ).get(id=id)
        return Response(
            IssueReportAdminDetailApi.IssueReportAdminDetailOutPutSerializer(issue_report).data,
            status=status.HTTP_200_OK,
        )


class CreateTagApi(ApiAuthMixin , APIView):
    permission_classes = (AdminOrManagerPermission,)

    class TagCreateInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=150)
        issue_type = serializers.ChoiceField(choices=IssueType.choices())


    class TagCreateOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Tag
            fields = ("id" , "title", "issue_type")


    @extend_schema(request=TagCreateInPutSerializer , responses=TagCreateOutPutSerializer)
    def post(self, request):
        serializer = self.TagCreateInPutSerializer(data = request.data)
        serializer.is_valid(raise_exception=True)
        try:
            issue_tag  =create_tag(
                title = serializer.validated_data.get("title"),
                issue_type = serializer.validated_data.get("issue_type")
            )
            return Response(self.TagCreateOutPutSerializer(issue_tag).data, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response({"error": "مشکلی در ساخت تگ پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueTagAdminApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueTagInPutSerializer(serializers.Serializer):
        issue = serializers.PrimaryKeyRelatedField(required=True, queryset=Issue.objects.all())
        tag = serializers.PrimaryKeyRelatedField(required=True, queryset=Tag.objects.all())

    @extend_schema(
        request=IssueTagInPutSerializer(many=True),
        responses={status.HTTP_201_CREATED: dict}
    )
    def post(self, request):
        serializer = self.IssueTagInPutSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)

        try:
            bulk_list = []
            for issue_tag_data in serializer.validated_data:
                issue = issue_tag_data['issue']
                tag = issue_tag_data['tag']
                bulk_list.append(IssueTag(issue=issue, tag=tag))
            create_issue_tags(issue_tag=bulk_list)
            return Response({"message": "تگ issue با موفقبت ساخته شد."}, status=status.HTTP_201_CREATED)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)


class IssueTagDetailApi(ApiAuthMixin, APIView):
    permission_classes = (AdminOrManagerPermission,)

    class IssueTagDetailInPutSerializer(serializers.Serializer):
        issue = serializers.PrimaryKeyRelatedField(required=True, queryset=Issue.objects.all())
        tag = serializers.PrimaryKeyRelatedField(required=True, queryset=Tag.objects.all())

    class IssueTagDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = IssueTag
            fields = ("id", "issue", "tag",)

    @extend_schema(request=IssueTagDetailInPutSerializer,
                   responses={status.HTTP_200_OK: IssueTagDetailOutPutSerializer})
    def put(self, request, id: int):
        serializer = self.IssueTagDetailInPutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            issue_category = update_issue_tag(
                issue_tag_id=id,
                issue=serializer.validated_data.get("issue"),
                tag=serializer.validated_data.get("tag")
            )
            return Response(self.IssueTagDetailOutPutSerializer(issue_category).data, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={status.HTTP_200_OK: dict})
    def delete(self, reqeust, id: int):
        try:
            delete_issue_tag(
                issue_tag_id=id
            )
            return Response({"message": "تگ issue با موفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as error:
            return Response({"error": "مشکلی رخ داده است."}, status=status.HTTP_400_BAD_REQUEST)



class TagDetailApi(ApiAuthMixin , APIView):
    permission_classes = (AdminOrManagerPermission,)

    class TagDetailInPutSerializer(serializers.Serializer):
        title = serializers.CharField(max_length=150)
        issue_type = serializers.ChoiceField(choices=IssueType.choices())

    class TagDetailOutPutSerializer(serializers.ModelSerializer):
        class Meta:
            model = Tag
            fields = ("id" , "title" , "issue_type")


    @extend_schema(request=TagDetailInPutSerializer, responses=TagDetailOutPutSerializer)
    def put(self , request , id):
        serializer = self.TagDetailInPutSerializer(data = request.data)
        serializer.is_valid(raise_exception=True)
        try:
            issue_tag = update_tag(
                tag_id=id,
                title=serializer.validated_data.get("title"),
                issue_type = serializer.validated_data.get("issue_type")
            )
            return Response(self.TagDetailOutPutSerializer(issue_tag).data , status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": "مشکلی در ویرایش تگ پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(responses={200 : dict})
    def delete(self, request , id):
        try:
            delete_tag(tag_id=id)
            return Response({"messgae" : "تگ باموفقیت حذف گردید."}, status=status.HTTP_200_OK)
        except Exception as e:
            return  Response({"error": "مشکلی در حذف پیش آمده است."}, status=status.HTTP_400_BAD_REQUEST)
