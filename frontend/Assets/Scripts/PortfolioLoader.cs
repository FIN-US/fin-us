using UnityEngine;

public class PortfolioLoader : MonoBehaviour
{
    public PortfolioData data;

    void Start()
    {
        TextAsset json = Resources.Load<TextAsset>("data");
        data = JsonUtility.FromJson<PortfolioData>(json.text);
        FindAnyObjectByType<PanelController>().UpdateTopBar(data);

        Debug.Log("총자산: " + data.total_asset);
        foreach (var h in data.holdings)
            Debug.Log(h.name + " / 수익률: " + h.return_rate + " / 비중: " + h.GetWeight(data.total_asset));

        SpawnSpheres();
    }

    void SpawnSpheres()
    {
        int count = data.holdings.Length;
        float spacing = 3f;
        float startX = -(count - 1) * spacing / 2f;

        for (int i = 0; i < count; i++)
        {
            Holding h = data.holdings[i];
            float weight = h.GetWeight(data.total_asset);
            float size = Mathf.Lerp(0.5f, 2.5f, weight / 100f);

            GameObject sphere = GameObject.CreatePrimitive(PrimitiveType.Sphere);
            sphere.name = h.name;
            sphere.transform.position = new Vector3(startX + i * spacing, 0f, 0f);
            sphere.transform.localScale = Vector3.one * size;

            // 클릭에 반응
            SphereClickHandler handler = sphere.AddComponent<SphereClickHandler>();
            handler.holding = h;

            Material mat = new Material(Shader.Find("Universal Render Pipeline/Lit"));

            if (h.return_rate > 2f)
                mat.color = new Color(1f, 0.2f, 0.2f);
            else if (h.return_rate < -2f)
                mat.color = new Color(0.2f, 0.5f, 1f);
            else
                mat.color = new Color(0.7f, 0.7f, 0.7f);

            sphere.GetComponent<Renderer>().material = mat;
        }
    }
}