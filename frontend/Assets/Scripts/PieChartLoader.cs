using System;
using UnityEngine;
using System.Collections;
using UnityEngine.UIElements;

public class PieChartLoader : MonoBehaviour
{
    // 번들을 서빙하는 nginx(8080)와 backend(8000)는 같은 호스트에서 뜬다. 절대 URL을
    // 박아 두면 localhost가 아닌 주소(예: Tailscale 홈서버)로 열었을 때 브라우저가
    // 자기 자신이 아닌 곳을 찌르므로, 페이지 오리진에서 호스트를 물려받고 포트만 바꾼다.
    private const int BackendPort = 8000;

    // 에디터·비 WebGL 실행에서는 Application.absoluteURL이 비어 있으므로 로컬 backend로 폴백한다.
    private const string FallbackApiBaseUrl = "http://localhost:8000";

    private ApiClient apiClient;
    private PieChart pieChart;
    [SerializeField] private PanelController panelController;

    void Awake()
    {
        apiClient = new ApiClient(ResolveApiBaseUrl());
        pieChart = FindAnyObjectByType<PieChart>();
        EnsurePanelController();
    }

    static string ResolveApiBaseUrl()
    {
        string pageUrl = Application.absoluteURL;
        if (string.IsNullOrEmpty(pageUrl))
            return FallbackApiBaseUrl;

        if (!Uri.TryCreate(pageUrl, UriKind.Absolute, out Uri pageUri))
            return FallbackApiBaseUrl;

        // file://로 연 경우 Host가 비어 있다. 그때는 폴백이 유일하게 의미 있는 값이다.
        if (pageUri.Scheme != Uri.UriSchemeHttp && pageUri.Scheme != Uri.UriSchemeHttps)
            return FallbackApiBaseUrl;
        if (string.IsNullOrEmpty(pageUri.Host))
            return FallbackApiBaseUrl;

        // Uri.Host는 IPv6 리터럴을 대괄호까지 포함해 돌려주므로 그대로 이어 붙여도 된다.
        return $"{pageUri.Scheme}://{pageUri.Host}:{BackendPort}";
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
