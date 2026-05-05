// FinUsDashboardUiController.cs — UI Toolkit(UXML/USS) 요소를 찾아 바인딩하고, 버튼 이벤트에서 API 코루틴을 돌린 뒤 라벨을 갱신한다.
// 흐름: OnEnable에서 root.Q로 컨트롤 참조 확보 → 클릭 시 입력 검증 → StartCoroutine → FinUsApiClient → 파싱·문자열 빌드 → SetSuccess/SetError.

using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using UnityEngine;
using UnityEngine.UIElements;

[RequireComponent(typeof(UIDocument))]
public class FinUsDashboardUiController : MonoBehaviour
{
    [SerializeField] private string apiBaseUrl = "http://localhost:8000";
    private ApiClient apiClient;

    private TextField stockInput;
    private DropdownField providerDropdown;
    private Button dataOnlyButton;
    private Button analyzeButton;

    private Label statusLabel;
    private Label stockHeaderLabel;
    private Label decisionLabel;
    private Label confidenceLabel;
    private Label summaryLabel;
    private Label reasonLabel;
    private Label trendLabel;
    private Label newsLabel;

    // 드롭다운에 보이는 문구(labels)와 실제 쿼리로 보낼 값(values)을 분리한다.
    private readonly List<string> providerValues = new List<string> { "openai", "anthropic" };
    private readonly List<string> providerLabels = new List<string> { "GPT-5.4-mini", "Claude-Sonnet-4-6" };

    private void OnEnable()
    {
        var document = GetComponent<UIDocument>();
        var root = document.rootVisualElement;

        stockInput = root.Q<TextField>("stock-input");
        providerDropdown = root.Q<DropdownField>("provider-dropdown");
        dataOnlyButton = root.Q<Button>("data-only-button");
        analyzeButton = root.Q<Button>("analyze-button");

        statusLabel = root.Q<Label>("status-label");
        stockHeaderLabel = root.Q<Label>("stock-header-label");
        decisionLabel = root.Q<Label>("decision-label");
        confidenceLabel = root.Q<Label>("confidence-label");
        summaryLabel = root.Q<Label>("summary-label");
        reasonLabel = root.Q<Label>("reason-label");
        trendLabel = root.Q<Label>("trend-label");
        newsLabel = root.Q<Label>("news-label");

        providerDropdown.choices = providerLabels;
        providerDropdown.index = 0;
        apiClient = new ApiClient(apiBaseUrl);

        dataOnlyButton.clicked += OnDataOnlyClicked;
        analyzeButton.clicked += OnAnalyzeClicked;

        SetIdleState(); // 초기 문구·버튼 상태
    }

    private void OnDisable()
    {
        if (dataOnlyButton != null)
        {
            dataOnlyButton.clicked -= OnDataOnlyClicked;
        }

        if (analyzeButton != null)
        {
            analyzeButton.clicked -= OnAnalyzeClicked;
        }
    }

    private void OnDataOnlyClicked()
    {
        var stock = stockInput.value.Trim();
        if (string.IsNullOrWhiteSpace(stock))
        {
            SetError("종목명을 입력해 주세요.");
            return;
        }

        StartCoroutine(FetchDataOnly(stock)); // AI 없이 뉴스+트렌드만
    }

    private void OnAnalyzeClicked()
    {
        var stock = stockInput.value.Trim();
        if (string.IsNullOrWhiteSpace(stock))
        {
            SetError("종목명을 입력해 주세요.");
            return;
        }

        var providerIndex = Mathf.Clamp(providerDropdown.index, 0, providerValues.Count - 1);
        var provider = providerValues[providerIndex];
        StartCoroutine(FetchAnalysis(stock, provider)); // 단일 analyze 엔드포인트
    }

    private IEnumerator FetchDataOnly(string stock)
    {
        SetLoading("원시 데이터를 조회 중입니다...");
        DataOnlyResult result = null;
        string error = null;
        yield return apiClient.FetchDataOnly(
            stock,
            data => result = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        var parsedTrend = TrendParser.Parse(result.trendRaw); // 원문 문자열 → TrendItem 리스트
        RenderStockHeader(stock, parsedTrend.LastOrDefault());
        decisionLabel.text = "결정: (DATA ONLY 모드)";
        decisionLabel.style.color = new StyleColor(new Color(0.2f, 0.2f, 0.2f));
        confidenceLabel.text = "신뢰도: -";
        summaryLabel.text = "요약: AI 분석 없이 뉴스/트렌드 원문만 표시합니다.";
        reasonLabel.text = "근거: -";
        trendLabel.text = BuildTrendSummary(parsedTrend, result.trendRaw);
        newsLabel.text = BuildNewsSummary(result.newsItems);
        SetSuccess("데이터 조회 완료");
    }

    private IEnumerator FetchAnalysis(string stock, string provider)
    {
        SetLoading("AI 분석 중입니다...");
        AnalyzeData report = null;
        string error = null;
        yield return apiClient.FetchAnalysis(
            stock,
            provider,
            data => report = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        var trend = TrendParser.Parse(report.trading_trend);
        RenderStockHeader(stock, trend.LastOrDefault());

        decisionLabel.text = $"결정: {report.details.decision}";
        decisionLabel.style.color = new StyleColor(report.details.decision == "BUY"
            ? new Color32(225, 29, 72, 255)
            : report.details.decision == "SELL"
                ? new Color32(79, 70, 229, 255)
                : new Color32(71, 85, 105, 255));

        confidenceLabel.text = $"신뢰도: {(report.details.confidence_score * 100f):0}%";
        summaryLabel.text = $"요약: {report.summary}";
        reasonLabel.text = $"근거: {report.details.reason}";
        trendLabel.text = BuildTrendSummary(trend, report.trading_trend);
        newsLabel.text = BuildNewsSummary(report.source_news ?? new string[0]);
        SetSuccess($"분석 완료 ({provider})");
    }

    private void RenderStockHeader(string stock, TrendItem latest)
    {
        if (latest == null)
        {
            stockHeaderLabel.text = stock;
            return;
        }

        var arrow = latest.isUp ? "▲" : "▼";
        stockHeaderLabel.text = $"{stock}  |  {latest.price.ToString("N0", CultureInfo.GetCultureInfo("ko-KR"))}원  {arrow} {latest.changeVal} ({latest.changePct})";
    }

    private string BuildNewsSummary(string[] news)
    {
        if (news == null || news.Length == 0)
        {
            return "뉴스: 데이터 없음";
        }

        var sb = new StringBuilder();
        sb.AppendLine("뉴스:");
        for (var i = 0; i < Mathf.Min(news.Length, 6); i++)
        {
            sb.AppendLine($"- {news[i]}");
        }
        return sb.ToString();
    }

    private string BuildTrendSummary(List<TrendItem> trendItems, string rawTrend)
    {
        if (trendItems.Count == 0)
        {
            return $"트렌드 원문:\n{rawTrend}";
        }

        var latest = trendItems[trendItems.Count - 1];
        var lines = trendItems.TakeLast(Mathf.Min(5, trendItems.Count))
            .Select(t => $"{t.date} | 종가 {t.price:N0} | 외인 {t.foreigner:N0} | 기관 {t.institution:N0} | 거래량 {t.volume:N0}");
        return $"트렌드(최근 {Mathf.Min(5, trendItems.Count)}일):\n{string.Join("\n", lines)}\n\n최신 변동: {latest.changeVal} ({latest.changePct})";
    }

    private void SetIdleState()
    {
        statusLabel.text = "종목명 입력 후 DATA ONLY 또는 ANALYZE를 누르세요.";
        statusLabel.style.color = new StyleColor(new Color32(79, 70, 229, 255));
        stockHeaderLabel.text = "Ready to Analyze Market Data";
        decisionLabel.text = "결정: -";
        decisionLabel.style.color = new StyleColor(new Color32(30, 41, 59, 255));
        confidenceLabel.text = "신뢰도: -";
        summaryLabel.text = "요약: 분석 결과가 여기에 표시됩니다.";
        reasonLabel.text = "근거: -";
        trendLabel.text = "트렌드: -";
        newsLabel.text = "뉴스: -";
        SetButtonsEnabled(true);
    }

    private void SetLoading(string message)
    {
        statusLabel.text = message;
        statusLabel.style.color = new StyleColor(new Color32(79, 70, 229, 255));
        SetButtonsEnabled(false);
    }

    private void SetSuccess(string message)
    {
        statusLabel.text = message;
        statusLabel.style.color = new StyleColor(new Color32(22, 163, 74, 255));
        SetButtonsEnabled(true);
    }

    private void SetError(string message)
    {
        statusLabel.text = message;
        statusLabel.style.color = new StyleColor(new Color32(225, 29, 72, 255));
        SetButtonsEnabled(true);
    }

    private void SetButtonsEnabled(bool enabled)
    {
        dataOnlyButton.SetEnabled(enabled);
        analyzeButton.SetEnabled(enabled);
    }

}
