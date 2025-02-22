import pytest
from dlt.common import jsonpath

from dlt.sources.helpers.rest_client.detector import (
    find_records,
    find_next_page_path,
    single_entity_path,
)


TEST_RESPONSES = [
    {
        "response": {
            "data": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}],
            "pagination": {"offset": 0, "limit": 2, "total": 100},
        },
        "expected": {
            "type": "offset_limit",
            "records_path": "data",
        },
    },
    {
        "response": {
            "items": [
                {"id": 11, "title": "Page Item 1"},
                {"id": 12, "title": "Page Item 2"},
            ],
            "page_info": {"current_page": 1, "items_per_page": 2, "total_pages": 50},
        },
        "expected": {
            "type": "page_number",
            "records_path": "items",
        },
    },
    {
        "response": {
            "products": [
                {"id": 101, "name": "Product 1"},
                {"id": 102, "name": "Product 2"},
            ],
            "next_cursor": "eyJpZCI6MTAyfQ==",
        },
        "expected": {
            "type": "cursor",
            "records_path": "products",
            "next_path": ["next_cursor"],
        },
    },
    {
        "response": {
            "results": [
                {"id": 201, "description": "Result 1"},
                {"id": 202, "description": "Result 2"},
            ],
            "cursors": {"next": "NjM=", "previous": "MTk="},
        },
        "expected": {
            "type": "cursor",
            "records_path": "results",
            "next_path": ["cursors", "next"],
        },
    },
    {
        "response": {
            "entries": [{"id": 31, "value": "Entry 1"}, {"id": 32, "value": "Entry 2"}],
            "next_id": 33,
            "limit": 2,
        },
        "expected": {
            "type": "cursor",
            "records_path": "entries",
            "next_path": ["next_id"],
        },
    },
    {
        "response": {
            "comments": [
                {"id": 51, "text": "Comment 1"},
                {"id": 52, "text": "Comment 2"},
            ],
            "page_number": 3,
            "total_pages": 15,
        },
        "expected": {
            "type": "page_number",
            "records_path": "comments",
        },
    },
    {
        "response": {
            "count": 1023,
            "next": "https://api.example.org/accounts/?page=5",
            "previous": "https://api.example.org/accounts/?page=3",
            "results": [{"id": 1, "name": "Account 1"}, {"id": 2, "name": "Account 2"}],
        },
        "expected": {
            "type": "json_link",
            "records_path": "results",
            "next_path": ["next"],
        },
    },
    {
        "response": {
            "_embedded": {"items": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}]},
            "_links": {
                "first": {"href": "http://api.example.com/items?page=0&size=2"},
                "self": {"href": "http://api.example.com/items?page=1&size=2"},
                "next": {"href": "http://api.example.com/items?page=2&size=2"},
                "last": {"href": "http://api.example.com/items?page=50&size=2"},
            },
            "page": {"size": 2, "totalElements": 100, "totalPages": 50, "number": 1},
        },
        "expected": {
            "type": "json_link",
            "records_path": "_embedded.items",
            "next_path": ["_links", "next", "href"],
        },
    },
    {
        "response": {
            "items": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}],
            "meta": {
                "currentPage": 1,
                "pageSize": 2,
                "totalPages": 50,
                "totalItems": 100,
            },
            "links": {
                "firstPage": "/items?page=1&limit=2",
                "previousPage": "/items?page=0&limit=2",
                "nextPage": "/items?page=2&limit=2",
                "lastPage": "/items?page=50&limit=2",
            },
        },
        "expected": {
            "type": "json_link",
            "records_path": "items",
            "next_path": ["links", "nextPage"],
        },
    },
    {
        "response": {
            "data": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}],
            "pagination": {
                "currentPage": 1,
                "pageSize": 2,
                "totalPages": 5,
                "totalItems": 10,
            },
        },
        "expected": {
            "type": "page_number",
            "records_path": "data",
        },
    },
    {
        "response": {
            "items": [{"id": 1, "title": "Item 1"}, {"id": 2, "title": "Item 2"}],
            "pagination": {"page": 1, "perPage": 2, "total": 10, "totalPages": 5},
        },
        "expected": {
            "type": "page_number",
            "records_path": "items",
        },
    },
    {
        "response": {
            "data": [
                {"id": 1, "description": "Item 1"},
                {"id": 2, "description": "Item 2"},
            ],
            "meta": {
                "currentPage": 1,
                "itemsPerPage": 2,
                "totalItems": 10,
                "totalPages": 5,
            },
            "links": {
                "first": "/api/items?page=1",
                "previous": None,
                "next": "/api/items?page=2",
                "last": "/api/items?page=5",
            },
        },
        "expected": {
            "type": "json_link",
            "records_path": "data",
            "next_path": ["links", "next"],
        },
    },
    {
        "response": {
            "page": 2,
            "per_page": 10,
            "total": 100,
            "pages": 10,
            "data": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}],
        },
        "expected": {
            "type": "page_number",
            "records_path": "data",
        },
    },
    {
        "response": {
            "currentPage": 1,
            "pageSize": 10,
            "totalPages": 5,
            "totalRecords": 50,
            "items": [{"id": 1, "name": "Item 1"}, {"id": 2, "name": "Item 2"}],
        },
        "expected": {
            "type": "page_number",
            "records_path": "items",
        },
    },
    {
        "response": {
            "articles": [
                {"id": 21, "headline": "Article 1"},
                {"id": 22, "headline": "Article 2"},
            ],
            "paging": {"current": 3, "size": 2, "total": 60},
        },
        "expected": {
            "type": "page_number",
            "records_path": "articles",
        },
    },
    {
        "response": {
            "feed": [
                {"id": 41, "content": "Feed Content 1"},
                {"id": 42, "content": "Feed Content 2"},
            ],
            "offset": 40,
            "limit": 2,
            "total_count": 200,
        },
        "expected": {
            "type": "offset_limit",
            "records_path": "feed",
        },
    },
    {
        "response": {
            "query_results": [
                {"id": 81, "snippet": "Result Snippet 1"},
                {"id": 82, "snippet": "Result Snippet 2"},
            ],
            "page_details": {
                "number": 1,
                "size": 2,
                "total_elements": 50,
                "total_pages": 25,
            },
        },
        "expected": {
            "type": "page_number",
            "records_path": "query_results",
        },
    },
    {
        "response": {
            "posts": [
                {"id": 91, "title": "Blog Post 1"},
                {"id": 92, "title": "Blog Post 2"},
            ],
            "pagination_details": {
                "current_page": 4,
                "posts_per_page": 2,
                "total_posts": 100,
                "total_pages": 50,
            },
        },
        "expected": {
            "type": "page_number",
            "records_path": "posts",
        },
    },
    {
        "response": {
            "catalog": [
                {"id": 101, "product_name": "Product A"},
                {"id": 102, "product_name": "Product B"},
            ],
            "page_metadata": {
                "index": 1,
                "size": 2,
                "total_items": 20,
                "total_pages": 10,
            },
        },
        "expected": {
            "type": "page_number",
            "records_path": "catalog",
        },
    },
]


@pytest.mark.parametrize("test_case", TEST_RESPONSES)
def test_find_records(test_case):
    response = test_case["response"]
    expected = test_case["expected"]["records_path"]
    r = find_records(response)
    # all of them look fine mostly because those are simple cases...
    # case 7 fails because it is nested but in fact we select a right response
    # assert r is create_nested_accessor(expected)(response)
    assert r == jsonpath.find_values(expected, response)[0]


@pytest.mark.parametrize("test_case", TEST_RESPONSES)
def test_find_next_page_key(test_case):
    response = test_case["response"]
    expected = test_case.get("expected").get("next_path", None)  # Some cases may not have next_path
    assert find_next_page_path(response) == expected


@pytest.mark.skip
@pytest.mark.parametrize(
    "path",
    [
        "/users/{user_id}",
        "/api/v1/products/{product_id}/",
        "/api/v1/products/{product_id}//",
        "/api/v1/products/{product_id}?param1=value1",
        "/api/v1/products/{product_id}#section",
        "/api/v1/products/{product_id}/#section",
        "/users/{user_id}/posts/{post_id}",
        "/users/{user_id}/posts/{post_id}/comments/{comment_id}",
        "{entity}",
        "/{entity}",
        "/{user_123}",
    ],
)
def test_single_entity_path_valid(path):
    assert single_entity_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/users/user_id",
        "/api/v1/products/product_id/",
        "/users/{user_id}/details",
        "/",
        "/{}",
        "/users/{123}",
        "/users/{user-id}",
        "/users/{user id}",
        "/users/{user_id}/{",  # Invalid ending
    ],
)
def test_single_entity_path_invalid(path):
    assert single_entity_path(path) is False
