// ApiModels.cs — 백엔드 JSON ↔ C# 매핑용 타입 정의.
// JsonUtility는 public 필드 이름이 JSON 키와 같아야 하므로, Python 쪽 snake_case(source_news 등)를 그대로 둔다.
// API 루트 래퍼와 UI 전용(DataOnlyResult, TrendItem)을 한 파일에 둔다.

using System;

[Serializable]
public class AnalyzeApiResponse
{
    public string status;
    public AnalyzeData data;
}

// /api/v1/analyze 의 data 본문. trading_trend는 이후 TrendParser에서 줄 단위로 해석한다.
[Serializable]
public class AnalyzeData
{
    public string summary;
    public TradingDetails details;
    public string[] source_news;
    public string trading_trend;
}

[Serializable]
public class TradingDetails
{
    public string decision;
    public float confidence_score;
    public string reason;
    public string target_stock;
}

[Serializable]
public class NewsApiResponse
{
    public string status;
    public NewsData data;
}

[Serializable]
public class NewsData
{
    public string[] news;
}

[Serializable]
public class TrendApiResponse
{
    public string status;
    public TrendData data;
}

[Serializable]
public class TrendData
{
    public string trend;
}

[Serializable]
public class BalanceApiResponse
{
    public string status;
    public BalanceData data;
}

[Serializable]
public class BalanceData
{
    public string report;
}

// 파싱 결과를 화면(헤더·트렌드 요약)에 쓰기 위한 값 객체. API JSON과 1:1이 아니라서 [Serializable] 생략 가능.
public class TrendItem
{
    public string date;
    public int price;
    public string changeVal;
    public string changePct;
    public bool isUp;
    public int foreigner;
    public int institution;
    public int volume;
}

// FetchDataOnly에서 뉴스 배열 + 트렌드 원문 문자열을 묶어 UI 계층으로 넘길 때 사용.
public class DataOnlyResult
{
    public string[] newsItems;
    public string trendRaw;
}

[Serializable]
public class ErrorDetailResponse
{
    public string detail;
}
