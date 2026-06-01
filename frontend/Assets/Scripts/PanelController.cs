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

    private bool isPointerDown;
    private Vector2 pointerDownPosition;
    private UIDocument document;
    private PieSliceClickHandler hoveredSlice;

    void OnEnable()
    {
        EnsureBindings();
    }

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

        return stockName != null &&
            currentPrice != null &&
            avgPrice != null &&
            returnRate != null &&
            totalAsset != null &&
            totalReturnRate != null &&
            quantity != null;
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

        stockName.text = holding.name;
        currentPrice.text = $"현재가: {holding.current_price:N0}원";
        avgPrice.text = $"평단가: {holding.avg_price:N0}원";
        returnRate.text = $"수익률: {holding.return_rate:F2}%";
        quantity.text = $"보유 수량: {holding.quantity:N0}주";
    }

    public bool UpdateTopBar(PortfolioData portfolioData)
    {
        if (portfolioData == null)
            return false;

        if (!EnsureBindings())
            return false;

        totalAsset.text = $"총자산: {portfolioData.total_asset:N0}원";
        totalReturnRate.text = $"총수익률: {portfolioData.total_return_rate:F2}%";
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
