using System.Collections;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using UnityEngine;
using UnityEngine.UIElements;

[RequireComponent(typeof(UIDocument))]
public class DashboardUiController : MonoBehaviour
{
    [SerializeField] private string apiBaseUrl = "http://localhost:8787";
    [SerializeField] private Font uiFont;

    private ApiClient apiClient;

    private TextField stockInput;
    private DropdownField providerSelect;
    private Button fetchNewsButton;
    private Button fetchTrendButton;
    private Button fetchBalanceButton;
    private Button analyzeButton;

    private VisualElement statusBar;
    private Label statusLabel;
    private Label errorLabel;

    private VisualElement summaryCard;
    private VisualElement decisionCard;
    private Label summaryLabel;
    private Label decisionBadge;
    private Label confidenceTextLabel;
    private VisualElement reasonPanel;
    private Label reasonLabel;
    private VisualElement trendPanel;
    private VisualElement sourceNewsPanel;
    private VisualElement balancePanel;
    private Label trendTextLabel;
    private TextField balanceReportField;

    private ListView sourceNewsListView;
    private Label sourceNewsLabelFallback;
    private readonly List<string> sourceNewsItems = new List<string>();

    private readonly List<string> defaultProviders = new List<string> { "openai", "anthropic" };

    private void OnEnable()
    {
        var root = GetComponent<UIDocument>().rootVisualElement;
        ApplyUiFont(root);

        stockInput = root.Q<TextField>("stock-input");
        providerSelect = root.Q<DropdownField>("provider-select");
        fetchNewsButton = root.Q<Button>("fetch-news-button");
        fetchTrendButton = root.Q<Button>("fetch-trend-button");
        fetchBalanceButton = root.Q<Button>("fetch-balance-button");
        analyzeButton = root.Q<Button>("analyze-button");

        statusBar = root.Q<VisualElement>("status-bar");
        statusLabel = root.Q<Label>("status-label");
        errorLabel = root.Q<Label>("error-label");

        summaryCard = root.Q<VisualElement>("summary-card");
        decisionCard = root.Q<VisualElement>("decision-card");
        summaryLabel = root.Q<Label>("summary-label");
        decisionBadge = root.Q<Label>("decision-badge");
        confidenceTextLabel = root.Q<Label>("confidence-text-label");
        reasonPanel = root.Q<VisualElement>("reason-panel");
        reasonLabel = root.Q<Label>("reason-label");
        trendPanel = root.Q<VisualElement>("trend-panel");
        sourceNewsPanel = root.Q<VisualElement>("source-news-panel");
        balancePanel = root.Q<VisualElement>("balance-panel");
        trendTextLabel = root.Q<Label>("trend-text-label");
        balanceReportField = root.Q<TextField>("balance-report-field");

        sourceNewsListView = root.Q<ListView>("source-news-list");
        sourceNewsLabelFallback = root.Q<Label>("source-news-list");
        ConfigureNewsListView();

        if (providerSelect != null)
        {
            if (providerSelect.choices == null || providerSelect.choices.Count == 0)
            {
                providerSelect.choices = defaultProviders;
            }

            providerSelect.index = providerSelect.choices.IndexOf("openai");
            if (providerSelect.index < 0)
            {
                providerSelect.index = 0;
            }
        }

        if (stockInput != null)
        {
            stockInput.RegisterCallback<FocusInEvent>(OnStockInputFocused);
        }

        if (balanceReportField != null)
        {
            balanceReportField.multiline = true;
            balanceReportField.isReadOnly = true;
        }

        apiClient = new ApiClient(apiBaseUrl);

        RegisterButtonCallbacks();
        SetIdleState();
    }

    private void OnDisable()
    {
        if (stockInput != null)
        {
            stockInput.UnregisterCallback<FocusInEvent>(OnStockInputFocused);
        }

        UnregisterButtonCallbacks();
    }

    private void OnStockInputFocused(FocusInEvent evt)
    {
#if UNITY_WEBGL && !UNITY_EDITOR
        RequestWebTextInput(stockInput?.value ?? string.Empty);
#endif
    }

    public void OnWebStockInputChanged(string value)
    {
        if (stockInput != null)
        {
            stockInput.value = value ?? string.Empty;
        }
    }

#if UNITY_WEBGL && !UNITY_EDITOR
    [DllImport("__Internal")]
    private static extern void FinUsPromptTextInput(string gameObjectName, string currentValue);

    private void RequestWebTextInput(string currentValue)
    {
        FinUsPromptTextInput(gameObject.name, currentValue ?? string.Empty);
    }
#endif

    private void RegisterButtonCallbacks()
    {
        if (fetchNewsButton != null) fetchNewsButton.clicked += OnFetchNewsClicked;
        if (fetchTrendButton != null) fetchTrendButton.clicked += OnFetchTrendClicked;
        if (fetchBalanceButton != null) fetchBalanceButton.clicked += OnFetchBalanceClicked;
        if (analyzeButton != null) analyzeButton.clicked += OnAnalyzeClicked;
    }

    private void UnregisterButtonCallbacks()
    {
        if (fetchNewsButton != null) fetchNewsButton.clicked -= OnFetchNewsClicked;
        if (fetchTrendButton != null) fetchTrendButton.clicked -= OnFetchTrendClicked;
        if (fetchBalanceButton != null) fetchBalanceButton.clicked -= OnFetchBalanceClicked;
        if (analyzeButton != null) analyzeButton.clicked -= OnAnalyzeClicked;
    }

    private void ConfigureNewsListView()
    {
        if (sourceNewsListView == null)
        {
            return;
        }

        sourceNewsListView.makeItem = () =>
        {
            var label = new Label();
            ApplyUiFont(label);
            return label;
        };
        sourceNewsListView.bindItem = (element, index) =>
        {
            if (element is Label label)
            {
                ApplyUiFont(label);
                label.text = sourceNewsItems[index];
            }
        };
        sourceNewsListView.itemsSource = sourceNewsItems;
        sourceNewsListView.Rebuild();
    }

    private void ApplyUiFont(VisualElement element)
    {
        if (uiFont == null || element == null)
        {
            return;
        }

        element.style.unityFont = uiFont;
        element.style.unityFontDefinition = FontDefinition.FromFont(uiFont);

        for (var i = 0; i < element.childCount; i++)
        {
            ApplyUiFont(element[i]);
        }
    }

    private void OnFetchNewsClicked()
    {
        var stock = GetStockOrNull();
        if (stock == null)
        {
            return;
        }

        StartCoroutine(FetchNewsRoutine(stock));
    }

    private void OnFetchTrendClicked()
    {
        var stock = GetStockOrNull();
        if (stock == null)
        {
            return;
        }

        StartCoroutine(FetchTrendRoutine(stock));
    }

    private void OnFetchBalanceClicked()
    {
        StartCoroutine(FetchBalanceRoutine());
    }

    private void OnAnalyzeClicked()
    {
        var stock = GetStockOrNull();
        if (stock == null)
        {
            return;
        }

        var provider = "openai";
        if (providerSelect != null && providerSelect.choices != null && providerSelect.choices.Count > 0)
        {
            var index = Mathf.Clamp(providerSelect.index, 0, providerSelect.choices.Count - 1);
            provider = providerSelect.choices[index];
        }

        StartCoroutine(FetchAnalyzeRoutine(stock, provider));
    }

    private string GetStockOrNull()
    {
        var stock = stockInput?.value?.Trim();
        if (!string.IsNullOrWhiteSpace(stock))
        {
            return stock;
        }

        SetError("종목(stock)을 입력해 주세요.");
        return null;
    }

    private IEnumerator FetchNewsRoutine(string stock)
    {
        SetLoading("뉴스를 조회 중입니다...");
        string[] news = null;
        string error = null;

        yield return apiClient.FetchNews(
            stock,
            data => news = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        SetNewsItems(news ?? new string[0]);
        SetSuccess("뉴스 조회 완료");
    }

    private IEnumerator FetchTrendRoutine(string stock)
    {
        SetLoading("트렌드를 조회 중입니다...");
        string trend = null;
        string error = null;

        yield return apiClient.FetchTrend(
            stock,
            data => trend = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        if (trendTextLabel != null)
        {
            trendTextLabel.text = trend ?? string.Empty;
        }
        SetSectionVisible(trendPanel, !string.IsNullOrWhiteSpace(trend));

        SetSuccess("트렌드 조회 완료");
    }

    private IEnumerator FetchBalanceRoutine()
    {
        SetLoading("잔고 리포트를 조회 중입니다...");
        string report = null;
        string error = null;

        yield return apiClient.FetchBalance(
            data => report = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        if (balanceReportField != null)
        {
            balanceReportField.value = report ?? string.Empty;
        }
        SetSectionVisible(balancePanel, !string.IsNullOrWhiteSpace(report));

        SetSuccess("잔고 리포트 조회 완료");
    }

    private IEnumerator FetchAnalyzeRoutine(string stock, string provider)
    {
        SetLoading("AI 분석을 실행 중입니다...");
        AnalyzeData analyze = null;
        string error = null;

        yield return apiClient.FetchAnalysis(
            stock,
            provider,
            data => analyze = data,
            message => error = message);

        if (!string.IsNullOrEmpty(error))
        {
            SetError(error);
            yield break;
        }

        if (analyze == null)
        {
            SetError("분석 결과가 비어 있습니다.");
            yield break;
        }

        ApplyAnalyzeResult(analyze);
        SetSuccess($"분석 완료 ({provider})");
    }

    private void ApplyAnalyzeResult(AnalyzeData analyze)
    {
        analyze = RecoverAnalyzeResult(analyze);

        var hasSummary = !string.IsNullOrWhiteSpace(analyze.summary);
        SetSectionVisible(summaryCard, hasSummary);

        if (summaryLabel != null)
        {
            summaryLabel.text = analyze.summary ?? string.Empty;
        }

        var details = analyze.details;
        var hasDecision = details != null && !string.IsNullOrWhiteSpace(details.decision);
        var hasReason = details != null && !string.IsNullOrWhiteSpace(details.reason);
        SetSectionVisible(decisionCard, hasDecision || details != null);
        SetSectionVisible(reasonPanel, hasReason);

        if (decisionBadge != null)
        {
            decisionBadge.text = hasDecision ? details.decision : string.Empty;
        }

        var confidence = details == null ? 0f : Mathf.Clamp01(details.confidence_score);
        if (confidenceTextLabel != null)
        {
            confidenceTextLabel.text = $"{confidence:P0}";
        }

        if (reasonLabel != null)
        {
            reasonLabel.text = hasReason ? details.reason : string.Empty;
        }

        var hasTrend = !string.IsNullOrWhiteSpace(analyze.trading_trend);
        SetSectionVisible(trendPanel, hasTrend);
        if (trendTextLabel != null)
        {
            trendTextLabel.text = analyze.trading_trend ?? string.Empty;
        }

        SetNewsItems(FilterSummaryDuplicates(analyze.source_news, analyze.summary));
    }

    private static AnalyzeData RecoverAnalyzeResult(AnalyzeData analyze)
    {
        if (analyze == null)
        {
            return null;
        }

        var recovered = TryParseAnalyzeJson(analyze.summary);
        return recovered ?? analyze;
    }

    private static AnalyzeData TryParseAnalyzeJson(string rawText)
    {
        if (string.IsNullOrWhiteSpace(rawText)
            || !rawText.Contains("\"summary\"")
            || !rawText.Contains("\"details\""))
        {
            return null;
        }

        for (var i = 0; i < rawText.Length; i++)
        {
            if (rawText[i] != '{')
            {
                continue;
            }

            var candidate = ExtractJsonObject(rawText, i);
            if (string.IsNullOrWhiteSpace(candidate)
                || !candidate.Contains("\"summary\"")
                || !candidate.Contains("\"details\""))
            {
                continue;
            }

            AnalyzeData parsed = null;
            try
            {
                parsed = JsonUtility.FromJson<AnalyzeData>(candidate);
            }
            catch (System.ArgumentException)
            {
                continue;
            }

            if (parsed != null && (parsed.details != null || !string.IsNullOrWhiteSpace(parsed.summary)))
            {
                return parsed;
            }
        }

        return null;
    }

    private static string ExtractJsonObject(string text, int startIndex)
    {
        var depth = 0;
        var inString = false;
        var escaping = false;

        for (var i = startIndex; i < text.Length; i++)
        {
            var ch = text[i];

            if (inString)
            {
                if (escaping)
                {
                    escaping = false;
                }
                else if (ch == '\\')
                {
                    escaping = true;
                }
                else if (ch == '"')
                {
                    inString = false;
                }

                continue;
            }

            if (ch == '"')
            {
                inString = true;
            }
            else if (ch == '{')
            {
                depth++;
            }
            else if (ch == '}')
            {
                depth--;
                if (depth == 0)
                {
                    return text.Substring(startIndex, i - startIndex + 1);
                }
            }
        }

        return null;
    }

    private void SetNewsItems(IReadOnlyList<string> items)
    {
        sourceNewsItems.Clear();
        for (var i = 0; i < items.Count; i++)
        {
            sourceNewsItems.Add(items[i]);
        }

        if (sourceNewsListView != null)
        {
            sourceNewsListView.Rebuild();
        }

        SetSectionVisible(sourceNewsPanel, sourceNewsItems.Count > 0);

        if (sourceNewsLabelFallback != null)
        {
            sourceNewsLabelFallback.text = sourceNewsItems.Count == 0 ? string.Empty : string.Join("\n", sourceNewsItems);
        }
    }

    private static IReadOnlyList<string> FilterSummaryDuplicates(IReadOnlyList<string> items, string summary)
    {
        if (items == null || items.Count == 0)
        {
            return new string[0];
        }

        var filteredItems = new List<string>();
        var normalizedSummary = NormalizeForComparison(summary);

        for (var i = 0; i < items.Count; i++)
        {
            var item = items[i];
            if (string.IsNullOrWhiteSpace(item))
            {
                continue;
            }

            var normalizedItem = NormalizeForComparison(item);
            if (IsSummaryDuplicate(normalizedItem, normalizedSummary))
            {
                continue;
            }

            filteredItems.Add(item);
        }

        return filteredItems;
    }

    private static bool IsSummaryDuplicate(string normalizedItem, string normalizedSummary)
    {
        if (string.IsNullOrEmpty(normalizedItem) || string.IsNullOrEmpty(normalizedSummary))
        {
            return false;
        }

        return normalizedItem == normalizedSummary
            || normalizedSummary.Contains(normalizedItem)
            || normalizedItem.Contains(normalizedSummary);
    }

    private static string NormalizeForComparison(string value)
    {
        return string.IsNullOrWhiteSpace(value)
            ? string.Empty
            : string.Join(" ", value.Split()).Trim();
    }

    private void SetIdleState()
    {
        if (statusLabel != null) statusLabel.text = "대기 중";
        if (errorLabel != null) errorLabel.text = string.Empty;
        HideResultSections();
        SetButtonsEnabled(true);
    }

    private void SetLoading(string message)
    {
        if (statusLabel != null) statusLabel.text = message;
        if (errorLabel != null) errorLabel.text = string.Empty;
        HideResultSections();
        SetButtonsEnabled(false);
    }

    private void SetSuccess(string message)
    {
        if (statusLabel != null) statusLabel.text = message;
        if (errorLabel != null) errorLabel.text = string.Empty;
        SetButtonsEnabled(true);
    }

    private void SetError(string message)
    {
        if (statusLabel != null) statusLabel.text = "error";
        if (errorLabel != null) errorLabel.text = message;
        SetButtonsEnabled(true);
    }

    private void SetButtonsEnabled(bool enabled)
    {
        fetchNewsButton?.SetEnabled(enabled);
        fetchTrendButton?.SetEnabled(enabled);
        fetchBalanceButton?.SetEnabled(enabled);
        analyzeButton?.SetEnabled(enabled);
        if (statusBar != null) statusBar.SetEnabled(true);
    }

    private void HideResultSections()
    {
        SetSectionVisible(summaryCard, false);
        SetSectionVisible(decisionCard, false);
        SetSectionVisible(reasonPanel, false);
        SetSectionVisible(trendPanel, false);
        SetSectionVisible(sourceNewsPanel, false);
        SetSectionVisible(balancePanel, false);
    }

    private static void SetSectionVisible(VisualElement section, bool visible)
    {
        if (section == null)
        {
            return;
        }

        section.style.display = visible ? DisplayStyle.Flex : DisplayStyle.None;
    }
}
