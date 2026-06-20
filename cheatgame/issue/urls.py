from django.urls import path

from cheatgame.issue.apis import IssueListApi, TagListApi, GenerateHTML, IssueReportCreateApi, IssueReportDetailApi, \
    IssueReportListApi, IssueCreateApi, IssueCategoryAdminApi, IssueCategoryDetailApi, IssueReportListAdminApi, \
    IssueReportAdminDetailApi, IssueReportAdminStatusUpdateApi, CreateTagApi, TagDetailApi, IssueDetailApi, \
    IssueTagAdminApi, IssueTagDetailApi, issueDetailUserApi

urlpatterns = [
    path("issue-list/", IssueListApi.as_view(), name="issue-list"),
    path("tag-list/", TagListApi.as_view(), name="tag-list"),
    path("generate-html/", GenerateHTML.as_view(), name="generate-html"),
    path("issue-report/", IssueReportCreateApi.as_view(), name="issue-report-user"),
    path("issue-report-detail/<int:id>/" , IssueReportDetailApi.as_view() , name= "issue-report-detail"),
    path("issue-report-list/" ,IssueReportListApi.as_view() , name="issue-report-list-user"),
    path("issue-create/" , IssueCreateApi.as_view() , name= "issue-create-admin"),
    path("issue-category/"  , IssueCategoryAdminApi.as_view() , name = "issue-category-list"),
    path("issue-category/<int:id>/"  , IssueCategoryDetailApi.as_view() , name = "issue-category-detail"),
    path("issue-report-list-admin/" , IssueReportListAdminApi.as_view(), name= "issue-report-list-admin"),
    path("admin/issue-report/<int:id>/", IssueReportAdminDetailApi.as_view(), name="issue-report-admin-detail"),
    path("admin/issue-report/<int:id>/status/", IssueReportAdminStatusUpdateApi.as_view(), name="issue-report-admin-status"),
    path("create-tag/" , CreateTagApi.as_view() , name = "issue-tag-create"),
    path("tag/<int:id>/" , TagDetailApi.as_view() , name = "issue-tag-detail"),
    path("issue-detail/<int:id>/" , IssueDetailApi.as_view() , name="issue-detail-admin"),
    path("create-issue-tag/" , IssueTagAdminApi.as_view() , name= "create-issue-tag-admin"),
    path("issue-tag/<int:id>/" , IssueTagDetailApi.as_view() , name = "issue-tag-detail-admin"),
    path("issue-detail-user/<int:id>/" , issueDetailUserApi.as_view() , name= "issue-detail-user"),




]
