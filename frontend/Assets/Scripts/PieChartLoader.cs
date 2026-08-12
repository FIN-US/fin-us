using UnityEngine;
using System.Collections;
using UnityEngine.UIElements;

public class PieChartLoader : MonoBehaviour
{
    private ApiClient apiClient;
    private PieChart pieChart;
    [SerializeField] private PanelController panelController;

    void Awake()
    {
        apiClient = new ApiClient("http://localhost:8000");
        pieChart = FindAnyObjectByType<PieChart>();
        EnsurePanelController();
    }

    void Start()
    {
        StartCoroutine(apiClient.FetchPortfolio(
            onSuccess: (portfolioData) =>
            {
                pieChart.Generate(portfolioData);
                StartCoroutine(UpdateTopBarWhenReady(portfolioData));
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
        StartCoroutine(UpdateTopBarWhenReady(data));
    }

    IEnumerator UpdateTopBarWhenReady(PortfolioData data)
    {
        for (int i = 0; i < 30; i++)
        {
            EnsurePanelController();
            if (panelController != null && panelController.UpdateTopBar(data))
            {
                yield break;
            }

            yield return null;
        }

        Debug.LogWarning(panelController == null
            ? "PanelController was not found. Make sure UIManager exists in DataVisualizationScene."
            : "PanelController was found, but its UI labels were not ready. Portfolio top bar was not updated.");
    }

    void EnsurePanelController()
    {
        if (panelController != null)
            return;

        panelController = FindAnyObjectByType<PanelController>();
        if (panelController != null)
            return;

        UIDocument document = FindAnyObjectByType<UIDocument>();
        if (document != null)
            panelController = document.gameObject.AddComponent<PanelController>();
    }
}
