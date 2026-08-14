using UnityEngine;
using System.Collections;
using UnityEngine.UIElements;

public class PieChartLoader : MonoBehaviour
{
    private ApiClient apiClient;
    private PieChart pieChart;
    [SerializeField] private PanelController panelController;

    // 비워 두면 페이지와 같은 오리진을 쓴다(#246) — DashboardUiController와 같은 규칙이다.
    // nginx 프록시 없이 번들만 정적 서빙하는 경우(예: python -m http.server)에는 /api를
    // 중계할 상대가 없으므로, 그때만 인스펙터에서 백엔드 주소를 채워 탈출구로 쓴다.
    // 단 그 구성은 cross-origin이라 backend에 CORS 설정을 되살려야 한다.
    [SerializeField] private string apiBaseUrl = "";

    void Awake()
    {
        // 주소를 하드코딩하지 않는다(#246). 자세한 근거는 ApiClient.DefaultBaseUrl 주석 참고.
        apiClient = new ApiClient(string.IsNullOrWhiteSpace(apiBaseUrl) ? ApiClient.DefaultBaseUrl : apiBaseUrl);
        pieChart = FindAnyObjectByType<PieChart>();
        EnsurePanelController();
    }

    void Start()
    {
        StartCoroutine(apiClient.FetchPortfolio(
            onSuccess: (portfolioData) =>
            {
                // 차트를 못 그려도 실데이터는 받았으므로 상단바는 갱신한다.
                if (pieChart == null)
                {
                    Debug.LogError("PieChart를 찾지 못해 차트를 그리지 못했습니다.");
                }
                else
                {
                    pieChart.Generate(portfolioData);
                }

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

        // pieChart가 null이면 Generate에서 NRE가 나 코루틴이 끊기고 배너조차 못 뜬다.
        if (pieChart == null)
        {
            Debug.LogError("PieChart를 찾지 못해 차트를 그리지 못했습니다.");
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
        bool noticeShown = false;

        for (int i = 0; i < 30; i++)
        {
            EnsurePanelController();
            if (panelController != null)
            {
                if (sampleDataError == null)
                {
                    // 실데이터를 받았으므로 이전 실패 표시를 걷는다. 지금은 재조회 경로가
                    // 없지만, 새로고침이 붙었을 때 [샘플]이 남지 않게 한다.
                    panelController.ClearDataSourceNotice();
                }
                else if (!noticeShown)
                {
                    // 한 번 띄우면 다시 만들지 않는다 — 대기 루프가 최대 30프레임 돈다.
                    noticeShown = panelController.ShowSampleDataNotice(sampleDataError);
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
