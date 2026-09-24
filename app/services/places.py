"""Google Places API (New) - Text Search. https://developers.google.com/maps/documentation/places/web-service/text-search"""
from dataclasses import dataclass

import httpx

from app.services.errors import ProviderError

URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.googleMapsUri", "places.primaryTypeDisplayName", "places.businessStatus", "nextPageToken",
])


@dataclass
class Place:
    place_id: str
    name: str
    address: str
    phone: str
    website: str
    rating: float | None
    reviews_count: int | None
    maps_url: str
    category: str
    business_status: str


def _parse(p: dict) -> Place:
    return Place(
        place_id=p.get("id", ""),
        name=(p.get("displayName") or {}).get("text", ""),
        address=p.get("formattedAddress", ""),
        phone=p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
        website=p.get("websiteUri", ""),
        rating=p.get("rating"),
        reviews_count=p.get("userRatingCount"),
        maps_url=p.get("googleMapsUri", ""),
        category=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
        business_status=p.get("businessStatus", ""),
    )


def text_search(api_key: str, query: str, *, region: str = "bd", language: str = "en",
                page_token: str | None = None, client: httpx.Client | None = None) -> tuple[list[Place], str | None]:
    body = {"textQuery": query, "pageSize": 20, "regionCode": region, "languageCode": language}
    if page_token:
        body["pageToken"] = page_token
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    c = client or httpx.Client(timeout=30)
    try:
        r = c.post(URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ProviderError(f"Places request failed: {exc}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise ProviderError(f"Places API {r.status_code}: {r.text[:300]}")
    data = r.json()
    places = [_parse(p) for p in data.get("places", [])]
    return places, data.get("nextPageToken")
