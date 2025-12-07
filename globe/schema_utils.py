import json
from typing import Any, Dict, List

from django.conf import settings
from django.urls import reverse


def _absolute_site_url(request, path_or_url: str | None) -> str | None:
    """
    Build an absolute URL for on-site links.

    - If `path_or_url` is already absolute (http/https), return as‑is.
    - If it is a relative path, use Django's `build_absolute_uri`.
    """
    if not path_or_url:
        return None

    val = str(path_or_url).strip()
    if not val:
        return None

    if val.startswith("http://") or val.startswith("https://"):
        return val

    # Treat as path on the current site
    return request.build_absolute_uri(val)


def _absolute_media_url(path_or_url: str | None) -> str | None:
    """
    Build an absolute URL for upstream media (images, videos) that are served
    from the 1Matrix / Globesuggest API domain.

    The templates currently prefix with `https://1matrix.io`, so we keep
    behaviour consistent but allow for configuration via settings.
    """
    if not path_or_url:
        return None

    val = str(path_or_url).strip()
    if not val:
        return None

    if val.startswith("http://") or val.startswith("https://"):
        return val

    base = getattr(
        settings,
        "GLOBESUGGEST_MEDIA_BASE",
        "https://1matrix.io",
    ).rstrip("/")
    if not val.startswith("/"):
        val = "/" + val
    return base + val


def _normalise_external_url(path_or_url: str | None) -> str | None:
    """
    Normalise fully‑qualified external URLs (social links, websites).

    For safety we only accept absolute http/https URLs. Bare usernames or
    invalid values are ignored so they don't pollute JSON‑LD.
    """
    if not path_or_url:
        return None
    val = str(path_or_url).strip()
    if not val:
        return None
    if val.startswith("http://") or val.startswith("https://"):
        return val
    return None


def _collect_product_images(product: Dict[str, Any]) -> List[str]:
    """
    Normalise image URLs from the product payload for JSON‑LD.
    """
    images: List[str] = []

    # Primary single image (if present)
    for key in ("image", "image_url", "thumbnail", "cover_image"):
        url = product.get(key)
        if url:
            abs_url = _absolute_media_url(url)
            if abs_url and abs_url not in images:
                images.append(abs_url)

    # From `images` collection used by the template
    raw_images = product.get("images") or []
    if isinstance(raw_images, list):
        for img in raw_images:
            if not isinstance(img, dict):
                continue
            url = img.get("image") or img.get("url")
            abs_url = _absolute_media_url(url)
            if abs_url and abs_url not in images:
                images.append(abs_url)

    # Dedicated product image slots commonly used across the template
    for key in (
        "product_image_1",
        "product_image_2",
        "product_image_3",
        "product_image_4",
        "product_image_5",
    ):
        url = product.get(key)
        if not url:
            continue
        abs_url = _absolute_media_url(url)
        if abs_url and abs_url not in images:
            images.append(abs_url)

    return images


def _build_video_objects(product: Dict[str, Any], page_url: str) -> List[Dict[str, Any]]:
    """
    Build VideoObject entries for up to three product videos / YouTube URLs.
    """
    videos: List[Dict[str, Any]] = []

    def add_video(idx: int, file_key: str, url_key: str, thumb_key: str) -> None:
        nonlocal videos
        file_url = product.get(file_key)
        embed_url = product.get(url_key)
        thumb = product.get(thumb_key)

        if not (file_url or embed_url):
            return

        content_url = _absolute_media_url(file_url) if file_url else None
        # For YouTube links or other full URLs, keep as‑is
        embed = embed_url if embed_url else content_url

        node: Dict[str, Any] = {
            "@type": "VideoObject",
            "@id": f"{page_url}#video-{idx}",
            "name": f"{product.get('product_title') or product.get('product_name') or 'Product'} – Video {idx}",
            "description": product.get("short_description") or product.get("description") or "",
            "url": embed or content_url or page_url,
        }

        if thumb:
            thumb_abs = _absolute_media_url(thumb)
            if thumb_abs:
                node["thumbnailUrl"] = thumb_abs

        if content_url:
            node["contentUrl"] = content_url

        if embed and embed.startswith(("http://", "https://")):
            node["embedUrl"] = embed

        videos.append(node)

    # Legacy / direct fields still used by the template
    add_video(1, "product_video_1", "video_url_1", "product_video_1_thumb")
    add_video(2, "product_video_2", "video_url_2", "product_video_2_thumb")
    add_video(3, "product_video_3", "video_url_3", "product_video_3_thumb")

    # Structured `videos` collection from the upstream payload
    raw_videos = product.get("videos") or []
    if isinstance(raw_videos, list):
        # Avoid creating duplicate VideoObject entries for the same URL
        seen_urls = set()
        for v in videos:
            for key in ("contentUrl", "embedUrl", "url"):
                u = v.get(key)
                if isinstance(u, str) and u:
                    seen_urls.add(u)

        for item in raw_videos:
            if not isinstance(item, dict):
                continue
            video_url = item.get("video_url")
            thumb_url = item.get("thumbnail_url")
            if not video_url and not thumb_url:
                continue

            content_url = _absolute_media_url(video_url)
            if content_url and content_url in seen_urls:
                continue

            idx = int(item.get("index") or (len(videos) + 1))
            node: Dict[str, Any] = {
                "@type": "VideoObject",
                "@id": f"{page_url}#video-{idx}",
                "name": f"{product.get('product_title') or product.get('product_name') or 'Product'} – Video {idx}",
                "description": product.get("short_description")
                or product.get("description")
                or "",
                "url": content_url or page_url,
            }

            if thumb_url:
                thumb_abs = _absolute_media_url(thumb_url)
                if thumb_abs:
                    node["thumbnailUrl"] = thumb_abs

            if content_url:
                node["contentUrl"] = content_url
                seen_urls.add(content_url)

            videos.append(node)

    return videos


def _build_faq_schema(page_url: str, product: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Build a FAQPage schema from `product.faqs_formatted` if present.
    """
    faqs = product.get("faqs_formatted") or []
    if not isinstance(faqs, list) or not faqs:
        return None

    entities: List[Dict[str, Any]] = []
    for item in faqs:
        if not isinstance(item, dict):
            continue
        q = (item.get("question") or "").strip()
        a = (item.get("answer") or "").strip()
        if not (q and a):
            continue
        entities.append(
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": a,
                },
            }
        )

    if not entities:
        return None

    return {
        "@type": "FAQPage",
        "@id": f"{page_url}#faqs",
        "mainEntity": entities,
    }


def _build_import_howto(page_url: str, product: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Build a HowTo schema for the 'How to Import' steps in the Import / Export section.
    """
    steps = product.get("how_to_import_steps") or []
    if not isinstance(steps, list) or not steps:
        return None

    howto_steps: List[Dict[str, Any]] = []
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        title = (step.get("title") or f"Step {idx}").strip()
        desc = (step.get("description") or "").strip()
        if not desc:
            continue
        howto_steps.append(
            {
                "@type": "HowToStep",
                "position": idx,
                "name": title,
                "text": desc,
            }
        )

    if not howto_steps:
        return None

    return {
        "@type": "HowTo",
        "@id": f"{page_url}#how-to-import",
        "name": f"How to import {product.get('product_title') or product.get('product_name') or 'this product'}",
        "description": "Step-by-step guidance for importing this product, based on trade documentation and logistics information.",
        "step": howto_steps,
    }


def _build_blog_posts_schema(request, page_url: str, product: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build BlogPosting entries for the first few associated blog posts, if any.
    """
    posts = product.get("blog_posts") or []
    if not isinstance(posts, list):
        return []

    items: List[Dict[str, Any]] = []
    product_id = product.get("product_id") or product.get("id")

    for idx, post in enumerate(posts[:3], start=1):
        if not isinstance(post, dict):
            continue

        title = (post.get("title") or "").strip()
        if not title:
            continue

        # Try to build the on-site blog detail URL if route is configured.
        blog_url: str | None = None
        if product_id:
            try:
                rel = reverse("blog_detail", args=[product_id, idx])
                blog_url = _absolute_site_url(request, rel)
            except Exception:
                blog_url = None

        # Fallback to product page if we cannot reverse.
        if not blog_url:
            blog_url = page_url

        image_url = None
        if post.get("image"):
            image_url = _absolute_media_url(post["image"])

        node: Dict[str, Any] = {
            "@type": "BlogPosting",
            "@id": f"{blog_url}#blog-{idx}",
            "headline": title,
            "url": blog_url,
        }
        if image_url:
            node["image"] = image_url

        summary = (post.get("summary") or post.get("description") or "").strip()
        if summary:
            node["description"] = summary

        items.append(node)

    return items


def _build_reviews_schema(page_url: str, product: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Build AggregateRating + individual Review entries if the upstream payload
    exposes review summary data under `reviews_data`.

    The template consumes `reviews_data` separately; here we only look for the
    same structure on the product dict (if present).
    """
    reviews_data = product.get("reviews_data") or {}
    if not isinstance(reviews_data, dict) or not reviews_data:
        return None

    avg = reviews_data.get("avg_rating")
    count = reviews_data.get("total_count")
    latest = reviews_data.get("latest_reviews") or []

    node: Dict[str, Any] = {
        "@type": "AggregateRating",
        "@id": f"{page_url}#aggregate-rating",
        "itemReviewed": {"@id": f"{page_url}#product"},
    }

    has_numbers = False
    try:
        if avg is not None:
            node["ratingValue"] = float(avg)
            has_numbers = True
    except (TypeError, ValueError):
        pass
    try:
        if count is not None:
            node["reviewCount"] = int(count)
            has_numbers = True
    except (TypeError, ValueError):
        pass

    # Attach up to a few individual reviews
    review_nodes: List[Dict[str, Any]] = []
    if isinstance(latest, list):
        for r in latest[:5]:
            if not isinstance(r, dict):
                continue
            content = (r.get("content") or "").strip()
            if not content:
                continue
            rating_val = r.get("rating")
            one_review: Dict[str, Any] = {
                "@type": "Review",
                "reviewBody": content,
            }
            if r.get("title"):
                one_review["name"] = r["title"]
            if r.get("name"):
                one_review["author"] = {
                    "@type": "Person",
                    "name": r["name"],
                }
            try:
                if rating_val is not None:
                    one_review["reviewRating"] = {
                        "@type": "Rating",
                        "ratingValue": float(rating_val),
                        "bestRating": 5,
                        "worstRating": 1,
                    }
                    has_numbers = True
            except (TypeError, ValueError):
                pass
            review_nodes.append(one_review)

    if review_nodes:
        node["review"] = review_nodes

    if not has_numbers and not review_nodes:
        return None

    return node


def _build_seller_organization_schema(
    product: Dict[str, Any], page_url: str
) -> Dict[str, Any] | None:
    """
    Build an Organisation node for the seller / manufacturer information
    (contact, address, GST, website, socials) and link it from Product / Offer.
    """
    user = product.get("user") or {}

    org_name = (
        (product.get("organization") or "") or (user.get("name") or "")
    ).strip()
    if not org_name:
        return None

    org_id = f"{page_url}#seller"
    org_node: Dict[str, Any] = {
        "@type": "Organization",
        "@id": org_id,
        "name": org_name,
    }

    # Basic address info
    street = (product.get("address") or "").strip()
    city = (product.get("city") or "").strip()
    country = (
        (product.get("origin_country") or "")
        or (product.get("badge_country") or "")
    ).strip()
    if street or city or country:
        addr: Dict[str, Any] = {"@type": "PostalAddress"}
        if street:
            addr["streetAddress"] = street
        if city:
            addr["addressLocality"] = city
        if country:
            addr["addressCountry"] = country
        org_node["address"] = addr

    # Contact details
    phone = (
        (product.get("contact_number") or "")
        or (user.get("phone") or "")
    ).strip()
    email = (
        (product.get("email") or "")
        or (user.get("email") or "")
    ).strip()
    if phone or email:
        contact_point: Dict[str, Any] = {
            "@type": "ContactPoint",
            "contactType": "sales",
        }
        if phone:
            contact_point["telephone"] = phone
        if email:
            contact_point["email"] = email
        org_node["contactPoint"] = contact_point

    # Website and social profiles
    same_as: List[str] = []

    website = _normalise_external_url(product.get("website_url"))
    if website:
        org_node["url"] = website

    for key in (
        "social_media_facebook",
        "social_media_twitter",
        "social_media_instagram",
    ):
        url = _normalise_external_url(product.get(key))
        if url and url not in same_as:
            same_as.append(url)

    if same_as:
        org_node["sameAs"] = same_as

    # Tax / GST details
    gst = (product.get("gst_details") or "").strip()
    if gst:
        org_node["taxID"] = gst

    owner = (product.get("owner_name") or "").strip()
    if owner:
        org_node["founder"] = owner

    # Use year of export as a lightweight founding date hint if available
    year_export = (product.get("badge_year_export") or "").strip()
    if year_export.isdigit() and len(year_export) == 4:
        org_node["foundingDate"] = f"{year_export}-01-01"

    return org_node


def build_product_schema(request, product: Dict[str, Any]) -> str:
    """
    Build a JSON‑LD payload (as a JSON string) representing all key sections
    of `product_detail.html`. This is rendered into the single
    `<script type="application/ld+json">` block in the template head.

    We intentionally keep this self‑contained so additional sections can be
    appended without touching the template again.
    """
    page_url = request.build_absolute_uri()

    # Product core node
    images = _collect_product_images(product)
    product_node: Dict[str, Any] = {
        "@type": "Product",
        "@id": f"{page_url}#product",
        "name": product.get("product_title")
        or product.get("product_name")
        or product.get("name"),
        "description": product.get("short_description")
        or product.get("description")
        or "",
        "url": page_url,
    }

    if images:
        product_node["image"] = images

    # High‑level SEO signals: keywords, origin country
    keywords: List[str] = []
    for field in ("focus_keywords", "alt_keyword_1", "alt_keyword_2"):
        val = (product.get(field) or "").strip()
        if val:
            keywords.append(val)
    if keywords:
        product_node["keywords"] = ", ".join(keywords)

    origin_country = (product.get("origin_country") or "").strip()
    if origin_country:
        product_node["countryOfOrigin"] = {
            "@type": "Country",
            "name": origin_country,
        }

    # Offers (hero price section)
    price = product.get("price")
    if price not in (None, "", 0, "0"):
        offer_node: Dict[str, Any] = {
            "@type": "Offer",
            "@id": f"{page_url}#offer",
            "url": page_url,
            "priceCurrency": product.get("currency") or "INR",
            "price": price,
            "availability": "https://schema.org/InStock",
            "itemCondition": "https://schema.org/NewCondition",
        }
        if product.get("price_unit"):
            offer_node["unitCode"] = str(product["price_unit"])

        # Dispatch time -> deliveryLeadTime
        dispatch_time = product.get("dispatch_time")
        try:
            if dispatch_time not in (None, "", 0, "0"):
                days = int(dispatch_time)
                if days > 0:
                    offer_node["deliveryLeadTime"] = {
                        "@type": "QuantitativeValue",
                        "value": days,
                        "unitCode": "DAY",
                    }
        except (TypeError, ValueError):
            pass

        if product.get("moq"):
            offer_node["eligibleQuantity"] = {
                "@type": "QuantitativeValue",
                "minValue": product.get("moq"),
            }
        product_node["offers"] = {"@id": offer_node["@id"]}
    else:
        offer_node = None

    # Variations / technical details -> additionalProperty
    additional_props: List[Dict[str, Any]] = []

    tech_details = product.get("technical_details") or []
    if isinstance(tech_details, list):
        for detail in tech_details:
            if not isinstance(detail, dict):
                continue
            name = (detail.get("name") or "").strip()
            value = (detail.get("description") or "").strip()
            if not (name and value):
                continue
            additional_props.append(
                {
                    "@type": "PropertyValue",
                    "name": name,
                    "value": value,
                }
            )

    # Core extra product metadata (HS code, uses, suitability, origin, etc.)
    simple_extra_fields = [
        ("hs_code", "HS Code"),
        ("uses", "Uses"),
        ("best_suited_for", "Best suited for"),
        ("primary_uses_title", "Primary uses title"),
        ("other_uses_title", "Other uses title"),
        ("origin_country", "Country of origin"),
        ("dispatch_time", "Dispatch time (days)"),
    ]
    for key, label in simple_extra_fields:
        raw_val = product.get(key)
        if raw_val in (None, "", 0, "0"):
            continue
        value_str = str(raw_val).strip()
        if not value_str:
            continue
        additional_props.append(
            {
                "@type": "PropertyValue",
                "name": label,
                "value": value_str,
            }
        )

    variations = product.get("variations") or []
    if isinstance(variations, list):
        for var in variations:
            if not isinstance(var, dict):
                continue
            vname = (var.get("name") or "").strip()
            if not vname:
                continue
            values = []
            for opt in var.get("values") or []:
                if isinstance(opt, dict):
                    val = opt.get("value") or ""
                else:
                    val = str(opt)
                val = val.strip()
                if not val:
                    continue
                values.append(val)
            if not values:
                continue
            additional_props.append(
                {
                    "@type": "PropertyValue",
                    "name": vname,
                    "value": ", ".join(values),
                }
            )

    # Primary / other uses lists
    list_extra_fields = [
        ("primary_uses_industries", "Primary uses – industries"),
        ("other_uses_bullets", "Other uses"),
        ("import_required_documents", "Import – required documents"),
        ("import_available_documents", "Import – available documents"),
        ("export_required_documents", "Export – required documents"),
        ("export_available_documents", "Export – available documents"),
    ]
    for key, label in list_extra_fields:
        raw_list = product.get(key) or []
        if not isinstance(raw_list, list):
            continue
        values = [str(v).strip() for v in raw_list if str(v).strip()]
        if not values:
            continue
        additional_props.append(
            {
                "@type": "PropertyValue",
                "name": label,
                "value": ", ".join(values),
            }
        )

    # Import shipping options
    shipping_options = product.get("import_shipping_options") or []
    if isinstance(shipping_options, list):
        for opt in shipping_options:
            if not isinstance(opt, dict):
                continue
            name = (opt.get("name") or "").strip()
            if not name:
                continue
            extra = (opt.get("image") or "").strip()
            value = name if not extra else f"{name} ({extra})"
            additional_props.append(
                {
                    "@type": "PropertyValue",
                    "name": "Import shipping option",
                    "value": value,
                }
            )

    # Packaging details
    packaging_details = product.get("packaging_details") or []
    if isinstance(packaging_details, list):
        for pkg in packaging_details:
            if not isinstance(pkg, dict):
                continue
            ptype = (pkg.get("type") or "").strip()
            unit = (pkg.get("unit") or "").strip()
            material = (pkg.get("material") or "").strip()
            notes = (pkg.get("notes") or "").strip()
            if not (ptype or unit or material or notes):
                continue
            name = f"Packaging – {ptype or 'Option'}"
            parts = []
            if unit:
                parts.append(unit)
            if material:
                parts.append(material)
            if notes:
                parts.append(notes)
            value = ", ".join(parts)
            additional_props.append(
                {
                    "@type": "PropertyValue",
                    "name": name,
                    "value": value,
                }
            )

    # Compliance / important notes
    compliance_items = product.get("important_compliance") or []
    if isinstance(compliance_items, list):
        for comp in compliance_items:
            if not isinstance(comp, dict):
                continue
            title = (comp.get("title") or "").strip()
            desc = (comp.get("description") or "").strip()
            if not desc:
                continue
            name = title or "Important compliance"
            additional_props.append(
                {
                    "@type": "PropertyValue",
                    "name": name,
                    "value": desc,
                }
            )

    # Export / logistics badge fields -> additionalProperty
    badge_fields = [
        ("badge_lead_time", "Lead time (days)"),
        ("badge_port", "Port of loading"),
        ("badge_year_export", "Year of export start"),
        ("badge_country", "Export country"),
        ("badge_region", "Export region"),
    ]
    for key, label in badge_fields:
        raw_val = product.get(key)
        if raw_val in (None, "", 0, "0"):
            continue
        value_str = str(raw_val).strip()
        if not value_str:
            continue
        additional_props.append(
            {
                "@type": "PropertyValue",
                "name": label,
                "value": value_str,
            }
        )

    if additional_props:
        product_node["additionalProperty"] = additional_props

    # Certifications mapped as awards
    certs = product.get("certifications") or []
    if isinstance(certs, list):
        awards: List[str] = []
        for c in certs:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if name and name not in awards:
                awards.append(name)
        if awards:
            product_node["award"] = awards if len(awards) > 1 else awards[0]

    # Assemble @graph
    graph: List[Dict[str, Any]] = [product_node]

    if offer_node:
        graph.append(offer_node)

    # Seller / organisation node (brand, contact, GST, socials)
    org_schema = _build_seller_organization_schema(product, page_url)
    if org_schema:
        graph.append(org_schema)
        product_node.setdefault("brand", {"@id": org_schema["@id"]})
        product_node.setdefault("manufacturer", {"@id": org_schema["@id"]})
        if offer_node:
            offer_node["seller"] = {"@id": org_schema["@id"]}

    # AggregateRating / Review (reviews section)
    reviews_schema = _build_reviews_schema(page_url, product)
    if reviews_schema:
        graph.append(reviews_schema)
        # Link from Product if rating present
        product_node["aggregateRating"] = {"@id": reviews_schema["@id"]}

    # Videos section
    video_nodes = _build_video_objects(product, page_url)
    graph.extend(video_nodes)

    # FAQ section
    faq_schema = _build_faq_schema(page_url, product)
    if faq_schema:
        graph.append(faq_schema)

    # Import/Export How‑to
    howto_schema = _build_import_howto(page_url, product)
    if howto_schema:
        graph.append(howto_schema)

    # Blog posts
    blog_nodes = _build_blog_posts_schema(request, page_url, product)
    graph.extend(blog_nodes)

    # Breadcrumbs: Home > Product
    breadcrumbs = {
        "@type": "BreadcrumbList",
        "@id": f"{page_url}#breadcrumbs",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": 1,
                "item": {
                    "@id": _absolute_site_url(request, "/"),
                    "name": "Home",
                },
            },
            {
                "@type": "ListItem",
                "position": 2,
                "item": {
                    "@id": page_url,
                    "name": product_node.get("name"),
                },
            },
        ],
    }
    graph.append(breadcrumbs)

    root = {
        "@context": "https://schema.org",
        "@graph": graph,
    }

    return json.dumps(root, ensure_ascii=False)


