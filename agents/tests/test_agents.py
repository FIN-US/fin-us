from finus_agents import agents


def test_supervisor_rules_are_one_per_line():
    """supervisor 규칙은 한 줄에 하나다 — 줄이 붙으면 뒤 규칙이 앞 문장의 꼬리로 읽힌다.

    PR #388 리뷰: 첫 규칙 끝에 개행이 없어 "…추론하세요 - 가격, 거래량, …"으로 trading 라우팅
    규칙(#380에서 주문 요청 안내를 더한 규칙)이 앞 규칙에 붙어 있었다.

    뮤테이션: 첫 규칙 끝의 ``.\\n``을 공백으로 되돌리면 red.
    """
    branches = [
        agents.SupervisorBranch(name="trading_agent", function_name="trading_branch_agent", description="조회"),
    ]
    prompt = agents._supervisor_system_prompt(branches)

    rules = prompt.split("규칙:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert rules and all(rule.startswith("- ") for rule in rules)
    assert [rule for rule in rules if " - " in rule] == []
    assert any(rule.startswith("- 가격, 거래량") and "trading_agent" in rule for rule in rules)
