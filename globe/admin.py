from django.contrib import admin
from django import forms
from django.db import models

from .models import *

admin.site.register(AnalyticsSession)
admin.site.register(AnalyticsEvent)

class TinyMCEPolicyWidget(forms.Textarea):
    """
    Lightweight TinyMCE widget for policy content using the official CDN.

    This avoids adding extra dependencies while still giving a rich text
    editing experience inside the Django admin.
    """

    def __init__(self, *args, **kwargs):
        attrs = kwargs.setdefault("attrs", {})
        css_classes = attrs.get("class", "")
        # Ensure we keep any existing classes and add our selector class.
        attrs["class"] = (css_classes + " vLargeTextField tinymce-policy").strip()
        super().__init__(*args, **kwargs)

    class Media:
        js = (
            # TinyMCE open‑source build via CDNJS (no API key required)
            "https://cdnjs.cloudflare.com/ajax/libs/tinymce/6.8.3/tinymce.min.js",
            # Local initialisation script to bind TinyMCE to the textarea.
            "js/policy_tinymce_init.js",
        )


class BasePolicyAdmin(admin.ModelAdmin):
    """
    Shared admin configuration for all policy models.

    - Enforces a single-instance constraint on the admin add view.
    - Uses TinyMCE for rich text editing of the policy content.
    - Exposes created/updated timestamps as read-only fields.
    """

    list_display = ("name", "effective_date", "updated_at")
    readonly_fields = ("created_at", "updated_at")

    formfield_overrides = {
        models.TextField: {
            "widget": TinyMCEPolicyWidget,
        }
    }

    def has_add_permission(self, request):
        """
        Allow creating at most one instance for each policy model.
        """
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(PrivacyPolicy)
class PrivacyPolicyAdmin(BasePolicyAdmin):
    pass


@admin.register(TermsOfService)
class TermsOfServiceAdmin(BasePolicyAdmin):
    pass


@admin.register(CookiesPolicy)
class CookiesPolicyAdmin(BasePolicyAdmin):
    pass


@admin.register(ContactEnquiry)
class ContactEnquiryAdmin(admin.ModelAdmin):
    """
    Admin configuration for viewing and searching contact enquiries.
    """

    list_display = ("name", "email", "phone", "submitted_at")
    list_filter = ("submitted_at",)
    search_fields = ("name", "email", "phone", "message")
    readonly_fields = ("submitted_at", "source_ip", "user_agent")
    ordering = ("-submitted_at",)


@admin.register(ContactRecipientEmail)
class ContactRecipientEmailAdmin(admin.ModelAdmin):
    """
    Admin configuration for managing recipient email addresses.
    """

    list_display = ("email", "name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("email", "name")
    ordering = ("email",)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    """
    Admin configuration for product‑specific leads coming from product_detail.html.
    """

    list_display = (
        "product_name",
        "product_id",
        "mobile",
        "email",
        "quantity",
        "frequency",
        "source",
        "is_draft",
        "created_at",
    )
    list_filter = ("source", "is_draft", "created_at")
    search_fields = (
        "product_name",
        "product_id",
        "product_slug",
        "mobile",
        "email",
        "session_id",
    )
    readonly_fields = ("created_at", "updated_at", "submitted_at", "source_ip", "user_agent")
    ordering = ("-created_at",)



class ServiceTabPointInline(admin.TabularInline):
    model = ServiceTabPoint
    extra = 5
    max_num = 5
    fields = ("order", "icon", "title", "description")


@admin.register(ServiceTab)
class ServiceTabAdmin(admin.ModelAdmin):
    list_display = ("tab_name", "order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("tab_name",)
    ordering = ("order", "tab_name")
    inlines = [ServiceTabPointInline]