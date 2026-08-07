[System.Serializable]
public class Holding
{
    public string name;
    public int current_price;
    // API는 float(가중평균 매수단가)를 반환한다. int로 받으면 소수 부분이 잘린다.
    public float avg_price;
    public float return_rate;
    public int quantity;
    // Unity JsonUtility는 nullable 값 타입을 지원하지 않아 null → 0으로 처리한다.
    // API가 current_price·return_rate를 모를 때 0이 아니라 "알 수 없음"임을 나타내는 플래그.
    // price_known=false이면 current_price·return_rate는 0이 아니라 미확인 값이다.
    public bool price_known;

    public float GetWeight(long total_asset)
    {
        if (!price_known || total_asset == 0)
            return 0f;
        return (float)(current_price * quantity) / total_asset * 100f;
    }
}

[System.Serializable]
public class PortfolioData
{
    public long total_asset;
    // true이면 total_asset에 현재가 없는 종목의 매입가 추정분이 포함된다.
    public bool total_asset_is_estimate;
    public float total_return_rate;
    // Unity JsonUtility는 null → 0으로 처리한다.
    // false이면 total_return_rate는 0%가 아니라 계산 불가 상태다.
    public bool total_return_rate_known;
    public Holding[] holdings;
}