[System.Serializable]
public class Holding
{
    public string name;
    // models.py: current_price Optional[float] — DB 스키마가 소수를 허용하므로
    // float으로 받아 소수 부분 손실을 방지한다.
    // (한국 주식 현재가는 실제로 항상 원 단위 정수이지만 스키마 타입을 따른다.)
    public float current_price;
    // API는 float(가중평균 매수단가)를 반환한다. int로 받으면 소수 부분이 잘린다.
    public float avg_price;
    public float return_rate;
    public int quantity;
    // Unity JsonUtility는 nullable 값 타입을 지원하지 않아 null → 0으로 처리한다.
    // price_known=false이면 current_price는 0이 아니라 미확인 값이다.
    public bool price_known;
    // return_rate_known=false이면 return_rate는 0이 아니라 계산 불가 상태다.
    // current_price는 알지만 avg_price <= 0이면 price_known=true·return_rate_known=false가 된다.
    public bool return_rate_known;

    public float GetWeight(double total_asset)
    {
        if (total_asset == 0)
            return 0f;
        // 비중은 현재가만 알면 계산된다 — 수익률을 모르는 것과 무관하다.
        // 현재가 미상이면 total_asset도 avg_price × quantity 기준 근사값을 포함하므로
        // 같은 기준으로 비중을 계산한다. avg_price=0이면 기여분이 없으므로 0을 반환.
        double basis = price_known ? (double)current_price : (double)avg_price;
        return (float)(basis * quantity / total_asset * 100.0);
    }
}

[System.Serializable]
public class PortfolioData
{
    // avg_price(가중평균 매수단가)는 소수다. 현재가 미상 종목의 total_asset 기여분은
    // avg_price × quantity로 계산되므로 total_asset도 소수가 될 수 있다.
    // long으로 선언하면 Unity JsonUtility가 소수 JSON 값을 파싱하지 못한다.
    public double total_asset;
    // true이면 total_asset에 현재가 없는 종목의 매입가 추정분이 포함된다.
    public bool total_asset_is_estimate;
    public float total_return_rate;
    // Unity JsonUtility는 null → 0으로 처리한다.
    // false이면 total_return_rate는 0%가 아니라 계산 불가 상태다.
    public bool total_return_rate_known;
    public Holding[] holdings;
}
