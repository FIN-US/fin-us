[System.Serializable]
public class Holding
{
    public string name;
    public int current_price;
    public int avg_price;
    public float return_rate;
    public int quantity;

    public float GetWeight(long total_asset)
    {
        return (float)(current_price * quantity) / total_asset * 100f;
    }
}

[System.Serializable]
public class PortfolioData
{
    public long total_asset;
    public float total_return_rate;
    public Holding[] holdings;
}