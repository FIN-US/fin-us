// index.js는 부작용을 가진 서버 진입점이라 여기서 import하지 않는다(balance.js:17,
// order-submit.js:1-3와 동일한 컨벤션) — 이 파일도 그래서 index.js 밖으로 분리했다.
//
// #163: kisPost(주문 제출 HTTP POST)가 export되지 않아 이 함수가 던지는 세 가지 플래그
// (kisOrderNotSubmitted / kisOrderSubmittedMaybe / kisOrderRejected) — order-submit.js:17의
// 중복 주문 가드 해제 여부를 좌우하는 그 경계 — 를 고정하는 테스트가 하나도 없었다. 여기로
// 옮기고 kisAxios 인스턴스를 주입받게 해서, 실제 네트워크 없이 세 갈래 분류를 테스트할 수
// 있게 한다.
//
// getAccessToken(토큰 캐시 포함, index.js)은 kisGet/kisApiGet 조회 경로와도 공유되고 파일
// 캐시라는 별개 관심사를 안고 있어 여기로 옮기지 않고 함수로 주입받는다 — 이 경계가 실제로
// 신경 쓰는 것은 "pre-flight가 실패했는가"이지 "왜 실패했는가"가 아니다.

async function createKisHashKey({ kisAxios, kisUrl, appKey, appSecret, body }) {
  const response = await kisAxios.post(`${kisUrl}/uapi/hashkey`, body, {
    headers: {
      "Content-Type": "application/json",
      appkey: appKey,
      appsecret: appSecret,
    },
  });
  const hashKey = response.data?.HASH;
  if (!hashKey) {
    throw new Error("KIS hashkey 발급 실패");
  }
  return hashKey;
}

// index.js는 이 아홉 개(kisAxios, kisUrl, appKey, appSecret, pathname, trId, body, useHashKey,
// getAccessToken)를 명시적으로 배선한다 — 분리 전에는 전부 index.js 모듈 스코프 변수를 암묵적으로
// 읽었다. 배선을 빠뜨리면(예: getAccessToken 인자를 통째로 빼먹으면) 그 자리에서
// "getAccessToken is not a function" 같은 일반 TypeError가 나서 어디가 문제인지 알기 어렵고,
// 실제 프로덕션 배선(index.js의 단일 호출부)을 그대로 재현하는 테스트가 아니면 잡히지도
// 않는다. 필수 의존성이 비어 있으면 어떤 것이 빠졌는지 이름으로 지목하는 에러를 즉시 던져서,
// 이 함수를 부르는 어떤 호출부에서든(지금은 index.js 하나지만) 실수를 빨리, 시끄럽게 드러낸다.
const KIS_ORDER_POST_REQUIRED_DEPENDENCIES = [
  "kisAxios",
  "kisUrl",
  "appKey",
  "appSecret",
  "pathname",
  "trId",
  "body",
  "getAccessToken",
];

function requireKisOrderPostDependencies(deps) {
  for (const name of KIS_ORDER_POST_REQUIRED_DEPENDENCIES) {
    if (deps[name] === undefined) {
      throw new Error(`kisOrderPost: 필수 인자 '${name}'이(가) 주입되지 않았습니다. 호출부의 배선을 확인하세요.`);
    }
  }
}

// 주문 제출 전용 POST. 호출자는 정확히 하나(index.js의 place_order 경로)이고 플래그를 읽는
// 곳도 order-submit.js 하나뿐이다. 읽기 전용 도구는 전부 kisGet/kisApiGet을 쓰므로, 다음에
// 읽기 전용 POST 도구(예: 주문 정정·취소 조회가 아닌 별도 POST)를 추가할 사람이 범용 이름에
// 이끌려 이 함수를 재사용해 주문 제출 시맨틱(중복 방지 가드 해제 판정)을 그대로 물려받지
// 않도록 kisOrderPost로 이름 붙였다.
export async function kisOrderPost({
  kisAxios,
  kisUrl,
  appKey,
  appSecret,
  pathname,
  trId,
  body,
  useHashKey = false,
  getAccessToken,
}) {
  requireKisOrderPostDependencies({ kisAxios, kisUrl, appKey, appSecret, pathname, trId, body, getAccessToken });

  // 이 블록(토큰 발급, 필요 시 해시키 발급)이 던지는 오류는 아래 실제 주문 POST가 아직
  // 나가기 전에 발생한 것이므로, 주문이 KIS에 제출되지 않았음이 확실하다.
  let token;
  let hashKey;
  try {
    token = await getAccessToken();
    if (useHashKey) {
      hashKey = await createKisHashKey({ kisAxios, kisUrl, appKey, appSecret, body });
    }
  } catch (error) {
    // ES 모듈은 항상 strict mode라 원시값(문자열/숫자/null 등)에 프로퍼티를 대입하면
    // TypeError가 난다. 이 구간의 모든 throw 지점(getAccessToken의 new Error, createKisHashKey의
    // new Error, axios 에러)은 항상 객체를 던지므로 오늘은 도달 불가능하지만, 바로 아래의
    // kisOrderSubmittedMaybe 태깅과 대칭을 맞추고 향후 이 블록에 원시값을 던질 수 있는 코드가
    // 섞여도 "에러를 분류하는" 이 코드 자체가 죽지 않도록 방어적으로 가드한다.
    if (error && typeof error === "object") {
      error.kisOrderNotSubmitted = true;
    }
    throw error;
  }

  const headers = {
    "Content-Type": "application/json",
    authorization: `Bearer ${token}`,
    appkey: appKey,
    appsecret: appSecret,
    tr_id: trId,
    custtype: "P",
  };
  if (useHashKey) {
    headers.hashkey = hashKey;
  }

  let response;
  try {
    response = await kisAxios.post(`${kisUrl}${pathname}`, body, { headers });
  } catch (error) {
    // 위 pre-flight 블록과 동일한 이유로 가드한다: axios 에러는 항상 객체이므로 오늘은
    // 도달 불가능하지만, 대칭을 맞춰 둔다.
    if (error && typeof error === "object") {
      error.kisOrderSubmittedMaybe = true;
    }
    throw error;
  }

  const data = response.data;
  if (data.rt_cd !== "0") {
    const error = new Error(`KIS API 오류: ${data.msg1 || data.msg_cd || "알 수 없는 오류"}`);
    error.kisOrderRejected = true;
    throw error;
  }
  return data;
}
