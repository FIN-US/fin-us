using System.Collections.Generic;
using UnityEngine;

public class PieChart : MonoBehaviour
{
    public PortfolioData data;
    private List<GameObject> slices = new List<GameObject>();

    [SerializeField] private float heightScale = 0.5f;
    [SerializeField] private float maxHeight = 3f;
    [SerializeField] private float radius = 5f;
    [SerializeField] private Material sliceMaterial;
    [SerializeField] private Font labelFont;
    [SerializeField] private Camera labelCamera;
    [SerializeField] private float labelRadiusFactor = 0.65f;
    [SerializeField] private float labelRadiusStep = 0.16f;
    [SerializeField] private float smallSliceLabelBoost = 0.24f;
    [SerializeField] private float labelYOffset = 0.35f;
    [SerializeField] private float labelYStep = 0.14f;
    [SerializeField] private float labelCharacterSize = 0.22f;

    private static readonly int BaseColorId = Shader.PropertyToID("_BaseColor");
    private static readonly int ColorId = Shader.PropertyToID("_Color");
    private MaterialPropertyBlock propertyBlock;

    void Awake()
    {
        propertyBlock = new MaterialPropertyBlock();
    }

    public void Generate(PortfolioData portfolioData)
    {
        data = portfolioData;
        foreach (var s in slices)
            Destroy(s);
        slices.Clear();

        float total = 0;
        foreach (var h in data.holdings)
            total += h.current_price * h.quantity;

        if (total <= 0f)
            return;

        float startAngle = 0f;
        for (int i = 0; i < data.holdings.Length; i++)
        {
            Holding h = data.holdings[i];
            float weight = h.current_price * h.quantity / total;
            float angle = weight * 360f;
            float height = Mathf.Clamp(Mathf.Abs(h.return_rate) * heightScale, 0f, maxHeight);
            float yOffset = h.return_rate >= 0 ? 0f : -height;

            GameObject slice = CreateSlice(h, startAngle, angle, height, yOffset);
            CreateLabel(slice.transform, h.name, startAngle + angle * 0.5f, yOffset + height, weight, i);
            slices.Add(slice);
            startAngle += angle;
        }
    }

    GameObject CreateSlice(Holding holding, float startAngle, float angle, float height, float yOffset)
    {
        GameObject obj = new GameObject(holding.name);
        obj.transform.SetParent(transform, false);

        int steps = Mathf.Max(3, Mathf.RoundToInt(angle));
        float yBottom = yOffset;
        float yTop = yOffset + height;

        List<Vector3> vertices = new List<Vector3>();
        List<Vector3> normals = new List<Vector3>();
        List<int> triangles = new List<int>();

        AddBottomFace(vertices, normals, triangles, startAngle, angle, steps, yBottom);
        AddTopFace(vertices, normals, triangles, startAngle, angle, steps, yTop);
        AddArcSide(vertices, normals, triangles, startAngle, angle, steps, yBottom, yTop);
        AddStartSide(vertices, normals, triangles, startAngle, yBottom, yTop);
        AddEndSide(vertices, normals, triangles, startAngle + angle, yBottom, yTop);

        Mesh mesh = new()
        {
            vertices = vertices.ToArray(),
            triangles = triangles.ToArray(),
            normals = normals.ToArray()
        };
        mesh.RecalculateBounds();

        obj.AddComponent<MeshFilter>().mesh = mesh;

        MeshCollider meshCollider = obj.AddComponent<MeshCollider>();
        meshCollider.sharedMesh = mesh;

        PieSliceClickHandler clickHandler = obj.AddComponent<PieSliceClickHandler>();
        clickHandler.Initialize(holding);

        MeshRenderer meshRenderer = obj.AddComponent<MeshRenderer>();
        if (sliceMaterial == null)
        {
            Debug.LogError("PieChart sliceMaterial is not assigned.");
            return obj;
        }

        meshRenderer.sharedMaterial = sliceMaterial;
        propertyBlock ??= new MaterialPropertyBlock();
        propertyBlock.Clear();
        Color sliceColor = GetColor(holding.return_rate);
        propertyBlock.SetColor(BaseColorId, sliceColor);
        propertyBlock.SetColor(ColorId, sliceColor);
        meshRenderer.SetPropertyBlock(propertyBlock);

        return obj;
    }

    void CreateLabel(Transform sliceTransform, string text, float angle, float yTop, float weight, int index)
    {
        GameObject labelObject = new GameObject($"{text} Label");
        labelObject.transform.SetParent(sliceTransform, false);

        float a = Mathf.Deg2Rad * angle;
        int ring = index % 3;
        float smallSliceOffset = Mathf.Lerp(smallSliceLabelBoost, 0f, Mathf.Clamp01(weight / 0.12f));
        float labelRadius = radius * (labelRadiusFactor + smallSliceOffset + labelRadiusStep * ring);
        labelObject.transform.localPosition = new Vector3(
            Mathf.Cos(a) * labelRadius,
            yTop + labelYOffset + labelYStep * ring,
            Mathf.Sin(a) * labelRadius
        );

        PieChartLabel label = labelObject.AddComponent<PieChartLabel>();
        label.Initialize(text, labelFont, labelCamera, labelCharacterSize);
    }

    void AddBottomFace(List<Vector3> vertices, List<Vector3> normals, List<int> triangles, float startAngle, float angle, int steps, float yBottom)
    {
        int centerIndex = vertices.Count;
        vertices.Add(new Vector3(0, yBottom, 0));
        normals.Add(Vector3.down);

        int rimStart = vertices.Count;
        for (int i = 0; i <= steps; i++)
        {
            Vector3 point = GetRimPoint(startAngle, angle, steps, i, yBottom);
            vertices.Add(point);
            normals.Add(Vector3.down);
        }

        for (int i = 0; i < steps; i++)
        {
            triangles.Add(centerIndex);
            triangles.Add(rimStart + i);
            triangles.Add(rimStart + i + 1);
        }
    }

    void AddTopFace(List<Vector3> vertices, List<Vector3> normals, List<int> triangles, float startAngle, float angle, int steps, float yTop)
    {
        int centerIndex = vertices.Count;
        vertices.Add(new Vector3(0, yTop, 0));
        normals.Add(Vector3.up);

        int rimStart = vertices.Count;
        for (int i = 0; i <= steps; i++)
        {
            Vector3 point = GetRimPoint(startAngle, angle, steps, i, yTop);
            vertices.Add(point);
            normals.Add(Vector3.up);
        }

        for (int i = 0; i < steps; i++)
        {
            triangles.Add(centerIndex);
            triangles.Add(rimStart + i + 1);
            triangles.Add(rimStart + i);
        }
    }

    void AddArcSide(List<Vector3> vertices, List<Vector3> normals, List<int> triangles, float startAngle, float angle, int steps, float yBottom, float yTop)
    {
        int sideStart = vertices.Count;
        for (int i = 0; i <= steps; i++)
        {
            float a = Mathf.Deg2Rad * (startAngle + angle / steps * i);
            float nx = Mathf.Cos(a);
            float nz = Mathf.Sin(a);
            Vector3 outNormal = new Vector3(nx, 0, nz);

            vertices.Add(new Vector3(nx * radius, yBottom, nz * radius));
            normals.Add(outNormal);
            vertices.Add(new Vector3(nx * radius, yTop, nz * radius));
            normals.Add(outNormal);
        }

        for (int i = 0; i < steps; i++)
        {
            int b0 = sideStart + i * 2;
            int t0 = sideStart + i * 2 + 1;
            int b1 = sideStart + (i + 1) * 2;
            int t1 = sideStart + (i + 1) * 2 + 1;

            triangles.Add(b0);
            triangles.Add(t0);
            triangles.Add(b1);
            triangles.Add(t0);
            triangles.Add(t1);
            triangles.Add(b1);
        }
    }

    void AddStartSide(List<Vector3> vertices, List<Vector3> normals, List<int> triangles, float angle, float yBottom, float yTop)
    {
        float a = Mathf.Deg2Rad * angle;
        float x = Mathf.Cos(a) * radius;
        float z = Mathf.Sin(a) * radius;
        Vector3 outNormal = new Vector3(Mathf.Sin(a), 0, -Mathf.Cos(a));

        int sideStart = vertices.Count;
        vertices.Add(new Vector3(0, yBottom, 0));
        normals.Add(outNormal);
        vertices.Add(new Vector3(0, yTop, 0));
        normals.Add(outNormal);
        vertices.Add(new Vector3(x, yBottom, z));
        normals.Add(outNormal);
        vertices.Add(new Vector3(x, yTop, z));
        normals.Add(outNormal);

        triangles.Add(sideStart);
        triangles.Add(sideStart + 1);
        triangles.Add(sideStart + 2);
        triangles.Add(sideStart + 1);
        triangles.Add(sideStart + 3);
        triangles.Add(sideStart + 2);
    }

    void AddEndSide(List<Vector3> vertices, List<Vector3> normals, List<int> triangles, float angle, float yBottom, float yTop)
    {
        float a = Mathf.Deg2Rad * angle;
        float x = Mathf.Cos(a) * radius;
        float z = Mathf.Sin(a) * radius;
        Vector3 outNormal = new Vector3(-Mathf.Sin(a), 0, Mathf.Cos(a));

        int sideStart = vertices.Count;
        vertices.Add(new Vector3(0, yBottom, 0));
        normals.Add(outNormal);
        vertices.Add(new Vector3(0, yTop, 0));
        normals.Add(outNormal);
        vertices.Add(new Vector3(x, yBottom, z));
        normals.Add(outNormal);
        vertices.Add(new Vector3(x, yTop, z));
        normals.Add(outNormal);

        triangles.Add(sideStart);
        triangles.Add(sideStart + 2);
        triangles.Add(sideStart + 1);
        triangles.Add(sideStart + 1);
        triangles.Add(sideStart + 2);
        triangles.Add(sideStart + 3);
    }

    Vector3 GetRimPoint(float startAngle, float angle, int steps, int index, float y)
    {
        float a = Mathf.Deg2Rad * (startAngle + angle / steps * index);
        return new Vector3(Mathf.Cos(a) * radius, y, Mathf.Sin(a) * radius);
    }

    Color GetColor(float returnRate)
    {
        if (returnRate > 2f)
            return Color.Lerp(new Color(1f, 0.6f, 0.6f), new Color(1f, 0.1f, 0.1f), Mathf.Clamp01(returnRate / 20f));
        else if (returnRate < -2f)
            return Color.Lerp(new Color(0.6f, 0.6f, 1f), new Color(0.1f, 0.1f, 1f), Mathf.Clamp01(-returnRate / 20f));
        else
            return new Color(0.75f, 0.75f, 0.75f);
    }
}
