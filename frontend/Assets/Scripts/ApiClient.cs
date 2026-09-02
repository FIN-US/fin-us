// ApiClient.cs — FastAPI 백엔드와 HTTP로 통신한다.
// UnityWebRequest + IEnumerator 코루틴: 프레임을 막지 않고 응답을 기다린 뒤 콜백으로 결과를 넘긴다.
// 성공 본문은 JsonUtility로 파싱(모델은 ApiModels). 실패 시 본문에 detail이 있으면 그 문자열을 우선 사용한다.
using System;
using System.Collections;
using UnityEngine;
using UnityEngine.Networking;

public class ApiClient
{
    private readonly string apiBaseUrl;

    // 백엔드 주소를 하드코딩하지 않기 위한 기본 베이스 URL이다(이슈 #246).
    // WebGL 번들은 nginx가 /api/·/health를 backend:8000으로 프록시하는 오리진에서
    // 서빙되므로(이슈 #245) 베이스 URL이 아예 필요 없다. 빈 문자열이면 요청 URL이
    // "/api/v1/..." 상대 경로가 되고, UnityWebRequest가 Application.absoluteURL(=페이지
    // 주소) 기준으로 절대 URL을 만든다. 그래서 호스트·스킴·포트가 무엇이든(로컬,
    // Tailscale, 443의 리버스 프록시 뒤) 그대로 맞는다. 포트를 8000으로 고정하는 오리진
    // 해석은 443 뒤에서 깨지므로 채택하지 않았다.
    //
    // 선행 슬래시가 붙은 root-relative 경로라 서브패스까지 자동으로 따라가지는 않는다.
    // 대시보드를 https://example.com/finus/ 에 마운트하면 요청은 /finus/api/...가 아니라
    // /api/...로 나가므로, 그 구성에서는 리버스 프록시가 /api를 루트에서 함께 중계해야 한다.
    //
    // 에디터 플레이 모드에는 페이지 오리진이 없어(Application.absoluteURL이 빈 문자열)
    // 상대 경로를 절대 URL로 만들 수 없다. 그때만 로컬 백엔드를 직접 가리킨다.
    public static string DefaultBaseUrl =>
#if UNITY_WEBGL && !UNITY_EDITOR
        string.Empty;
#else
        "http://localhost:8000";
#endif

    public ApiClient(string apiBaseUrl)
    {
        // 끝의 '/'를 남기면 "http://host:8000//api/v1/..."처럼 슬래시가 겹친다.
        this.apiBaseUrl = string.IsNullOrWhiteSpace(apiBaseUrl)
            ? string.Empty
            : apiBaseUrl.Trim().TrimEnd('/');
    }

    // 흐름: 뉴스 GET → 성공 시 트렌드 GET → 둘 다 성공이면 JSON 파싱 후 DataOnlyResult로 묶어 onSuccess.
    public IEnumerator FetchDataOnly(string stock, Action<DataOnlyResult> onSuccess, Action<string> onError)
    {
        var newsUrl = $"{apiBaseUrl}/api/v1/news?stock={UnityWebRequest.EscapeURL(stock)}";
        var trendUrl = $"{apiBaseUrl}/api/v1/trading/trend?stock={UnityWebRequest.EscapeURL(stock)}";

        using var newsReq = UnityWebRequest.Get(newsUrl);
        using var trendReq = UnityWebRequest.Get(trendUrl);

        yield return newsReq.SendWebRequest();
        if (newsReq.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(newsReq, "뉴스 조회 실패"));
            yield break;
        }

        yield return trendReq.SendWebRequest();
        if (trendReq.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(trendReq, "트렌드 조회 실패"));
            yield break;
        }

        var newsResponse = TryFromJson<NewsApiResponse>(newsReq.downloadHandler.text);
        var trendResponse = TryFromJson<TrendApiResponse>(trendReq.downloadHandler.text);

        // 파싱 실패를 빈 결과로 넘기면 "뉴스가 없음"과 구분되지 않는다. 다른 Fetch들과
        // 같이 onError로 돌린다.
        if (newsResponse == null || trendResponse == null)
        {
            onError?.Invoke("뉴스·트렌드 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(new DataOnlyResult
        {
            newsItems = newsResponse.data?.news ?? new string[0],
            trendRaw = trendResponse.data?.trend ?? string.Empty
        });
    }

    // 흐름: analyze GET 한 번 → HTTP 성공 후 status/data 검증 → AnalyzeData만 onSuccess로 전달.
    public IEnumerator FetchAnalysis(string stock, string provider, Action<AnalyzeData> onSuccess, Action<string> onError)
    {
        var analyzeUrl = $"{apiBaseUrl}/api/v1/analyze?stock={UnityWebRequest.EscapeURL(stock)}&provider={UnityWebRequest.EscapeURL(provider)}";
        using var req = UnityWebRequest.Get(analyzeUrl);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(req, "분석 실패"));
            yield break;
        }

        var parsed = TryFromJson<AnalyzeApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("분석 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data);
    }

    public IEnumerator FetchBalance(Action<string> onSuccess, Action<string> onError)
    {
        var balanceUrl = $"{apiBaseUrl}/api/v1/trading/balance";
        using var req = UnityWebRequest.Get(balanceUrl);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(req, "잔고 조회 실패"));
            yield break;
        }

        var parsed = TryFromJson<BalanceApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("잔고 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data.report ?? string.Empty);
    }

    public IEnumerator FetchNews(string stock, Action<string[]> onSuccess, Action<string> onError)
    {
        var newsUrl = $"{apiBaseUrl}/api/v1/news?stock={UnityWebRequest.EscapeURL(stock)}";
        using var req = UnityWebRequest.Get(newsUrl);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(req, "뉴스 조회 실패"));
            yield break;
        }

        var parsed = TryFromJson<NewsApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("뉴스 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data.news ?? new string[0]);
    }

    public IEnumerator FetchTrend(string stock, Action<string> onSuccess, Action<string> onError)
    {
        var trendUrl = $"{apiBaseUrl}/api/v1/trading/trend?stock={UnityWebRequest.EscapeURL(stock)}";
        using var req = UnityWebRequest.Get(trendUrl);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(req, "트렌드 조회 실패"));
            yield break;
        }

        var parsed = TryFromJson<TrendApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("트렌드 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data.trend ?? string.Empty);
    }

    // JsonUtility.FromJson은 JSON이 아닌 본문에 대해 null을 주지 않고 ArgumentException을
    // 던진다. 이 클래스의 파싱은 전부 코루틴 안에서 일어나므로, 예외가 나면 코루틴이 그
    // 자리에서 끊기고 onError조차 불리지 않는다 — 화면에는 아무 일도 일어나지 않고, #244가
    // 막으려던 "조용한 실패"가 그대로 재현된다.
    //
    // 가정과 달리 이 경로는 드물지 않다. backend가 떠 있지 않으면 nginx가 502를 HTML 본문과
    // 함께 돌려주는데(#245의 프록시 구성), 그 본문이 곧바로 여기로 들어온다. 즉 가장 흔한
    // 실패 상황에서 실패 배너가 뜨지 않는다.
    // PieChartLoader가 Resources 픽스처를 파싱할 때도 쓴다 — 그 호출도 코루틴 안이라
    // 실패 양상이 같다. 안전한 파서가 이 클래스 밖에 또 생기지 않게 여기를 연다.
    public static T TryFromJson<T>(string body) where T : class
    {
        if (string.IsNullOrWhiteSpace(body))
            return null;

        try
        {
            return JsonUtility.FromJson<T>(body);
        }
        catch (ArgumentException)
        {
            // 본문이 JSON이 아니다. 호출부는 null을 "파싱 실패"로 다뤄 onError로 넘긴다.
            return null;
        }
    }

    // FastAPI HTTPException 응답은 보통 { "detail": "문자열" }. 없으면 UnityWebRequest.error로 폴백.
    private static string ExtractErrorMessage(UnityWebRequest request, string fallbackPrefix)
    {
        var detail = TryFromJson<ErrorDetailResponse>(request.downloadHandler?.text);
        if (!string.IsNullOrWhiteSpace(detail?.detail))
        {
            return detail.detail;
        }

        var errorDetail = string.IsNullOrWhiteSpace(request.error) ? request.result.ToString() : request.error;
        return $"{fallbackPrefix}: {errorDetail}{DetailSuffix}{request.url}, status={request.responseCode})";
    }

    // 위 포맷이 붙이는 꼬리와, 아래에서 그것을 떼는 코드를 같은 자리에 둔다. 떨어져 있으면
    // 포맷만 바뀌었을 때 잘라내기가 조용히 실패해 내부 주소가 사용자 화면에 다시 샌다.
    private const string DetailSuffix = " (url=";

    // 배너처럼 사용자에게 보이는 자리에는 요약만 싣는다(#262 리뷰). 꼬리에는 백엔드 내부
    // 주소가 들어 있고, 원문은 호출부가 콘솔에 남긴다.
    public static string SummarizeError(string error)
    {
        if (string.IsNullOrEmpty(error))
            return error;

        int tail = error.IndexOf(DetailSuffix, StringComparison.Ordinal);
        return tail < 0 ? error : error.Substring(0, tail);
    }

    public IEnumerator FetchPortfolio(Action<PortfolioData> onSuccess, Action<string> onError)
    {
        var url = $"{apiBaseUrl}/api/v1/portfolio";
        using var req = UnityWebRequest.Get(url);
        yield return req.SendWebRequest();

        if (req.result != UnityWebRequest.Result.Success)
        {
            onError?.Invoke(ExtractErrorMessage(req, "포트폴리오 조회 실패"));
            yield break;
        }

        var parsed = TryFromJson<PortfolioApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("포트폴리오 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data);
    }
}
