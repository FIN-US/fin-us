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
    // 주소) 기준으로 절대 URL을 만든다. 그래서 로컬·Tailscale·리버스 프록시 뒤·서브패스
    // 어디로 열어도 그대로 맞는다. 포트를 고정하는 오리진 해석은 443·서브패스에서
    // 깨지므로 채택하지 않았다.
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

        var newsResponse = JsonUtility.FromJson<NewsApiResponse>(newsReq.downloadHandler.text);
        var trendResponse = JsonUtility.FromJson<TrendApiResponse>(trendReq.downloadHandler.text);

        onSuccess?.Invoke(new DataOnlyResult
        {
            newsItems = newsResponse?.data?.news ?? new string[0],
            trendRaw = trendResponse?.data?.trend ?? string.Empty
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

        var parsed = JsonUtility.FromJson<AnalyzeApiResponse>(req.downloadHandler.text);
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

        var parsed = JsonUtility.FromJson<BalanceApiResponse>(req.downloadHandler.text);
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

        var parsed = JsonUtility.FromJson<NewsApiResponse>(req.downloadHandler.text);
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

        var parsed = JsonUtility.FromJson<TrendApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("트렌드 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data.trend ?? string.Empty);
    }

    // FastAPI HTTPException 응답은 보통 { "detail": "문자열" }. 없으면 UnityWebRequest.error로 폴백.
    private static string ExtractErrorMessage(UnityWebRequest request, string fallbackPrefix)
    {
        var body = request.downloadHandler?.text;
        if (!string.IsNullOrWhiteSpace(body))
        {
            var detail = JsonUtility.FromJson<ErrorDetailResponse>(body);
            if (!string.IsNullOrWhiteSpace(detail?.detail))
            {
                return detail.detail;
            }
        }

        var errorDetail = string.IsNullOrWhiteSpace(request.error) ? request.result.ToString() : request.error;
        return $"{fallbackPrefix}: {errorDetail} (url={request.url}, status={request.responseCode})";
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

        var parsed = JsonUtility.FromJson<PortfolioApiResponse>(req.downloadHandler.text);
        if (parsed == null || parsed.status != "success" || parsed.data == null)
        {
            onError?.Invoke("포트폴리오 데이터를 파싱하지 못했습니다.");
            yield break;
        }

        onSuccess?.Invoke(parsed.data);
    }
}
