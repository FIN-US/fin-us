using UnityEngine;

public class PieSliceClickHandler : MonoBehaviour
{
    public Holding Holding { get; private set; }
    private GameObject highlightObject;

    public void Initialize(Holding holding, GameObject hoverHighlight)
    {
        Holding = holding;
        highlightObject = hoverHighlight;
        SetHovered(false);
    }

    public void SetHovered(bool hovered)
    {
        if (highlightObject != null)
            highlightObject.SetActive(hovered);
    }
}
