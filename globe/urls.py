from django.urls import path
from django.conf import settings

from .views import (
    home,
    api_search_suggest,
    contact_api,
    product_detail,
    blog_detail,
    privacy_policy,
    terms_of_service,
    cookies_policy,
    about_view,
    contact_view,
    enquiry_draft,
    enquiry_submit,
    test_api,
)

urlpatterns = [
    path("", home, name="home"),
    path("api/search/suggest/", api_search_suggest, name="api_search_suggest"),
    path("api/contact/", contact_api, name="api_contact"),
    # Product‑specific enquiry & draft endpoints used by product_detail.html
    path("api/enquiry/draft/", enquiry_draft, name="api_enquiry_draft"),
    path("api/enquiry/submit/", enquiry_submit, name="api_enquiry_submit"),
    # Use the product UUID (or stable ID) in the URL so we can call the
    # upstream detail API as /globesuggest/api/products/{uuid}
    path("product/<slug:product_id>/", product_detail, name="product_detail"),
    # Blog detail for a specific product blog entry
    path(
        "product/<slug:product_id>/blog/<int:blog_index>/",
        blog_detail,
        name="blog_detail",
    ),
    # Legal / policy pages
    path("privacy-policy/", privacy_policy, name="privacy_policy"),
    path("terms-of-service/", terms_of_service, name="terms_of_service"),
    path("cookies-policy/", cookies_policy, name="cookies_policy"),
    path("about/", about_view, name="about"),
    path("contact/", contact_view, name="contact_view"),
]

if settings.DEBUG:
    urlpatterns += [
        path("test/", test_api, name="test_api"),
    ]