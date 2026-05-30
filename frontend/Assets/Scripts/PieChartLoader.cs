using UnityEngine;

public class PieChartLoader : MonoBehaviour
{
    private ApiClient apiClient;
    private PieChart pieChart;

    void Awake()
    {
        apiClient = new ApiClient("http://localhost:8000");
        pieChart = FindAnyObjectByType<PieChart>();
    }

    void Start()
    {
        StartCoroutine(apiClient.FetchPortfolio(
            onSuccess: (portfolioData) =>
            {
                pieChart.Generate(portfolioData);
            },
            onError: (err) =>
            {
                Debug.LogError("포트폴리오 오류: " + err);
                LoadDummy();
            }
        ));
    }

    void LoadDummy()
    {
        TextAsset json = Resources.Load<TextAsset>("data");
        PortfolioData data = JsonUtility.FromJson<PortfolioData>(json.text);
        pieChart.Generate(data);
    }
}