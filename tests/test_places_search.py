import json

import httpx
import pytest
import respx

from app.services import places, search
from app.services.errors import ProviderError


@respx.mock
def test_places_text_search_request_and_parsing():
    route = respx.post("https://places.googleapis.com/v1/places:searchText").mock(return_value=httpx.Response(200, json={
        "places": [{"id": "abc", "displayName": {"text": "Square Hospital"}, "formattedAddress": "Panthapath, Dhaka",
                    "internationalPhoneNumber": "+880 2-8144400", "websiteUri": "https://www.squarehospital.com/",
                    "rating": 4.1, "userRatingCount": 900, "googleMapsUri": "https://maps.google.com/?cid=1",
                    "primaryTypeDisplayName": {"text": "Hospital"}, "businessStatus": "OPERATIONAL"}],
        "nextPageToken": "tok2"}))
    res, nxt = places.text_search("KEY", "hospital in Dhaka, Bangladesh")
    assert nxt == "tok2" and res[0].name == "Square Hospital" and res[0].website.startswith("https://www.square")
    req = route.calls[0].request
    assert req.headers["X-Goog-Api-Key"] == "KEY" and "places.websiteUri" in req.headers["X-Goog-FieldMask"]
    assert json.loads(req.content)["regionCode"] == "bd"


@respx.mock
def test_places_error_is_provider_error():
    respx.post("https://places.googleapis.com/v1/places:searchText").mock(return_value=httpx.Response(403, text="denied"))
    with pytest.raises(ProviderError):
        places.text_search("KEY", "x")


@respx.mock
def test_serper_and_brave_parsing():
    respx.post("https://google.serper.dev/search").mock(return_value=httpx.Response(200, json={
        "organic": [{"title": "T", "link": "https://a.com", "snippet": "S"}]}))
    respx.get(url__startswith="https://api.search.brave.com/").mock(return_value=httpx.Response(200, json={
        "web": {"results": [{"title": "B", "url": "https://b.com", "description": "D"}]}}))
    assert search.search("serper", "k", "q")[0].url == "https://a.com"
    assert search.search("brave", "k", "q")[0].snippet == "D"
