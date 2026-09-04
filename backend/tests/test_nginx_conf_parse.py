"""`nginx_conf.py`의 최소 파서가 이 설정의 함정들을 실제로 견디는지 본다.

파서가 조용히 틀리면 그 위에 선 검사들(`test_nginx_rate_limit.py`·
`test_nginx_api_key_cookie.py`)이 **초록불로** 틀린다 — 보호가 없는데 있는 것으로 읽거나,
있는데 없는 것으로 읽는다. 그래서 파서 자체를 따로 고정한다.

여기 있는 입력은 모두 `frontend/nginx.conf.template`에 실제로 들어 있는 모양에서 왔다.
"""

from backend.tests.nginx_conf import blocks, collect_locations, directives, strip_comments


def test_strip_comments_keeps_a_hash_inside_a_quoted_string():
    """따옴표 안의 `#`을 주석으로 오해하면 그 뒤가 통째로 사라진다."""

    assert strip_comments('return 200 "a#b"; # 꼬리') == 'return 200 "a#b"; '


def test_blocks_ignores_braces_inside_a_quoted_string():
    """429 본문의 `{"detail":...}`가 블록을 조기에 닫지 않아야 한다."""

    parsed = blocks('location @x { return 429 \'{"detail":"d"}\'; } server { listen 80; }')

    assert [header for header, _ in parsed] == ["location @x", "server"]
    assert 'return 429 \'{"detail":"d"}\'' in directives(parsed[0][1])


def test_directives_excludes_the_bodies_of_nested_blocks():
    """중첩 블록 안의 지시어를 바깥 것으로 세면 없는 보호가 있는 것으로 읽힌다."""

    body = " listen 80; location /a { limit_req zone=z; } server_tokens off; "

    assert directives(body) == ["listen 80", "server_tokens off"]


def test_collect_locations_reaches_a_nested_location():
    """중첩 location을 놓치면 "프록시 경로를 전부 훑는다"가 거짓이 된다(PR #349 리뷰).

    nginx는 location 안의 location을 허용하므로, 최상위만 보는 구현에서는 중첩 블록에
    넣은 `proxy_pass`가 제한 없이 통과한다.
    """

    body = "location /outer { proxy_pass http://a; location /outer/inner { proxy_pass http://b; } }"

    found = collect_locations(body, {})

    assert set(found) == {"/outer", "/outer/inner"}
    assert found["/outer"] == ["proxy_pass http://a"]

