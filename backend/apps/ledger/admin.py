from django.contrib import admin

from .models import (
    Account,
    AuditEvent,
    JournalEntry,
    JournalLine,
    MigrationJob,
    MigrationPhase,
    ReconciliationBreak,
    ReconciliationRun,
)


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0
    readonly_fields = ("created_at", "updated_at")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "type", "normal_balance", "currency", "is_active")
    list_filter = ("type", "is_active", "currency")
    search_fields = ("code", "name", "external_ref")


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("external_ref", "entry_date", "status", "memo")
    list_filter = ("status", "source_system")
    search_fields = ("external_ref", "memo")
    inlines = [JournalLineInline]


@admin.register(MigrationJob)
class MigrationJobAdmin(admin.ModelAdmin):
    list_display = ("customer_reference", "status", "source_system", "as_of_date", "created_at")
    list_filter = ("status", "source_system", "executor")
    readonly_fields = ("counters", "temporal_workflow_id", "temporal_run_id")


@admin.register(MigrationPhase)
class MigrationPhaseAdmin(admin.ModelAdmin):
    list_display = ("job", "name", "attempt", "chunk", "status", "started_at", "finished_at")
    list_filter = ("name", "status")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_type", "actor", "message")
    list_filter = ("event_type", "actor")

    def has_change_permission(self, request, obj=None):
        # The table refuses updates at the database level, so the admin must too.
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(admin.ModelAdmin):
    list_display = ("job", "as_of_date", "difference", "loaded_entry_count", "created_at")


@admin.register(ReconciliationBreak)
class ReconciliationBreakAdmin(admin.ModelAdmin):
    list_display = ("account_code", "classification", "severity", "variance", "resolved")
    list_filter = ("classification", "severity", "resolved")
    search_fields = ("account_code", "account_name")