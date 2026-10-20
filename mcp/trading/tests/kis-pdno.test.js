import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  dailyCcldPdnoParam,
  ETN_SECURITY_GROUP,
  etnResponsePdno,
  kisResponsePdnoMatches,
} from "../kis-pdno.js";
import { getDefaultStocks, resolveStock } from "../stock-master.js";

// 이슈 #433: 공유 판정표. backend/tests/test_stock_code.py가 같은 파일을 읽는다.
const POLICY = JSON.parse(
  readFileSync(new URL("fixtures/kis_response_pdno_policy.json", import.meta.url), "utf-8"),
);

for (const { master_code: code, scrt_grp_cls_code: group, response_pdno: pdno, matches, note } of POLICY.cases) {
  test(`판정표: ${code}(${group}) ↔ 응답 pdno ${JSON.stringify(pdno)} → ${matches}`, () => {
    assert.equal(kisResponsePdnoMatches(pdno, { code, scrt_grp_cls_code: group }), matches, note);
  });
}

// 실측 응답 행을 실제 종목마스터와 맞댄다. 보낸 코드의 마스터 항목과 맞고, 마스터 전체에서
// 그 항목 하나하고만 맞아야 한다 — 둘 이상이면 응답 행이 어느 종목인지 갈린다.
for (const [index, { tr_id: trId, sent_code: sentCode, row }] of POLICY.responses.entries()) {
  test(`실측 ${trId} 응답 #${index} (${sentCode} → pdno ${row.pdno})는 마스터의 그 종목 하나와만 맞는다`, () => {
    const stock = resolveStock(sentCode);
    assert.equal(stock.code, sentCode, "실측에 쓴 코드는 마스터에 있어야 한다");
    assert.ok(kisResponsePdnoMatches(row.pdno, stock), `${row.pdno}는 ${sentCode}의 응답 행이다`);

    const matched = getDefaultStocks().filter((s) => kisResponsePdnoMatches(row.pdno, s)).map((s) => s.code);
    assert.deepEqual(matched, [sentCode]);
  });
}

test("실측 사실: ETN 응답 행의 pdno는 보낸 코드와 다르고, 그 밖은 같다", () => {
  // 이 단언이 깨지면(예: KIS가 Q를 붙여 돌려주기 시작) 규칙의 전제가 바뀐 것이다 — 판정표와
  // kis-pdno.js 주석을 다시 확인해야 한다. 픽스처가 실측값이라는 점을 고정하는 검사다.
  for (const { sent_code: sentCode, row } of POLICY.responses) {
    const stock = resolveStock(sentCode);
    const isEtn = stock.scrt_grp_cls_code === ETN_SECURITY_GROUP;
    assert.equal(row.pdno === sentCode, !isEtn, `${sentCode} → ${row.pdno}`);
  }
});

test("응답에는 상품군 판별 값이 없다: 잔고 행엔 상품유형 필드가 없고 주문체결 prdt_type_cd는 비어 있다", () => {
  // 판정 근거를 응답이 아닌 마스터에 두는 이유를 고정한다.
  for (const { tr_id: trId, row } of POLICY.responses) {
    if (trId === "VTTC8434R") {
      assert.ok(!Object.keys(row).some((key) => /prdt_type|scrt_grp|prdt_clsf/.test(key)), Object.keys(row).join(","));
    } else {
      assert.equal(row.prdt_type_cd, "");
    }
  }
});

test("종목마스터 불변식: EN은 모두 Q+숫자 6자이고, 그 6자를 코드로 쓰는 다른 항목이 없다", () => {
  // 마스터가 갱신돼 이 전제가 깨지면 Q를 뗀 응답 pdno가 두 종목과 맞을 수 있다.
  const stocks = getDefaultStocks();
  const codes = new Set(stocks.map((s) => String(s.code)));
  const etns = stocks.filter((s) => s.scrt_grp_cls_code === ETN_SECURITY_GROUP);
  assert.ok(etns.length > 0, "마스터에 ETN이 있어야 이 검사가 의미 있다");
  for (const etn of etns) {
    const bare = etnResponsePdno(etn);
    assert.notEqual(bare, null, `${etn.code}는 Q+숫자 6자여야 한다`);
    assert.ok(!codes.has(bare), `${etn.code}의 응답 형태 ${bare}가 다른 마스터 코드와 겹친다`);
  }
  // Q로 시작하는 마스터 코드는 모두 EN이다 — 그룹 값과 코드 모양이 어긋난 항목이 없다.
  for (const s of stocks.filter((x) => String(x.code).startsWith("Q"))) {
    assert.equal(s.scrt_grp_cls_code, ETN_SECURITY_GROUP, s.code);
  }
});

test("dailyCcldPdnoParam: ETN만 서버 필터를 비우고 나머지는 코드를 그대로 보낸다", () => {
  assert.equal(dailyCcldPdnoParam(resolveStock("Q500035")), "");
  assert.equal(dailyCcldPdnoParam(resolveStock("00088K")), "00088K");
  assert.equal(dailyCcldPdnoParam(resolveStock("005930")), "005930");
  // 마스터에 없는 코드 형태 입력(resolveStock의 UNKNOWN 에코)은 그룹을 모르므로 그대로 보낸다.
  assert.equal(dailyCcldPdnoParam(resolveStock("Q999999")), "Q999999");
});
