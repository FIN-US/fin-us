/**
 * fetchAllPaged 가드 테스트
 *
 * index.js의 두 루프(fetchAllDailyOrderCcld, fetchAllBalanceRlzPl)는 MCP 서버 진입점에
 * 있어 import 시점에 StdioServerTransport를 연결하므로 유닛 레벨에서 직접 테스트할 수 없다.
 * 두 루프가 위임하는 fetchAllPaged를 동일한 파라미터(maxPages: 50, timeBudgetMs: 90_000)로
 * 직접 테스트해 네 가지 가드의 실효성을 확인한다.
 *
 * 시간 예산 90초 근거: 호출자 상한 120초(diary_agent.yml timeout_sec)에서
 * 요청당 타임아웃 8초 + 기동 오버헤드 2초를 빼면 ~110초 헤드룸.
 * 90 + 8 + 2 = 100 < 120이므로 상한 안에 안전하게 들어온다.
 * 실제 페이지당 지연 측정치가 없어 이 값은 검증 불가 — 소스 주석과 PR에 명시.
 */

import assert from "node:assert/strict";
import test from "node:test";
import { fetchAllPaged } from "../balance.js";

// 두 루프가 공유하는 파라미터
const MAX_PAGES = 50;
const TIME_BUDGET_MS = 90_000;

// ─── 가드 1: 커서 trim ────────────────────────────────────────────────────────

test("fetchAllPaged: KIS가 공백 패딩 커서를 반환해도 다음 요청에는 trim된 값을 전송한다", async () => {
  const calls = [];
  let callCount = 0;
  const fetchPage = async ({ ctxAreaFk100, ctxAreaNk100, trCont }) => {
    calls.push({ ctxAreaFk100, ctxAreaNk100, trCont });
    callCount += 1;
    if (callCount === 1) {
      return {
        body: {
          output1: [{ pdno: "005930" }],
          output2: [],
          // KIS 고정폭 반환: 패딩된 커서
          ctx_area_fk100: "  FK1  ",
          ctx_area_nk100: "  NK1  ",
        },
        trCont: "F",
      };
    }
    // 2회 호출에서 커서가 없으면 no_cursor로 멈춤 — trim 동작을 확인한 뒤 루프 종료
    return {
      body: {
        output1: [{ pdno: "035420" }],
        output2: [],
        ctx_area_fk100: "",
        ctx_area_nk100: "",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
  });

  assert.equal(callCount, 2);
  // 2회 호출 시 전송된 커서는 trim된 값이어야 한다
  assert.deepEqual(calls[1], { ctxAreaFk100: "FK1", ctxAreaNk100: "NK1", trCont: "N" });
  assert.equal(result.truncated, "no_cursor");
});

// ─── 가드 2: no_cursor 가드 ──────────────────────────────────────────────────

test("fetchAllPaged: 공백 패딩 커서 + 연속 tr_cont에서 50회가 아닌 2회 이내에 멈춘다 (no_cursor)", async () => {
  // 수용 기준: "공백 패딩 커서 + 연속 tr_cont에서 두 루프가 2회 이내에 멈출 것 (지금은 50회)"
  let callCount = 0;
  const fetchPage = async () => {
    callCount += 1;
    return {
      body: {
        output1: [{ pdno: String(callCount).padStart(6, "0") }],
        output2: [],
        // KIS가 공백 커서를 반환 — truthy라 trim 없이는 가드를 통과함
        ctx_area_fk100: "   ",
        ctx_area_nk100: "   ",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
  });

  // 1회: 첫 페이지 조회 → 공백 커서 수신
  // 2회: 공백 커서 trim → 비어 있어 no_cursor 가드에서 멈춤
  // 따라서 총 호출 횟수는 2, max_pages(50)에는 도달하지 않음
  assert.ok(callCount <= 2, `호출 횟수 ${callCount}회 — 50회 미만이어야 함`);
  assert.equal(result.truncated, "no_cursor");
  assert.notEqual(result.truncated, null);
});

test("fetchAllPaged: tr_cont 연속 신호가 와도 커서가 완전히 없을 때 no_cursor로 멈춘다", async () => {
  let callCount = 0;
  const fetchPage = async () => {
    callCount += 1;
    if (callCount === 1) {
      return {
        body: {
          output1: [{ pdno: "005930" }],
          output2: [{ tot: "1" }],
          ctx_area_fk100: "FK1",
          ctx_area_nk100: "NK1",
        },
        trCont: "F",
      };
    }
    // 두 번째 페이지부터 커서 없음
    return {
      body: {
        output1: [{ pdno: String(callCount).padStart(6, "0") }],
        output2: [],
        ctx_area_fk100: "",
        ctx_area_nk100: "",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
  });

  assert.equal(callCount, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "no_cursor");
});

// ─── 가드 3: 커서 반복 감지 ──────────────────────────────────────────────────

test("fetchAllPaged: KIS가 같은 커서를 반복 반환하면 2회 이내에 멈춘다 (repeated_cursor)", async () => {
  // 수용 기준: "공백 패딩 커서 + 연속 tr_cont에서 두 루프가 2회 이내에 멈출 것"
  let callCount = 0;
  const fetchPage = async () => {
    callCount += 1;
    return {
      body: {
        output1: [{ pdno: callCount === 1 ? "000000" : "000001" }],
        output2: callCount === 1 ? [{ tot: "1" }] : [],
        // 매 페이지 동일한 커서 반환 → 반복 감지
        ctx_area_fk100: "FK-SAME",
        ctx_area_nk100: "NK-SAME",
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
  });

  assert.equal(callCount, 2);
  assert.equal(result.pages, 2);
  assert.equal(result.truncated, "repeated_cursor");
  // 방금 받은 2번째 페이지는 이번에 처음 그 커서로 조회한 신규 데이터이므로 rows에 남아야 한다
  assert.deepEqual(result.rows.map((r) => r.pdno), ["000000", "000001"]);
});

test("fetchAllPaged: 주기 2 커서 순환(A,B,A)을 감지해 50회가 아닌 3회에서 멈춘다", async () => {
  const cursors = ["A", "B", "A"];
  let callCount = 0;
  const fetchPage = async () => {
    callCount += 1;
    const cursor = cursors[callCount - 1] ?? "X";
    return {
      body: {
        output1: [{ pdno: String(callCount).padStart(6, "0") }],
        output2: callCount === 1 ? [{ tot: "1" }] : [],
        ctx_area_fk100: `FK-${cursor}`,
        ctx_area_nk100: cursor,
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
  });

  assert.equal(callCount, 3);
  assert.equal(result.truncated, "repeated_cursor");
});

// ─── 가드 4: 시간 예산 ───────────────────────────────────────────────────────

test("fetchAllPaged: 시간 예산 초과 시 max_pages(50)보다 먼저 멈추고 부분 결과를 반환한다", async () => {
  // 수용 기준: "시간 예산 만료 시 부분 결과와 잘림 표시를 반환할 것"
  let callCount = 0;
  let clock = 0;
  const fetchPage = async () => {
    callCount += 1;
    clock += 50_000; // 페이지당 50초 시뮬레이션 → 2번째 호출 후 clock=100_000 > 90_000
    return {
      body: {
        output1: [{ pdno: String(callCount).padStart(6, "0") }],
        output2: [],
        // 시간 예산 경로를 타려면 커서가 있어야 한다 (없으면 no_cursor가 먼저 걸림)
        ctx_area_fk100: `FK${callCount}`,
        ctx_area_nk100: `NK${callCount}`,
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
    now: () => clock,
  });

  // 1회: clock=50_000 (아직 90_000 미만 → 계속)
  // 2회: clock=100_000 > 90_000 → 3회 진입 시 time_budget 가드 발동
  assert.equal(callCount, 2);
  assert.equal(result.truncated, "time_budget");
  assert.equal(result.rows.length, 2);
  assert.ok(result.pages < MAX_PAGES, `pages(${result.pages})가 MAX_PAGES(${MAX_PAGES})보다 작아야 함`);
});

// ─── 수용 기준: rows.length === 0이어도 잘림 표시 ────────────────────────────

test("fetchAllPaged: rows가 없어도 truncated 값이 반환돼 호출자가 잘림 안내를 표시할 수 있다", async () => {
  // 수용 기준: "잘림 표시가 rows.length === 0인 경우에도 나올 것"
  // index.js의 formatDailyOrderCcldReport / balance-rlz-pl-report.js의
  // formatBalanceRlzPlReport 둘 다 truncated를 받아 안내를 추가해야 한다.
  let callCount = 0;
  let clock = 0;
  const fetchPage = async () => {
    callCount += 1;
    clock += 50_000;
    return {
      body: {
        output1: [], // 행 없음
        output2: [],
        ctx_area_fk100: `FK${callCount}`,
        ctx_area_nk100: `NK${callCount}`,
      },
      trCont: "F",
    };
  };

  const result = await fetchAllPaged(fetchPage, {
    maxPages: MAX_PAGES,
    timeBudgetMs: TIME_BUDGET_MS,
    now: () => clock,
  });

  assert.equal(result.rows.length, 0);
  assert.notEqual(result.truncated, null);
  assert.equal(result.truncated, "time_budget");
});
