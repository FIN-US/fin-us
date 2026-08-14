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
        // 주소를 하드코딩하지 않는다(#246). 자세한 근거는 ApiClient.DefaultBaseUrl 주석 참고.
        apiClient = new ApiClient(ApiClient.DefaultBaseUrl);
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
                // 콘솔 로그만 남기면 샘플 포트폴리오가 실제 자산처럼 보인다(#244).
                // 금융 대시보드에서 가짜 잔고를 조용히 보여주는 건 위험하므로,
                // 실패 사유를 화면 배너로 올리고 차트도 샘플 표시로 그린다.
                Debug.LogError("포트폴리오 오류: " + err);
                LoadSample(err);
            }
        ));
    }

    void LoadSample(string error)
    {
        TextAsset json = Resources.Load<TextAsset>("data");
        PortfolioData data = json == null ? null : JsonUtility.FromJson<PortfolioData>(json.text);

        // 샘플조차 없으면 그릴 것이 없다. 그래도 실패 사실은 반드시 화면에 남긴다.
        if (data == null || data.holdings == null)
        {
            Debug.LogError("샘플 포트폴리오(Resources/data)를 불러오지 못했습니다.");
            StartCoroutine(ShowDataSourceErrorWhenReady(error));
            return;
        }

        pieChart.Generate(data, isSampleData: true);
        StartCoroutine(UpdateTopBarWhenReady(data, error));
    }

    // sampleDataError가 null이 아니면 실데이터가 아니라 샘플을 그린 것이므로,
    // 같은 재시도 루프에서 배너부터 띄운 뒤 상단바를 갱신한다.
    IEnumerator UpdateTopBarWhenReady(PortfolioData data, string sampleDataError = null)
    {
        for (int i = 0; i < 30; i++)
        {
            EnsurePanelController();
            if (panelController != null)
            {
                if (sampleDataError != null)
                {
                    panelController.ShowSampleDataNotice(sampleDataError);
                }

                if (panelController.UpdateTopBar(data))
                {
                    yield break;
                }
            }

            yield return null;
        }

        Debug.LogWarning(panelController == null
            ? "PanelController was not found. Make sure UIManager exists in DataVisualizationScene."
            : "PanelController was found, but its UI labels were not ready. Portfolio top bar was not updated.");
    }

    IEnumerator ShowDataSourceErrorWhenReady(string error)
    {
        for (int i = 0; i < 30; i++)
        {
            EnsurePanelController();
            if (panelController != null && panelController.ShowDataSourceError(error))
            {
                yield break;
            }

            yield return null;
        }

        Debug.LogWarning("PanelController was not ready. Portfolio load failure was not shown on screen.");
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
