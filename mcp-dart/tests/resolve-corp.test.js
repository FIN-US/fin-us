import assert from "node:assert/strict";
import test from "node:test";
import { resolveCorp } from "../corp-resolver.js";

// 합성 items 배열 — 외부 의존성 없이 순수 단위 테스트
const items = [
  { corp_name: "CJ", stock_code: "001040", corp_code: "00000001" },
  { corp_name: "삼성전자", stock_code: "005930", corp_code: "00000002" },
  { corp_name: "현대차", stock_code: "005380", corp_code: "00000003" },
  { corp_name: "LG화학", stock_code: "051910", corp_code: "00000004" },
  // 우선주가 실제로 corp_name으로 등록된 경우(완전일치 우선 검증용)
  { corp_name: "삼성전자우", stock_code: "005935", corp_code: "00000005" },
];

// 우선주·전환주 접미사 정규화 폴백 케이스
test("CJ4우(전환) → corp_name CJ 항목 반환", () => {
  const result = resolveCorp(items, "CJ4우(전환)");
  assert.equal(result.corp_name, "CJ");
  assert.equal(result.stock_code, "001040");
});

test("현대차2우B → corp_name 현대차 항목 반환", () => {
  const result = resolveCorp(items, "현대차2우B");
  assert.equal(result.corp_name, "현대차");
  assert.equal(result.stock_code, "005380");
});

test("LG화학우 → corp_name LG화학 항목 반환", () => {
  const result = resolveCorp(items, "LG화학우");
  assert.equal(result.corp_name, "LG화학");
  assert.equal(result.stock_code, "051910");
});

// 완전일치가 폴백보다 우선
test("삼성전자우 → items에 실제로 있으므로 완전일치(삼성전자우) 우선 반환", () => {
  const result = resolveCorp(items, "삼성전자우");
  // 폴백(삼성전자)이 아니라 완전일치(삼성전자우)가 반환되어야 함
  assert.equal(result.corp_name, "삼성전자우");
  assert.equal(result.stock_code, "005935");
});

// 6자리 종목코드 경로
test("005930 → stock_code 경로로 삼성전자 반환", () => {
  const result = resolveCorp(items, "005930");
  assert.equal(result.corp_name, "삼성전자");
  assert.equal(result.stock_code, "005930");
});

// 접미사 없는 이름이 없으면 throw
test("없는 법인명은 접미사 제거 후에도 throw", () => {
  assert.throws(
    () => resolveCorp(items, "존재하지않는회사우"),
    (err) => {
      assert.ok(err.message.includes("존재하지않는회사우"));
      return true;
    },
  );
});

// 정규화 후에도 없으면 원래 query로 에러
test("정규화 후에도 없으면 원래 query가 에러 메시지에 포함됨", () => {
  assert.throws(
    () => resolveCorp(items, "없는회사4우(전환)"),
    (err) => {
      assert.ok(err.message.includes("없는회사4우(전환)"));
      return true;
    },
  );
});

// 빈 입력은 throw
test("빈 stock_name은 throw", () => {
  assert.throws(
    () => resolveCorp(items, ""),
    /stock_name 파라미터가 비어 있습니다/,
  );
});

// 일반 법인명(접미사 없음)은 완전일치 경로
test("삼성전자(접미사 없음) → 완전일치로 반환", () => {
  const result = resolveCorp(items, "삼성전자");
  assert.equal(result.corp_name, "삼성전자");
  assert.equal(result.stock_code, "005930");
});
