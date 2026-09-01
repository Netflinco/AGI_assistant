#!/usr/bin/env python3
"""Offline contracts for generalized travel recommendations and licensed media."""

from __future__ import annotations

import json

from travel_enrichment import (
    WikidataTravelClient,
    WikimediaImageClient,
    append_recommendations_to_answer,
    is_precise_venue_address,
    recommendations_from_citations,
    travel_guide_payload,
    travel_search_queries,
)


def main():
    queries = travel_search_queries("罗马", 2026)
    assert "罗马" in queries["hotels"] and "hotels" in queries["hotels"]
    assert "罗马" in queries["restaurants"] and "restaurants" in queries["restaurants"]
    hotels = recommendations_from_citations(
        "罗马",
        "hotels",
        [
            {
                "title": "Hotel Artemide Rome | Official Site",
                "url": "https://www.hotelartemide.it/",
                "snippet": "Address: Via Nazionale 22, 00184 Roma. A hotel near Repubblica metro.",
                "domain": "www.hotelartemide.it",
            },
            {
                "title": "10 Best Hotels in Rome",
                "url": "https://list.example/rome",
                "snippet": "A generic hotel list.",
                "domain": "list.example",
            },
        ],
    )
    assert len(hotels) == 1
    assert hotels[0]["name"] == "Hotel Artemide Rome"
    assert hotels[0]["address"] == "Via Nazionale 22, 00184 Roma"
    assert hotels[0]["address_verified"] is True
    assert hotels[0]["map_url"].startswith("https://www.google.com/maps/search/")
    restaurants = recommendations_from_citations(
        "京都",
        "restaurants",
        [
            {
                "title": "Gion Karyo",
                "url": "https://gion-karyo.example/",
                "snippet": "Kyoto restaurant serving seasonal cuisine. Address: 570-235 Gionmachi Minamigawa, Higashiyama Ward, Kyoto.",
                "domain": "gion-karyo.example",
            }
        ],
    )
    assert restaurants[0]["address_verified"] is True

    taiwan_hotels = recommendations_from_citations(
        "台湾",
        "hotels",
        [
            {
                "title": "Accommodation - ASCPaLM 2026",
                "url": "https://conference.example/accommodation",
                "snippet": (
                    "Address: 11F, No. 495, Guangfu South Rd., Xinyi Dist., Taipei, Taiwan "
                    "Walking Distance: 5 minutes on foot MRT: MRT Red Line - Taipei 101. "
                    "Official Website: Pacific Business Hotel Promo code: EVENT2026. "
                    "Nearby Hotels #### Humble House Taipei Official Hotels #### "
                    "Grand Hyatt Taipei Address: No. 2, SongShou Rd., Xinyi Dist., Taipei, Taiwan "
                    "Walking Distance: 8 minutes on foot MRT: Taipei City Hall."
                ),
                "domain": "conference.example",
            },
            {
                "title": "TRAVELING TO TAIWAN IN 2026: Ultimate Travel Guide",
                "url": "https://guide.example/taiwan",
                "snippet": "Taiwan hotel and restaurant travel planning guide.",
                "domain": "guide.example",
            },
            {
                "title": "Taipei",
                "url": "https://region.example/taipei",
                "snippet": "Taipei hotel neighborhoods and accommodation choices.",
                "domain": "region.example",
            },
            {
                "title": "Accommodation - APITS & TSTS 2026",
                "url": "https://conference.example/rooms",
                "snippet": (
                    "Address: No. 18, SongGao Rd., Xinyi Dist., Taipei, Taiwan Walking Distance: "
                    "15 minutes on foot Official Website: ## Howard Hotel Address: No. 160, Section 3, "
                    "Renai Road, Daan Dist., Taipei, Taiwan Walking Distance: 10-minute by Taxi. "
                    "Official Website: ## Home Hotel XinYi [...] Address: 11F, No. 495, Guangfu South "
                    "Rd., Xinyi Dist., Taipei, Taiwan Walking Distance: 5 minutes on foot Official Website: BOOK NOW"
                ),
                "domain": "conference.example",
            },
            {
                "title": "Taipei Survival Guide for First Time Visitors",
                "url": "https://guide.example/taipei-first-time",
                "snippet": (
                    "Input any requests into the special request box. InPage Hotel and Hostel: Check Rates "
                    "How to Get There: 10 minute walk from Taipei Main Station. "
                    "Address: No. 37, Section 1, Chongqing South Road, Zhongzheng District, Taipei City, Taiwan 100."
                ),
                "domain": "guide.example",
            },
        ],
    )
    assert [item["name"] for item in taiwan_hotels] == [
        "Grand Hyatt Taipei", "Howard Hotel", "InPage Hotel and Hostel",
    ]
    assert taiwan_hotels[0]["address"] == "No. 2, SongShou Rd., Xinyi Dist., Taipei, Taiwan"
    assert taiwan_hotels[1]["address"] == "No. 160, Section 3, Renai Road, Daan Dist., Taipei, Taiwan"
    assert all("Walking Distance" not in item["address"] and "MRT" not in item["address"] for item in taiwan_hotels)
    assert all(item["name"] not in {"BOOK NOW", "minute walk from Taipei Main Station"} for item in taiwan_hotels)
    assert not recommendations_from_citations(
        "台湾",
        "restaurants",
        [{
            "title": "MICHELIN Guide Taiwan 2026 Restaurants",
            "url": "https://list.example/taiwan-restaurants",
            "snippet": "A full list of the best restaurants in Taiwan without street addresses.",
            "domain": "list.example",
        }],
    )
    assert not is_precise_venue_address("台湾 · 坐标 23.7, 121.0")
    guide = travel_guide_payload("京都", 4, 2027, hotels, restaurants, [])
    answer = append_recommendations_to_answer("四日行程初稿。", guide)
    assert "住宿候选" in answer and "餐饮候选" in answer and "地图：https://" in answer

    def wikidata_fetcher(req, _timeout):
        if "wbsearchentities" in req.full_url:
            return json.dumps({"search": [{"id": "Q17", "label": "日本", "description": "island country in East Asia"}]}).encode("utf-8")
        if "wbgetentities" in req.full_url:
            return json.dumps(
                {
                    "entities": {
                        "Q17": {
                            "labels": {
                                "zh": {"language": "zh", "value": "日本"},
                                "en": {"language": "en", "value": "Japan"},
                            },
                            "claims": {
                                "P625": [
                                    {"mainsnak": {"datavalue": {"value": {"latitude": 35.68, "longitude": 139.76}}}}
                                ]
                            }
                        }
                    }
                }
            ).encode("utf-8")
        return json.dumps(
            {
                "results": {
                    "bindings": [
                        {
                            "category": {"value": "hotels"},
                            "place": {"value": "http://www.wikidata.org/entity/Q100"},
                            "placeLabel": {"value": "Example Hotel Tokyo"},
                            "placeDescription": {"value": "hotel in Tokyo, Japan"},
                            "coord": {"value": "Point(139.70 35.69)"},
                            "address": {"value": "1-2-3 Example, Tokyo"},
                        },
                        {
                            "category": {"value": "restaurants"},
                            "place": {"value": "http://www.wikidata.org/entity/Q101"},
                            "placeLabel": {"value": "Example Sushi"},
                            "placeDescription": {"value": "restaurant in Tokyo, Japan"},
                            "coord": {"value": "Point(139.71 35.68)"},
                            "address": {"value": "4-5-6 Example, Tokyo"},
                        },
                    ]
                }
            }
        ).encode("utf-8")

    places_client = WikidataTravelClient(fetcher=wikidata_fetcher)
    destination_info = places_client.resolve_destination("日本")
    assert destination_info["entity_id"] == "Q17"
    assert destination_info["aliases"] == ["Japan"]
    place_candidates = places_client.recommendations("东京", destination_info, {"hotels": [], "restaurants": []})
    assert place_candidates["hotels"][0]["address"] == "1-2-3 Example, Tokyo"
    assert place_candidates["restaurants"][0]["map_url"].startswith("https://www.google.com/maps/search/")

    requests = []

    def media_fetcher(req, _timeout):
        requests.append(req)
        if req.full_url.startswith("https://upload.wikimedia.org/"):
            return b"fake-image"
        return json.dumps(
            {
                "query": {
                    "pages": [
                        {
                            "pageid": 42,
                            "title": "File:Rome skyline.jpg",
                            "imageinfo": [
                                {
                                    "mime": "image/jpeg",
                                    "url": "https://upload.wikimedia.org/original/rome.jpg",
                                    "thumburl": "https://upload.wikimedia.org/thumb/rome.jpg",
                                    "extmetadata": {
                                        "ImageDescription": {"value": "<b>Rome skyline</b>"},
                                        "Artist": {"value": "Example Author"},
                                        "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                                    },
                                }
                            ],
                        },
                        {
                            "pageid": 43,
                            "title": "File:Unsupported.svg",
                            "imageinfo": [
                                {
                                    "mime": "image/svg+xml",
                                    "url": "https://upload.wikimedia.org/unsupported.svg",
                                    "thumburl": "https://upload.wikimedia.org/unsupported.svg",
                                    "extmetadata": {"LicenseShortName": {"value": "CC0"}},
                                }
                            ],
                        },
                    ]
                }
            }
        ).encode("utf-8")

    media = WikimediaImageClient(fetcher=media_fetcher)
    images = media.search("Rome", limit=1)
    assert len(images) == 1
    assert images[0]["author"] == "Example Author"
    assert images[0]["license"] == "CC BY-SA 4.0"
    assert images[0]["source_url"] == "https://commons.wikimedia.org/?curid=42"
    assert media.download(images[0]) == b"fake-image"
    assert len([req for req in requests if req.full_url.startswith("https://commons.wikimedia.org/")]) == 1
    print("PASS travel enrichment tests: exact venue addresses, Taiwan regression, map verification, licensed media")


if __name__ == "__main__":
    main()
