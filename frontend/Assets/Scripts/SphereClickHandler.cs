using UnityEngine;

public class SphereClickHandler : MonoBehaviour
{
    public Holding holding;
    private PanelController panelController;

    void Start()
    {
        panelController = FindAnyObjectByType<PanelController>();
        Debug.Log("PanelController 찾음: " + (panelController != null));
    }
}