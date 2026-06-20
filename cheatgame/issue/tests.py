from datetime import timedelta
import importlib

from django.apps import apps
from django.test import TestCase, override_settings
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APIClient

from cheatgame.issue.models import (
    Issue,
    IssueType,
    IssueTag,
    IssueListReport,
    IssueReport,
    IssueReportStatus,
    RepairStatusHistory,
    RepairItem,
    RepairItemIssue,
    RepairItemType,
    Tag,
)
from cheatgame.shop.models import (
    DeliveryData,
    DeliverySchedule,
    DeliveryScheduleType,
    DeliverySide,
    DeliveryType,
)
from cheatgame.product.models import DeliveryOption
from cheatgame.users.models import Address, BaseUser, UserTypes


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1", "localhost"])
class IssueSearchTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        Issue.objects.create(
            title="HDMI port repair",
            picture="issue/hdmi.svg",
            description="issue/hdmi.html",
            min_price=900000,
            max_price=2500000,
        )
        Issue.objects.create(
            title="Analog drift repair",
            picture="issue/analog.svg",
            description="issue/analog.html",
            min_price=350000,
            max_price=750000,
        )

    def test_issue_search_does_not_crash_on_sqlite(self):
        response = self.client.get("/api/issue/issue-list/", {"search": "HDMI"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [item["title"] for item in response.data["results"]]
        self.assertEqual(titles, ["HDMI port repair"])


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1", "localhost"])
class IssueCatalogMetadataTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.manager = BaseUser.objects.create_user(
            phone_number="09128880401",
            firstname="Issue",
            lastname="Manager",
            password="StrongPass123!",
            user_type=UserTypes.MANAGER,
        )
        self.customer = BaseUser.objects.create_user(
            phone_number="09128880402",
            firstname="Issue",
            lastname="Customer",
            password="StrongPass123!",
        )

    def create_issue(self, title, *, is_active=True, sort_order=0, issue_type=IssueType.CONTROLLER):
        issue = Issue.objects.create(
            title=title,
            picture="issue/test.svg",
            description="issue/test.html",
            min_price=100000,
            max_price=300000,
            is_active=is_active,
            sort_order=sort_order,
        )
        tag = Tag.objects.create(title=f"{title} tag", issue_type=issue_type.value)
        IssueTag.objects.create(issue=issue, tag=tag)
        return issue

    def test_issue_list_returns_metadata_and_orders_by_sort_order(self):
        controller_issue = self.create_issue("Controller issue", sort_order=20, issue_type=IssueType.CONTROLLER)
        console_issue = self.create_issue("Console issue", sort_order=10, issue_type=IssueType.CONSOLE)

        response = self.client.get("/api/issue/issue-list/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = response.data["results"]
        self.assertEqual([row["id"] for row in rows], [console_issue.id, controller_issue.id])
        self.assertEqual(rows[0]["device_type"], "console")
        self.assertEqual(rows[0]["device_type_label"], "کنسول")
        self.assertEqual(rows[0]["issue_type"], IssueType.CONSOLE.value)
        self.assertEqual(rows[0]["sort_order"], 10)
        self.assertTrue(rows[0]["is_active"])
        self.assertEqual(rows[0]["tags"][0]["issue_type"], IssueType.CONSOLE.value)
        self.assertEqual(rows[1]["device_type"], "controller")
        self.assertEqual(rows[1]["device_type_label"], "دسته")

    def test_issue_list_can_filter_active_issues(self):
        active_issue = self.create_issue("Active issue", is_active=True)
        self.create_issue("Inactive issue", is_active=False)

        response = self.client.get("/api/issue/issue-list/", {"is_active": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([row["id"] for row in response.data["results"]], [active_issue.id])

    def test_issue_create_sets_metadata_and_device_type(self):
        self.client.force_authenticate(self.manager)

        response = self.client.post(
            "/api/issue/issue-create/",
            {
                "title": "Owner managed issue",
                "picture": SimpleUploadedFile("issue.png", b"image-content", content_type="image/png"),
                "description": SimpleUploadedFile("issue.html", b"<p>description</p>", content_type="text/html"),
                "min_price": "100000",
                "max_price": "300000",
                "is_active": "false",
                "sort_order": "7",
                "device_type": "controller",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        issue = Issue.objects.get(id=response.data["id"])
        self.assertFalse(issue.is_active)
        self.assertEqual(issue.sort_order, 7)
        self.assertEqual(IssueTag.objects.get(issue=issue).tag.issue_type, IssueType.CONTROLLER.value)
        self.assertEqual(response.data["device_type"], "controller")

    def test_used_issue_delete_is_blocked_with_persian_error(self):
        issue = self.create_issue("Used issue")
        report = IssueReport.objects.create(user=self.customer, explanation="Used repair")
        repair_item = RepairItem.objects.create(issue_report=report, item_type=RepairItemType.CONTROLLER)
        RepairItemIssue.objects.create(repair_item=repair_item, issue=issue)
        IssueListReport.objects.create(report=report, issue=issue)
        self.client.force_authenticate(self.manager)

        response = self.client.delete(f"/api/issue/issue-detail/{issue.id}/")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn("غیرفعال", response.data["error"])
        self.assertTrue(Issue.objects.filter(id=issue.id).exists())

    def test_unused_issue_delete_succeeds(self):
        issue = self.create_issue("Unused issue")
        self.client.force_authenticate(self.manager)

        response = self.client.delete(f"/api/issue/issue-detail/{issue.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(Issue.objects.filter(id=issue.id).exists())


@override_settings(ALLOWED_HOSTS=["testserver", "127.0.0.1", "localhost"])
class IssueReportMultiItemTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = BaseUser.objects.create_user(
            phone_number="09128880101",
            firstname="Repair",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.user.phone_verified = True
        self.user.save(update_fields=["phone_verified"])
        self.client.force_authenticate(self.user)
        self.hdmi_issue = self.create_issue("HDMI repair")
        self.drift_issue = self.create_issue("DualSense drift")
        self.trigger_issue = self.create_issue("Trigger repair")

    def create_issue(self, title):
        return Issue.objects.create(
            title=title,
            picture="issue/test.svg",
            description="issue/test.html",
            min_price=100000,
            max_price=300000,
        )

    def test_legacy_issue_list_creates_one_repair_item(self):
        response = self.client.post(
            "/api/issue/issue-report/",
            {"explanation": "Legacy repair note", "issue_list": [self.drift_issue.id, self.trigger_issue.id]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue_report = IssueReport.objects.get(id=response.data["id"])
        repair_item = issue_report.items.get()
        self.assertEqual(repair_item.item_type, RepairItemType.LEGACY)
        self.assertEqual(repair_item.customer_note, "Legacy repair note")
        self.assertEqual(
            list(repair_item.item_issues.order_by("issue_id").values_list("issue_id", flat=True)),
            [self.drift_issue.id, self.trigger_issue.id],
        )
        self.assertEqual(IssueListReport.objects.filter(report=issue_report).count(), 2)
        self.assertEqual(len(response.data["items"]), 1)
        self.assertEqual(response.data["items"][0]["item_type"], RepairItemType.LEGACY)

    def test_grouped_items_payload_creates_multiple_repair_items(self):
        response = self.client.post(
            "/api/issue/issue-report/",
            {
                "overall_explanation": "One FX case with multiple devices",
                "items": [
                    {
                        "item_type": RepairItemType.CONSOLE,
                        "model": "ps5",
                        "issue_ids": [self.hdmi_issue.id],
                        "customer_note": "No HDMI output",
                    },
                    {
                        "item_type": RepairItemType.CONTROLLER,
                        "model": "dualsense",
                        "issue_ids": [self.drift_issue.id, self.trigger_issue.id],
                        "customer_note": "Controller symptoms",
                    },
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue_report = IssueReport.objects.get(id=response.data["id"])
        self.assertEqual(issue_report.explanation, "One FX case with multiple devices")
        repair_items = list(issue_report.items.order_by("sort_order"))
        self.assertEqual(len(repair_items), 2)
        self.assertEqual(repair_items[0].item_type, RepairItemType.CONSOLE)
        self.assertEqual(repair_items[0].model, "ps5")
        self.assertEqual(repair_items[1].item_type, RepairItemType.CONTROLLER)
        self.assertEqual(repair_items[1].model, "dualsense")
        self.assertEqual(repair_items[0].item_issues.count(), 1)
        self.assertEqual(repair_items[1].item_issues.count(), 2)
        self.assertEqual(IssueListReport.objects.filter(report=issue_report).count(), 3)
        self.assertEqual([item["item_type"] for item in response.data["items"]], ["console", "controller"])

    def test_grouped_payload_requires_at_least_one_issue_per_item(self):
        response = self.client.post(
            "/api/issue/issue-report/",
            {
                "items": [
                    {
                        "item_type": RepairItemType.CONTROLLER,
                        "model": "dualsense",
                        "issue_ids": [],
                    }
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("issue_ids", response.data["detail"]["items"][0])
        self.assertEqual(
            str(response.data["detail"]["items"][0]["issue_ids"][0]),
            "برای هر دستگاه حداقل یک مشکل انتخاب کنید.",
        )

    def test_report_list_and_detail_keep_legacy_fields_and_return_items(self):
        create_response = self.client.post(
            "/api/issue/issue-report/",
            {
                "items": [
                    {
                        "item_type": RepairItemType.CONSOLE,
                        "model": "ps5",
                        "issue_ids": [self.hdmi_issue.id],
                    }
                ]
            },
            format="json",
        )

        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)
        issue_report_id = create_response.data["id"]

        list_response = self.client.get("/api/issue/issue-report-list/")
        detail_response = self.client.get(f"/api/issue/issue-report-detail/{issue_report_id}/")

        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(detail_response.status_code, status.HTTP_200_OK)
        self.assertIn("items", list_response.data[0])
        self.assertEqual(list_response.data[0]["items"][0]["model"], "ps5")
        self.assertIn("issue_list_report", detail_response.data)
        self.assertIn("items", detail_response.data)
        self.assertEqual(detail_response.data["items"][0]["issues"][0]["title"], "HDMI repair")

    def test_backfill_old_reports_have_one_repair_item(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Old flat report")
        IssueListReport.objects.bulk_create([
            IssueListReport(report=issue_report, issue=self.drift_issue),
            IssueListReport(report=issue_report, issue=self.trigger_issue),
        ])
        migration_module = importlib.import_module("cheatgame.issue.migrations.0016_repairitem_repairitemissue")

        migration_module.backfill_repair_items(apps, None)

        repair_item = RepairItem.objects.get(issue_report=issue_report)
        self.assertEqual(repair_item.item_type, RepairItemType.LEGACY)
        self.assertEqual(repair_item.customer_note, "Old flat report")
        self.assertEqual(RepairItemIssue.objects.filter(repair_item=repair_item).count(), 2)


class IssueReportStatusMigrationTests(TestCase):
    def setUp(self):
        self.user = BaseUser.objects.create_user(
            phone_number="09128880301",
            firstname="Status",
            lastname="Customer",
            password="StrongPass123!",
        )

    def test_old_status_values_map_to_repair_status_v1(self):
        submitted_report = IssueReport.objects.create(user=self.user, status=1)
        done_report = IssueReport.objects.create(user=self.user, status=2)
        canceled_report = IssueReport.objects.create(user=self.user, status=3)
        imperfect_report = IssueReport.objects.create(user=self.user, status=4)
        migration_module = importlib.import_module("cheatgame.issue.migrations.0017_repair_status_v1")

        migration_module.migrate_old_repair_statuses(apps, None)

        submitted_report.refresh_from_db()
        done_report.refresh_from_db()
        canceled_report.refresh_from_db()
        imperfect_report.refresh_from_db()
        self.assertEqual(submitted_report.status, IssueReportStatus.SUBMITTED.value)
        self.assertEqual(done_report.status, IssueReportStatus.DELIVERED.value)
        self.assertEqual(canceled_report.status, IssueReportStatus.CANCELED.value)
        self.assertEqual(imperfect_report.status, IssueReportStatus.SUBMITTED.value)


class IssueReportAdminReadOnlyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = BaseUser.objects.create_user(
            phone_number="09128880201",
            firstname="Repair",
            lastname="Customer",
            password="StrongPass123!",
        )
        self.customer.phone_verified = True
        self.customer.email = "repair-customer@example.com"
        self.customer.save(update_fields=["phone_verified", "email"])
        self.manager = BaseUser.objects.create_user(
            phone_number="09128880202",
            firstname="Repair",
            lastname="Manager",
            password="StrongPass123!",
            user_type=UserTypes.MANAGER,
        )
        self.hdmi_issue = self.create_issue("HDMI repair")
        self.drift_issue = self.create_issue("DualSense drift")
        self.address = Address.objects.create(
            user=self.customer,
            province="Tehran",
            city="Tehran",
            postal_code="1234567890",
            address_detail="Repair address",
        )
        self.delivery_type = DeliveryType.objects.create(
            name="تحویل حضوری",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        start = timezone.now() + timedelta(days=5)
        self.schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=start,
            end=start + timedelta(hours=2),
            capacity=15,
        )
        self.delivery_data = DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=self.address,
        )

    def create_issue(self, title):
        return Issue.objects.create(
            title=title,
            picture="issue/test.svg",
            description="issue/test.html",
            min_price=100000,
            max_price=300000,
        )

    def create_report(self):
        issue_report = IssueReport.objects.create(
            user=self.customer,
            explanation="Multi-device repair",
            delivery_data=self.delivery_data,
            status=IssueReportStatus.DURING,
        )
        console_item = RepairItem.objects.create(
            issue_report=issue_report,
            item_type=RepairItemType.CONSOLE,
            model="ps5",
            customer_note="No image",
            sort_order=1,
        )
        controller_item = RepairItem.objects.create(
            issue_report=issue_report,
            item_type=RepairItemType.CONTROLLER,
            model="dualsense",
            customer_note="Drift",
            sort_order=2,
        )
        RepairItemIssue.objects.create(repair_item=console_item, issue=self.hdmi_issue)
        RepairItemIssue.objects.create(repair_item=controller_item, issue=self.drift_issue)
        IssueListReport.objects.create(report=issue_report, issue=self.hdmi_issue)
        IssueListReport.objects.create(report=issue_report, issue=self.drift_issue)
        return issue_report

    def test_manager_admin_list_includes_operational_repair_fields(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.manager)

        response = self.client.get("/api/issue/issue-report-list-admin/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        row = response.data["results"][0]
        self.assertEqual(row["id"], issue_report.id)
        self.assertEqual(row["public_tracking_code"], issue_report.public_tracking_code)
        self.assertEqual(row["customer"]["phone_number"], self.customer.phone_number)
        self.assertEqual(row["customer"]["first_name"], self.customer.firstname)
        self.assertEqual(row["delivery_data"]["type"]["name"], self.delivery_type.name)
        self.assertEqual(row["delivery_data"]["address"]["address_detail"], self.address.address_detail)
        self.assertEqual(row["item_count"], 2)
        self.assertEqual(row["appointment_summary"]["id"], self.schedule.id)
        self.assertEqual(row["status"], IssueReportStatus.SUBMITTED.value)
        self.assertEqual(row["status_display"], "SUBMITTED")
        self.assertEqual(row["status_label"], "ثبت شده")

    def test_manager_can_retrieve_admin_repair_detail_with_grouped_items(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.manager)

        response = self.client.get(f"/api/issue/admin/issue-report/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["id"], issue_report.id)
        self.assertEqual(response.data["public_tracking_code"], issue_report.public_tracking_code)
        self.assertEqual(response.data["customer"]["phone_number"], self.customer.phone_number)
        self.assertEqual(response.data["customer"]["email"], self.customer.email)
        self.assertEqual(response.data["delivery_data"]["schedule"]["id"], self.schedule.id)
        self.assertEqual(response.data["delivery_data"]["address"]["city"], self.address.city)
        self.assertEqual(response.data["item_count"], 2)
        self.assertEqual([item["model"] for item in response.data["items"]], ["ps5", "dualsense"])
        self.assertEqual(response.data["items"][0]["issues"][0]["title"], self.hdmi_issue.title)
        self.assertIn("image", response.data["items"][0]["issues"][0])
        self.assertEqual(response.data["issue_list_report"][0]["issue"]["title"], self.hdmi_issue.title)
        self.assertEqual(response.data["status_label"], "ثبت شده")

    def test_manager_can_update_repair_status_and_history_is_created(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.manager)

        response = self.client.patch(
            f"/api/issue/admin/issue-report/{issue_report.id}/status/",
            {"status": "INSPECTING", "note": "بررسی اولیه شروع شد"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        issue_report.refresh_from_db()
        self.assertEqual(issue_report.status, IssueReportStatus.INSPECTING.value)
        self.assertEqual(response.data["status"], IssueReportStatus.INSPECTING.value)
        self.assertEqual(response.data["status_display"], "INSPECTING")
        self.assertEqual(response.data["status_label"], "در حال بررسی")
        history = RepairStatusHistory.objects.get(issue_report=issue_report)
        self.assertEqual(history.old_status, IssueReportStatus.SUBMITTED.value)
        self.assertEqual(history.new_status, IssueReportStatus.INSPECTING.value)
        self.assertEqual(history.changed_by, self.manager)
        self.assertEqual(history.note, "بررسی اولیه شروع شد")
        self.assertEqual(response.data["status_history"][0]["new_status_label"], "در حال بررسی")

    def test_status_update_rejects_invalid_status(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.manager)

        response = self.client.patch(
            f"/api/issue/admin/issue-report/{issue_report.id}/status/",
            {"status": "UNKNOWN_STATUS"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        self.assertEqual(issue_report.status, IssueReportStatus.SUBMITTED.value)

    def test_customer_cannot_retrieve_admin_repair_detail(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.customer)

        response = self.client.get(f"/api/issue/admin/issue-report/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_update_admin_repair_status(self):
        issue_report = self.create_report()
        self.client.force_authenticate(self.customer)

        response = self.client.patch(
            f"/api/issue/admin/issue-report/{issue_report.id}/status/",
            {"status": "INSPECTING"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class IssueReportOwnershipTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.create_verified_user("09128880001")
        self.other_user = self.create_verified_user("09128880002")
        self.address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="1234567890",
            address_detail="Repair address",
        )
        self.other_address = Address.objects.create(
            user=self.other_user,
            province="Tehran",
            city="Tehran",
            postal_code="2234567890",
            address_detail="Other repair address",
        )
        self.delivery_type = DeliveryType.objects.create(
            name="Repair pickup",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        start = timezone.now() + timedelta(days=5)
        self.schedule = DeliverySchedule.objects.create(
            type=DeliveryScheduleType.ISSUE,
            start=start,
            end=start + timedelta(hours=2),
            capacity=2,
        )
        self.client.force_authenticate(self.user)

    def create_verified_user(self, phone_number):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Issue",
            lastname="User",
            password="StrongPass123!",
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def create_delivery_data(self, address):
        return DeliveryData.objects.create(
            type=self.delivery_type,
            schedule=self.schedule,
            address=address,
        )

    def test_customer_can_update_own_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        issue_report.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, delivery_data.id)
        self.assertEqual(issue_report.status, IssueReportStatus.DURING)
        self.assertTrue(delivery_data.is_used)

    def test_customer_can_retrieve_own_issue_report_detail(self):
        delivery_data = self.create_delivery_data(self.address)
        issue_report = IssueReport.objects.create(
            user=self.user,
            explanation="Own repair",
            delivery_data=delivery_data,
            status=IssueReportStatus.DURING,
        )

        response = self.client.get(f"/api/issue/issue-report-detail/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], issue_report.id)
        self.assertEqual(response.data["public_tracking_code"], issue_report.public_tracking_code)
        self.assertTrue(response.data["public_tracking_code"].startswith("FX-"))
        self.assertEqual(response.data["delivery_data"]["id"], delivery_data.id)
        self.assertEqual(response.data["delivery_data"]["address"]["address_detail"], self.address.address_detail)
        self.assertEqual(response.data["status"], IssueReportStatus.DURING.value)
        self.assertFalse(response.data["is_paid"])

    def test_issue_reports_get_unique_public_tracking_codes(self):
        first_report = IssueReport.objects.create(user=self.user, explanation="First repair")
        second_report = IssueReport.objects.create(user=self.user, explanation="Second repair")

        self.assertTrue(first_report.public_tracking_code.startswith("FX-"))
        self.assertTrue(second_report.public_tracking_code.startswith("FX-"))
        self.assertNotEqual(first_report.public_tracking_code, second_report.public_tracking_code)
        self.assertNotEqual(first_report.public_tracking_code, str(first_report.id))

    def test_customer_issue_report_list_returns_public_tracking_code(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")

        response = self.client.get("/api/issue/issue-report-list/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["id"], issue_report.id)
        self.assertEqual(response.data[0]["public_tracking_code"], issue_report.public_tracking_code)
        self.assertTrue(response.data[0]["public_tracking_code"].startswith("FX-"))

    def test_customer_cannot_retrieve_another_users_issue_report_detail(self):
        issue_report = IssueReport.objects.create(user=self.other_user, explanation="Other repair")

        response = self.client.get(f"/api/issue/issue-report-detail/{issue_report.id}/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_customer_cannot_replace_issue_report_reservation(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        first_delivery_data = self.create_delivery_data(self.address)
        first_delivery_data.is_used = True
        first_delivery_data.save(update_fields=["is_used"])
        issue_report.delivery_data = first_delivery_data
        issue_report.status = IssueReportStatus.DURING
        issue_report.save(update_fields=["delivery_data", "status"])
        second_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="3234567890",
            address_detail="Second repair address",
        )
        second_delivery_data = self.create_delivery_data(second_address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": second_delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        second_delivery_data.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, first_delivery_data.id)
        self.assertFalse(second_delivery_data.is_used)

    def test_customer_can_resubmit_same_issue_report_reservation_without_duplicate(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.address)
        delivery_data.is_used = True
        delivery_data.save(update_fields=["is_used"])
        issue_report.delivery_data = delivery_data
        issue_report.status = IssueReportStatus.DURING
        issue_report.save(update_fields=["delivery_data", "status"])

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 1)

    def test_customer_cannot_schedule_issue_report_when_slot_is_full(self):
        self.schedule.capacity = 1
        self.schedule.save(update_fields=["capacity"])
        used_delivery_data = self.create_delivery_data(self.address)
        used_delivery_data.is_used = True
        used_delivery_data.save(update_fields=["is_used"])
        other_address = Address.objects.create(
            user=self.user,
            province="Tehran",
            city="Tehran",
            postal_code="4234567890",
            address_detail="Another repair address",
        )
        candidate_delivery_data = self.create_delivery_data(other_address)
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": candidate_delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        candidate_delivery_data.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertFalse(candidate_delivery_data.is_used)

    def test_customer_can_schedule_issue_report_same_address_when_slot_has_capacity(self):
        used_delivery_data = self.create_delivery_data(self.address)
        used_delivery_data.is_used = True
        used_delivery_data.save(update_fields=["is_used"])
        existing_issue_report = IssueReport.objects.create(user=self.user, explanation="Existing repair")
        existing_issue_report.delivery_data = used_delivery_data
        existing_issue_report.status = IssueReportStatus.DURING
        existing_issue_report.save(update_fields=["delivery_data", "status"])
        issue_report = IssueReport.objects.create(user=self.user, explanation="New repair")

        book_time_response = self.client.post(
            "/api/shop/book-time/",
            {"type": self.delivery_type.id, "schedule": self.schedule.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(book_time_response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(book_time_response.data["id"], used_delivery_data.id)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": book_time_response.data["id"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        issue_report.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, book_time_response.data["id"])
        self.assertEqual(DeliveryData.objects.filter(schedule=self.schedule, is_used=True).count(), 2)

    def test_customer_can_attach_issue_delivery_method_without_appointment(self):
        delivery_type_without_appointment = DeliveryType.objects.create(
            name="ارسال با پیک",
            delivery_type=DeliveryOption.MOTOR,
            side=DeliverySide.RECIEVEFROMUSER,
        )
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")

        book_time_response = self.client.post(
            "/api/shop/book-time/",
            {"type": delivery_type_without_appointment.id, "address": self.address.id},
            format="json",
        )

        self.assertEqual(book_time_response.status_code, status.HTTP_200_OK, book_time_response.data)
        delivery_data = DeliveryData.objects.get(id=book_time_response.data["id"])
        self.assertIsNone(delivery_data.schedule_id)
        self.assertFalse(delivery_data.is_used)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        issue_report.refresh_from_db()
        delivery_data.refresh_from_db()
        self.assertEqual(issue_report.delivery_data_id, delivery_data.id)
        self.assertIsNone(issue_report.delivery_data.schedule_id)
        self.assertEqual(issue_report.status, IssueReportStatus.DURING)
        self.assertFalse(delivery_data.is_used)

    def test_customer_cannot_update_another_users_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.other_user, explanation="Other repair")
        delivery_data = self.create_delivery_data(self.address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        issue_report.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertEqual(issue_report.status, IssueReportStatus.IMPERFECT)

    def test_customer_cannot_use_another_users_delivery_data_for_issue_report(self):
        issue_report = IssueReport.objects.create(user=self.user, explanation="Own repair")
        delivery_data = self.create_delivery_data(self.other_address)

        response = self.client.put(
            f"/api/issue/issue-report-detail/{issue_report.id}/",
            {"delivery_data": delivery_data.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        issue_report.refresh_from_db()
        self.assertIsNone(issue_report.delivery_data_id)
        self.assertEqual(issue_report.status, IssueReportStatus.IMPERFECT)


class GenerateHtmlPermissionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.customer = self.create_user("09128880003")
        self.manager = self.create_user("09128880004", user_type=UserTypes.MANAGER)

    def create_user(self, phone_number, user_type=UserTypes.CUSTOMER):
        user = BaseUser.objects.create_user(
            phone_number=phone_number,
            firstname="Html",
            lastname="User",
            password="StrongPass123!",
            user_type=user_type,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        return user

    def test_anonymous_cannot_generate_html(self):
        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_customer_cannot_generate_html(self):
        self.client.force_authenticate(self.customer)

        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_can_generate_html(self):
        self.client.force_authenticate(self.manager)

        response = self.client.post("/api/issue/generate-html/", {"input_string": "<b>x</b>"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Content-Disposition"], "attachment; filename=output.html")
