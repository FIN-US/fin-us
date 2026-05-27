using System.Collections.Generic;
using UnityEngine;

public class PortfolioLoader : MonoBehaviour
{
    public PortfolioData data;
    public GameObject[] treePrefabs;

    [SerializeField] private float _lerpMin = 0.5f;
    [SerializeField] private float _lerpMax = 1.7f;

    void Awake()
    {
        TextAsset json = Resources.Load<TextAsset>("data");
        data = JsonUtility.FromJson<PortfolioData>(json.text);
    }

    void Start()
    {
        SpawnSpheres();
        FindAnyObjectByType<PanelController>().UpdateTopBar(data);
    }

    void SpawnSpheres()
    {
        float minX = 17.5f - 4f;
        float maxX = 17.5f + 4f;
        float minZ = 6.8f - 4f;
        float maxZ = 6.8f + 4f;
        float groundY = -13f;

        int count = data.holdings.Length;
        int cols = Mathf.CeilToInt(Mathf.Sqrt(count));
        int rows = Mathf.CeilToInt((float)count / cols);
        float cellW = (maxX - minX) / cols;
        float cellH = (maxZ - minZ) / rows;

        List<Vector3> placedPositions = new List<Vector3>();
        System.Array.Sort(data.holdings, (a, b) => a.GetWeight(data.total_asset).CompareTo(b.GetWeight(data.total_asset)));

        for (int i = 0; i < data.holdings.Length; i++)
        {
            Holding h = data.holdings[i];
            float weight = h.GetWeight(data.total_asset);
            float size = Mathf.Lerp(_lerpMin, _lerpMax, weight / 100f);

            int col = i % cols;
            int row = i / cols;

            float x, z;
            Vector3 pos;
            int attempts = 0;
            do
            {
                x = Random.Range(minX + col * cellW + 0.3f, minX + (col + 1) * cellW - 0.3f);
                z = Random.Range(minZ + row * cellH + 0.3f, minZ + (row + 1) * cellH - 0.3f);
                pos = new Vector3(x, groundY + size / 2f, z);
                attempts++;
            }
            while (IsTooClose(placedPositions, pos, size) && attempts < 100);

            placedPositions.Add(pos);

            GameObject tree = Instantiate(treePrefabs[i % treePrefabs.Length]);
            tree.name = h.name;
            tree.transform.position = pos;
            tree.transform.localScale = Vector3.one * size;

            Renderer[] renderers = tree.GetComponentsInChildren<Renderer>();
            foreach (Renderer r in renderers)
            {
                foreach (Material mat in r.materials)
                {
                    Color tint;
                    if (h.return_rate > 2f)
                        tint = new Color(1.0f, 0.1f, 0.1f);
                    else if (h.return_rate < -2f)
                        tint = new Color(0.2f, 0.2f, 0.8f);
                    else
                        tint = new Color(1.0f, 1.0f, 1.0f);

                    mat.SetColor("_BaseColor", tint);
                }
            }

            // 클릭에 반응
            SphereClickHandler handler = tree.AddComponent<SphereClickHandler>();
            handler.holding = h;
        }
    }

    bool IsTooClose(List<Vector3> placed, Vector3 newPos, float size)
    {
        foreach (var p in placed)
        {
            if (Vector3.Distance(p, newPos) < size + 0.5f)
                return true;
        }
        return false;
    }
}