// index.js는 부작용을 가진 서버 진입점이라 여기서 import하지 않는다(balance.js:17와 동일한
// 컨벤션) — order.js, order-dedup.js, formatters.js, stock-master.js처럼 이 파일도 그래서
// index.js 밖으로 분리했다.
//
// KIS 제출과 dedup 원장 기록을 분리한다: 주문 결과는 오직 KIS 응답으로만 결정되고,
// 원장 기록(성공 표시/거절·미제출 확인 시 해제)의 실패는 이미 확정된 주문 결과를
// 절대 뒤집지 않는다.
export async function submitOrder({ dedupStore, dedupKey, submit }) {
  let data;
  try {
    data = await submit();
  } catch (error) {
    // 명시적 허용 목록: KIS가 거절했다고 확인됐거나(kisOrderRejected) KIS로 보내는 POST
    // 자체가 나가기 전에 실패해 제출되지 않았음이 확실한 경우(kisOrderNotSubmitted)에만
    // 해제한다. axios 전송 실패(제출 여부 불명, kisOrderSubmittedMaybe)나 그 밖에
    // 인식하지 못한 예외는 기본적으로 해제하지 않는다(fail-closed).
    if (error?.kisOrderRejected === true || error?.kisOrderNotSubmitted === true) {
      try {
        dedupStore.release(dedupKey);
      } catch (releaseError) {
        // release()도 원장 파일을 거쳐 던질 수 있다. 여기서 던지면 원래 KIS 오류(거절/
        // 미제출 사유)가 사라지므로, 로그만 남기고 원래 예외를 그대로 전파한다. 항목은
        // in_flight로 남아 TTL까지 중복을 계속 막으므로 fail-closed 성질은 유지된다.
        console.error(
          `주문 거절/미제출 확인 후 원장 정리 실패(항목은 in_flight로 유지): ${String(releaseError?.message ?? releaseError)}`,
        );
      }
    }
    throw error;
  }

  try {
    dedupStore.markSucceeded(dedupKey, data);
  } catch (error) {
    // 주문은 이미 KIS에 체결됐다. 기록 실패로 가드를 지우면 재시도가 통과해 중복 주문이
    // 나간다. 항목을 in_flight로 남겨두면 TTL 만료까지 중복을 계속 막는 안전한 저하다.
    console.error(
      `주문 성공 후 원장 기록 실패(항목은 in_flight로 유지): ${String(error?.message ?? error)}`,
    );
  }

  return data;
}
