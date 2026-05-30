using UnityEngine;

public class PieSliceClickHandler : MonoBehaviour
{
    public Holding Holding { get; private set; }

    public void Initialize(Holding holding)
    {
        Holding = holding;
    }
}
