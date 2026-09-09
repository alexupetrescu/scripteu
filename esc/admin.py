from django.contrib import admin

from .models import Candidate, GlobalSettings, MessageTemplate, Outreach, Run, SearchProfile


@admin.register(SearchProfile)
class SearchProfileAdmin(admin.ModelAdmin):
    list_display = ['name', 'pass_id', 'template', 'per_run_cap', 'per_day_cap', 'enabled']
    list_filter = ['enabled']
    search_fields = ['name', 'pass_id']


@admin.register(MessageTemplate)
class MessageTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'subject', 'created_at']
    search_fields = ['name', 'subject', 'body']


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ['external_id', 'display_name', 'first_seen_at', 'last_seen_at']
    search_fields = ['external_id', 'display_name']


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'search_profile', 'state', 'dry_run', 'found_count',
        'contacted_count', 'skipped_count', 'failed_count', 'started_at',
    ]
    list_filter = ['state', 'dry_run', 'search_profile']
    readonly_fields = ['log', 'error']


@admin.register(Outreach)
class OutreachAdmin(admin.ModelAdmin):
    list_display = ['candidate', 'search_profile', 'status', 'sent_at', 'run']
    list_filter = ['status', 'search_profile']
    search_fields = ['candidate__external_id', 'candidate__display_name']

    def has_delete_permission(self, request, obj=None):
        # Deleting a SENT row would let that person be contacted a second time.
        # Removing the shortcut here keeps the audit trail meaningful; use the
        # shell if a deletion is genuinely intended.
        return False


@admin.register(GlobalSettings)
class GlobalSettingsAdmin(admin.ModelAdmin):
    list_display = ['paused', 'hard_daily_ceiling', 'updated_at']

    def has_add_permission(self, request):
        return not GlobalSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
