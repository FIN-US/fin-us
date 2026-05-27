using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.UIElements;

public class PanelController : MonoBehaviour
{
    private Label stockName;
    private Label currentPrice;
    private Label avgPrice;
    private Label returnRate;
    private Label totalAsset;
    private Label totalReturnRate;
    private Label quantity;

    void Awake()
    {
        var root = GetComponent<UIDocument>().rootVisualElement;
        stockName = root.Q<Label>("stock-name");
        currentPrice = root.Q<Label>("current-price");
        avgPrice = root.Q<Label>("avg-price");
        returnRate = root.Q<Label>("return-rate");
        totalAsset = root.Q<Label>("total-asset");
        totalReturnRate = root.Q<Label>("total-return-rate");
        quantity = root.Q<Label>("quantity");
    }

    void Update()
    {
        if (Mouse.current.leftButton.wasPressedThisFrame)
        {
            Ray ray = Camera.main.ScreenPointToRay(Mouse.current.position.ReadValue());
            if (Physics.Raycast(ray, out RaycastHit hit))
            {
                SphereClickHandler handler = hit.collider.GetComponent<SphereClickHandler>();
                if (handler != null)
                {
                    Debug.Log("레이캐스트 클릭: " + handler.holding.name);
                    FindAnyObjectByType<PanelController>().UpdatePanel(handler.holding);
                }
            }
        }
    }

    public void UpdatePanel(Holding h)
    {
        stockName.text = h.name;
        currentPrice.text = "현재가: " + h.current_price.ToString("N0") + "원";
        avgPrice.text = "평단가: " + h.avg_price.ToString("N0") + "원";
        returnRate.text = "수익률: " + h.return_rate.ToString("F2") + "%";
        quantity.text = "보유수량: " + h.quantity + "주";
    }

    public void UpdateTopBar(PortfolioData data)
    {
        totalAsset.text = "총자산: " + data.total_asset.ToString("N0") + "원";
        totalReturnRate.text = "총수익률: " + data.total_return_rate.ToString("F2") + "%";
    }
}