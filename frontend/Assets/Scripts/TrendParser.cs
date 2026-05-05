// TrendParser.cs — API가 내려주는 트렌드 필드는 한 덩어리 텍스트(줄바꿈 구분)다.
// "|"가 있는 줄만 후보로 삼고, 날짜·종가·변동·외인·기관·거래량 토큰을 잘라 TrendItem으로 만든다. 포맷이 어긋나면 해당 줄은 스킵.

using System.Collections.Generic;
using System.Linq;

public static class TrendParser
{
    public static List<TrendItem> Parse(string trendStr)
    {
        var results = new List<TrendItem>();
        if (string.IsNullOrWhiteSpace(trendStr))
        {
            return results;
        }

        var lines = trendStr.Split('\n').Where(line => line.Contains("|"));
        foreach (var line in lines)
        {
            var parts = line.Split('|').Select(part => part.Trim()).ToArray();
            if (parts.Length < 6)
            {
                continue; // 기대 컬럼 수 미만이면 파싱 불가
            }

            var changeText = parts[2].Replace("변동:", string.Empty).Trim();
            var isUp = changeText.Contains("상승");
            var cleaned = changeText.Replace("상승", string.Empty).Replace("하락", string.Empty).Trim();
            var changeValue = cleaned.Split('(')[0].Trim();
            var changePct = changeText.Contains("(") ? changeText.Split('(')[1].Replace(")", string.Empty).Trim() : "0%";

            results.Add(new TrendItem
            {
                date = parts[0].Split(' ')[0],
                price = ParseInt(parts[1].Replace("종가:", string.Empty)),
                changeVal = changeValue,
                changePct = changePct,
                isUp = isUp,
                foreigner = ParseInt(parts[3].Replace("외인:", string.Empty)),
                institution = ParseInt(parts[4].Replace("기관:", string.Empty)),
                volume = ParseInt(parts[5].Replace("거래량:", string.Empty))
            });
        }

        return results;
    }

    private static int ParseInt(string text)
    {
        var cleaned = text.Replace(",", string.Empty).Trim();
        return int.TryParse(cleaned, out var value) ? value : 0;
    }
}
