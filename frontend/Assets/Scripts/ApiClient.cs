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

    public ApiClient(string apiBaseUrl)
    {
        this.apiBaseUrl = apiBaseUrl;
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
}
