using UnityEngine;
using UnityEngine.UIElements;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

public class PanelController : MonoBehaviour
{
    [SerializeField] private Camera clickCamera;
    [SerializeField] private float clickDragThreshold = 8f;

    private Label stockName;
    private Label currentPrice;
    private Label avgPrice;
    private Label returnRate;
    private Label totalAsset;
    private Label totalReturnRate;
    private Label quantity;
    private Label dataSourceBanner;

    // 실데이터 로드에 실패해 샘플 포트폴리오를 그린 상태인지(#244).
    // true이면 배너를 띄우고 숫자 라벨에도 [샘플] 표시를 붙인다.
    private bool isSampleData;
    private string dataSourceMessage;

    private bool isPointerDown;
    private Vector2 pointerDownPosition;
    private UIDocument document;
    private PieSliceClickHandler hoveredSlice;

    void OnEnable()
    {
        EnsureBindings();
    }

    // 반환값은 "숫자 라벨이 준비됐는가"만 뜻한다. 배너는 표시 전용이라 이 조건에 넣지
    // 않는다 — 배너 생성이 실패해도 UpdatePanel·UpdateTopBar는 계속 돌아야 한다.
    // 배너 준비 여부는 SetDataSourceMessage가 따로 판정해 호출자에게 알린다(#262 리뷰).
    bool EnsureBindings()
    {
        if (stockName != null &&
            currentPrice != null &&
            avgPrice != null &&
            returnRate != null &&
            totalAsset != null &&
            totalReturnRate != null &&
            quantity != null)
        {
            return true;
        }

        document ??= GetComponent<UIDocument>();
        if (document == null || document.rootVisualElement == null)
            return false;

        VisualElement root = document.rootVisualElement;
        stockName = root.Q<Label>("stock-name");
        currentPrice = root.Q<Label>("current-price");
        avgPrice = root.Q<Label>("avg-price");
        returnRate = root.Q<Label>("return-rate");
        totalAsset = root.Q<Label>("total-asset");
        totalReturnRate = root.Q<Label>("total-return-rate");
        quantity = root.Q<Label>("quantity");

        if (stockName == null ||
            currentPrice == null ||
            avgPrice == null ||
            returnRate == null ||
            totalAsset == null ||
            totalReturnRate == null ||
            quantity == null)
        {
            BuildFallbackUi(root);
        }

        // 배너는 구 UXML 템플릿에 없을 수 있으므로 없으면 코드로 만들어 붙인다.
        // BuildFallbackUi가 root를 비우므로 반드시 그 뒤에 잡는다.
        dataSourceBanner = root.Q<Label>("data-source-banner") ?? CreateDataSourceBanner(root);
        RefreshDataSourceBanner();

        return stockName != null &&
            currentPrice != null &&
            avgPrice != null &&
            returnRate != null &&
            totalAsset != null &&
            totalReturnRate != null &&
            quantity != null;
    }

    // 라벨이 이미 바인딩된 뒤(EnsureBindings가 곧바로 true를 반환하는 경로)에는 위 바인딩
    // 패스가 돌지 않으므로, 배너만 따로 붙일 자리가 필요하다.
    void EnsureDataSourceBanner()
    {
        if (dataSourceBanner != null)
            return;

        VisualElement root = document == null ? null : document.rootVisualElement;
        if (root == null)
            return;

        dataSourceBanner = root.Q<Label>("data-source-banner") ?? CreateDataSourceBanner(root);
    }

    Label CreateDataSourceBanner(VisualElement root)
    {
        if (root == null)
            return null;

        // 화면 하단 전체 폭. 상단은 top-bar·panel이 이미 차지하고 있어 가린다.
        Label banner = new Label { name = "data-source-banner" };
        banner.style.position = Position.Absolute;
        banner.style.left = 0;
        banner.style.right = 0;
        banner.style.bottom = 0;
        banner.style.paddingTop = 14;
        banner.style.paddingRight = 20;
        banner.style.paddingBottom = 14;
        banner.style.paddingLeft = 20;
        banner.style.backgroundColor = new Color(0.72f, 0.11f, 0.11f, 0.92f);
        banner.style.color = Color.white;
        banner.style.fontSize = 28;
        banner.style.unityTextAlign = TextAnchor.MiddleCenter;
        // 오류 메시지에 url·status가 붙어 길어지므로 줄바꿈을 허용한다.
        banner.style.whiteSpace = WhiteSpace.Normal;
        banner.style.display = DisplayStyle.None;
        root.Add(banner);
        return banner;
    }

    // 실데이터 대신 샘플 포트폴리오를 그렸음을 화면에 알린다(#244).
    // UI가 아직 준비되지 않았으면 false — 호출자가 다음 프레임에 다시 부르면 된다.
    public bool ShowSampleDataNotice(string error)
    {
        isSampleData = true;
        return SetDataSourceMessage($"⚠ 실데이터 연결 실패 — 이 화면은 실제 자산이 아닌 샘플 데이터입니다\n{error}");
    }

    // 샘플조차 없어 아무것도 그리지 못한 경우.
    public bool ShowDataSourceError(string error)
    {
        isSampleData = false;
        return SetDataSourceMessage($"⚠ 실데이터 연결 실패 — 포트폴리오를 표시할 수 없습니다\n{error}");
    }

    // 실데이터를 (다시) 받았을 때 배너와 [샘플] 표시를 걷는다.
    public bool ClearDataSourceNotice()
    {
        isSampleData = false;
        return SetDataSourceMessage(null);
    }

    bool SetDataSourceMessage(string message)
    {
        dataSourceMessage = message;

        // 이미 바인딩이 끝난 뒤라면 EnsureBindings가 곧바로 true를 반환하므로,
        // 배너 갱신은 여기서 따로 호출해야 한다.
        if (!EnsureBindings())
            return false;

        EnsureDataSourceBanner();
        RefreshDataSourceBanner();

        // 배너가 없으면 메시지를 화면에 남기지 못한 것이다. false를 돌려 호출자의
        // 재시도 루프가 다음 프레임에 다시 시도하게 한다.
        return dataSourceBanner != null;
    }

    void RefreshDataSourceBanner()
    {
        if (dataSourceBanner == null)
            return;

        bool hasMessage = !string.IsNullOrEmpty(dataSourceMessage);
        dataSourceBanner.text = hasMessage ? dataSourceMessage : string.Empty;
        dataSourceBanner.style.display = hasMessage ? DisplayStyle.Flex : DisplayStyle.None;
    }

    // 샘플 데이터일 때 종목명과 상단바(총자산·총수익률)에 [샘플]을 붙인다.
    // 배너를 놓치거나 스크린샷으로 잘려 나가도 실제 잔고로 오해되지 않게 하기 위함이다.
    // 패널의 개별 수치(현재가·평단가·수익률·수량)에는 붙이지 않는다 — 이미 [샘플] 표시가
    // 붙은 종목명 바로 아래라 맥락이 분명하고, 라벨마다 붙이면 시끄럽기만 하다.
    string MarkSample(string text)
    {
        return isSampleData ? $"[샘플] {text}" : text;
    }

    void BuildFallbackUi(VisualElement root)
    {
        root.Clear();
        root.style.flexGrow = 1;
        root.style.paddingTop = 24;
        root.style.paddingRight = 24;
        root.style.paddingBottom = 24;
        root.style.paddingLeft = 24;

        VisualElement topBar = new VisualElement { name = "top-bar" };
        topBar.style.position = Position.Absolute;
        topBar.style.left = 24;
        topBar.style.top = 24;
        topBar.style.paddingTop = 10;
        topBar.style.paddingRight = 14;
        topBar.style.paddingBottom = 10;
        topBar.style.paddingLeft = 14;
        topBar.style.backgroundColor = new Color(0.93f, 0.93f, 0.93f, 0.78f);
        root.Add(topBar);

        totalAsset = CreateLabel("total-asset", "총자산: -", 30);
        totalReturnRate = CreateLabel("total-return-rate", "총수익률: -", 30);
        topBar.Add(totalAsset);
        topBar.Add(totalReturnRate);

        VisualElement panel = new VisualElement { name = "panel" };
        panel.style.position = Position.Absolute;
        panel.style.right = 24;
        panel.style.top = 24;
        panel.style.width = 260;
        panel.style.paddingTop = 14;
        panel.style.paddingRight = 16;
        panel.style.paddingBottom = 14;
        panel.style.paddingLeft = 16;
        panel.style.backgroundColor = new Color(0.93f, 0.93f, 0.93f, 0.78f);
        root.Add(panel);

        stockName = CreateLabel("stock-name", "종목 선택", 40);
        currentPrice = CreateLabel("current-price", "현재가: -", 30);
        avgPrice = CreateLabel("avg-price", "평단가: -", 30);
        quantity = CreateLabel("quantity", "보유 수량: -", 30);
        returnRate = CreateLabel("return-rate", "수익률: -", 30);

        panel.Add(stockName);
        panel.Add(currentPrice);
        panel.Add(avgPrice);
        panel.Add(quantity);
        panel.Add(returnRate);
    }

    Label CreateLabel(string name, string text, int fontSize)
    {
        Label label = new Label(text) { name = name };
        label.style.fontSize = fontSize;
        label.style.color = Color.black;
        label.style.marginBottom = 6;
        return label;
    }

    void Update()
    {
        UpdateHover();

        if (TryGetPointerDown(out Vector2 downPosition))
        {
            isPointerDown = true;
            pointerDownPosition = downPosition;
            return;
        }

        if (!isPointerDown)
            return;

        if (!TryGetPointerUp(out Vector2 upPosition))
            return;

        isPointerDown = false;
        if (Vector2.Distance(pointerDownPosition, upPosition) > clickDragThreshold)
            return;

        TrySelectSlice(upPosition);
    }

    public void UpdatePanel(Holding holding)
    {
        if (holding == null)
            return;

        if (!EnsureBindings())
            return;

        stockName.text = MarkSample(holding.name);
        // price_known=false이면 current_price를 알 수 없다.
        // return_rate_known=false이면 return_rate를 알 수 없다(현재가는 알더라도 avg_price <= 0이면 수익률 계산 불가).
        // JsonUtility가 JSON null을 0으로 변환하므로 플래그로 구분해 "—"를 표시한다.
        currentPrice.text = holding.price_known
            ? $"현재가: {holding.current_price:N0}원"
            : "현재가: —";
        avgPrice.text = $"평단가: {holding.avg_price:N0}원";
        returnRate.text = holding.return_rate_known
            ? $"수익률: {holding.return_rate:F2}%"
            : "수익률: —";
        quantity.text = $"보유 수량: {holding.quantity:N0}주";
    }

    public bool UpdateTopBar(PortfolioData portfolioData)
    {
        if (portfolioData == null)
            return false;

        if (!EnsureBindings())
            return false;

        // total_asset_is_estimate=true이면 현재가 없는 종목이 매입가 기준으로 잡혀 있다.
        totalAsset.text = MarkSample(portfolioData.total_asset_is_estimate
            ? $"총자산: {portfolioData.total_asset:N0}원 (추정)"
            : $"총자산: {portfolioData.total_asset:N0}원");
        // total_return_rate_known=false이면 현재가가 없어 수익률을 계산할 수 없다.
        totalReturnRate.text = MarkSample(portfolioData.total_return_rate_known
            ? $"총수익률: {portfolioData.total_return_rate:F2}%"
            : "총수익률: —");
        return true;
    }

    void TrySelectSlice(Vector2 screenPosition)
    {
        Camera cameraToUse = clickCamera != null ? clickCamera : Camera.main;
        if (cameraToUse == null)
            return;

        Ray ray = cameraToUse.ScreenPointToRay(screenPosition);
        if (!Physics.Raycast(ray, out RaycastHit hit))
            return;

        PieSliceClickHandler handler = hit.collider.GetComponentInParent<PieSliceClickHandler>();
        if (handler?.Holding == null)
            return;

        UpdatePanel(handler.Holding);
    }

    void UpdateHover()
    {
        if (!TryGetPointerPosition(out Vector2 pointerPosition))
        {
            SetHoveredSlice(null);
            return;
        }

        SetHoveredSlice(GetSliceAt(pointerPosition));
    }

    PieSliceClickHandler GetSliceAt(Vector2 screenPosition)
    {
        Camera cameraToUse = clickCamera != null ? clickCamera : Camera.main;
        if (cameraToUse == null)
            return null;

        Ray ray = cameraToUse.ScreenPointToRay(screenPosition);
        if (!Physics.Raycast(ray, out RaycastHit hit))
            return null;

        return hit.collider.GetComponentInParent<PieSliceClickHandler>();
    }

    void SetHoveredSlice(PieSliceClickHandler nextHoveredSlice)
    {
        if (hoveredSlice == nextHoveredSlice)
            return;

        if (hoveredSlice != null)
            hoveredSlice.SetHovered(false);

        hoveredSlice = nextHoveredSlice;

        if (hoveredSlice != null)
            hoveredSlice.SetHovered(true);
    }

    bool TryGetPointerDown(out Vector2 position)
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null && Mouse.current.leftButton.wasPressedThisFrame)
        {
            position = Mouse.current.position.ReadValue();
            return true;
        }

        if (Touchscreen.current != null && Touchscreen.current.primaryTouch.press.wasPressedThisFrame)
        {
            position = Touchscreen.current.primaryTouch.position.ReadValue();
            return true;
        }
#endif

#if ENABLE_LEGACY_INPUT_MANAGER
        if (Input.GetMouseButtonDown(0))
        {
            position = Input.mousePosition;
            return true;
        }
#endif

        position = Vector2.zero;
        return false;
    }

    bool TryGetPointerUp(out Vector2 position)
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null && Mouse.current.leftButton.wasReleasedThisFrame)
        {
            position = Mouse.current.position.ReadValue();
            return true;
        }

        if (Touchscreen.current != null && Touchscreen.current.primaryTouch.press.wasReleasedThisFrame)
        {
            position = Touchscreen.current.primaryTouch.position.ReadValue();
            return true;
        }
#endif

#if ENABLE_LEGACY_INPUT_MANAGER
        if (Input.GetMouseButtonUp(0))
        {
            position = Input.mousePosition;
            return true;
        }
#endif

        position = Vector2.zero;
        return false;
    }

    bool TryGetPointerPosition(out Vector2 position)
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null)
        {
            position = Mouse.current.position.ReadValue();
            return true;
        }

        if (Touchscreen.current != null && Touchscreen.current.primaryTouch.press.isPressed)
        {
            position = Touchscreen.current.primaryTouch.position.ReadValue();
            return true;
        }
#endif

#if ENABLE_LEGACY_INPUT_MANAGER
        position = Input.mousePosition;
        return true;
#else
        position = Vector2.zero;
        return false;
#endif
    }

}
