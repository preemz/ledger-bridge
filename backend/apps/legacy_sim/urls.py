from django.urls import path

from . import views

urlpatterns = [
    path("v1/accounts", views.accounts, name="legacy-accounts"),
    path("v1/journal-entries", views.journal_entries, name="legacy-journal-entries"),
    path("v1/trial-balance", views.trial_balance_view, name="legacy-trial-balance"),
    path("v1/exports", views.request_export, name="legacy-request-export"),
]
