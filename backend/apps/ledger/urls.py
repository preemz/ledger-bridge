from django.urls import path

from . import views

urlpatterns = [
    path("stats/", views.StatsView.as_view(), name="stats"),
    path("jobs/", views.JobListCreateView.as_view(), name="job-list"),
    path("jobs/<uuid:job_id>/", views.JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:job_id>/audit/", views.JobAuditView.as_view(), name="job-audit"),
    path(
        "jobs/<uuid:job_id>/reconciliation/",
        views.JobReconciliationView.as_view(),
        name="job-reconciliation",
    ),
    path("jobs/<uuid:job_id>/signal/", views.JobSignalView.as_view(), name="job-signal"),
    path("breaks/<int:break_id>/resolve/", views.BreakResolveView.as_view(), name="break-resolve"),
    path("webhooks/legacy/", views.legacy_webhook, name="legacy-webhook"),
]