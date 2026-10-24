import assert from "node:assert/strict";
import test from "node:test";
import { mentionsStock, pickStockNews } from "../relevance.js";

const item = (title, description = "") => ({ title, description });

test("이메일·URL의 도메인에만 걸린 종목명은 언급으로 치지 않는다 (#448: NAVER → naver.com)", () => {
  assert.equal(mentionsStock("뷰어스 홍길동 기자 reporter@naver.com 이에이트가 세종대와 협력", "NAVER"), false);
  assert.equal(mentionsStock("출처: https://m.blog.naver.com/abc", "NAVER"), false);
  assert.equal(mentionsStock("NAVER 주가, 장중 1.29% 상승", "NAVER"), true);
  assert.equal(mentionsStock("naver 검색 점유율", "NAVER"), true);
});

test("종목명을 언급한 기사만 고르고, 제목 언급을 요약 언급보다 앞에 둔다(같은 순위는 최신순 유지)", () => {
  const items = [
    item("이에이트, 세종대와 건설로봇 협력", "홍길동 기자 a@naver.com"),
    item("코스피 마감 시황", "NAVER는 1% 올랐다"),
    item("NAVER, AI 투자 확대"),
    item("리솔, 일본 시장 진출", "b@naver.com"),
    item("NAVER 주가 상승"),
  ];
  assert.deepEqual(
    pickStockNews(items, "NAVER", 3).map((i) => i.title),
    ["NAVER, AI 투자 확대", "NAVER 주가 상승", "코스피 마감 시황"],
  );
});

test("언급한 기사가 상한보다 적으면 언급하지 않은 기사로 채우지 않는다", () => {
  const items = [item("이에이트 협력", "a@naver.com"), item("NAVER 실적")];
  assert.deepEqual(pickStockNews(items, "NAVER", 3).map((i) => i.title), ["NAVER 실적"]);
});

test("언급한 기사가 하나도 없으면(예: 6자리 코드 조회) 최신 기사를 그대로 돌려준다", () => {
  const items = [item("a"), item("b"), item("c"), item("d")];
  assert.deepEqual(pickStockNews(items, "035420", 3).map((i) => i.title), ["a", "b", "c"]);
  assert.deepEqual(pickStockNews([], "NAVER", 3), []);
});
