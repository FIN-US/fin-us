using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Rendering;

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
    [SerializeField] private Material highlightMaterial;
    [SerializeField] private Color highlightColor = new Color(1f, 0.86f, 0.12f, 1f);
    [SerializeField] private float highlightScale = 1.035f;
    [SerializeField] private float highlightEdgeWidth = 0.035f;
    [SerializeField] private float highlightEdgeOffset = 0.03f;
    // 샘플 데이터를 그릴 때 조각 색을 회색 쪽으로 끌어당겨, 배너 없이도 실데이터와
    // 구분되게 한다(이슈 #244). 0이면 실데이터와 같은 색, 1이면 완전한 회색이다.
    [SerializeField, Range(0f, 1f)] private float sampleDataDesaturation = 0.72f;
    [SerializeField] private Color sampleDataTint = new Color(0.62f, 0.62f, 0.62f);

    private static readonly int BaseColorId = Shader.PropertyToID("_BaseColor");
    private static readonly int ColorId = Shader.PropertyToID("_Color");
    private MaterialPropertyBlock propertyBlock;
    private Material runtimeHighlightMaterial;
    private Material runtimeOverlayLineMaterial;
    private bool renderingSampleData;

    void Awake()
    {
        propertyBlock = new MaterialPropertyBlock();
    }

    void OnDestroy()
    {
        if (runtimeHighlightMaterial != null)
            Destroy(runtimeHighlightMaterial);

        if (runtimeOverlayLineMaterial != null)
            Destroy(runtimeOverlayLineMaterial);
    }

    // isSampleData=true이면 실데이터 로드 실패로 Resources의 샘플을 그리는 경우다(#244).
    public void Generate(PortfolioData portfolioData, bool isSampleData = false)
    {
        data = portfolioData;
        renderingSampleData = isSampleData;
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

        GameObject hoverHighlight = CreateHoverHighlight(obj.transform, mesh, startAngle, angle, yBottom, yTop);

        PieSliceClickHandler clickHandler = obj.AddComponent<PieSliceClickHandler>();
        clickHandler.Initialize(holding, hoverHighlight);

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

    GameObject CreateHoverHighlight(Transform parent, Mesh sourceMesh, float startAngle, float angle, float yBottom, float yTop)
    {
        GameObject highlightRoot = new GameObject("HoverHighlight");
        highlightRoot.transform.SetParent(parent, false);

        GameObject highlightObject = new GameObject("HoverOutline");
        highlightObject.transform.SetParent(highlightRoot.transform, false);
        float outlineScale = Mathf.Max(1.001f, highlightScale);
        Vector3 boundsCenter = sourceMesh.bounds.center;
        highlightObject.transform.localPosition = boundsCenter * (1f - outlineScale);
        highlightObject.transform.localScale = Vector3.one * outlineScale;

        MeshFilter meshFilter = highlightObject.AddComponent<MeshFilter>();
        meshFilter.sharedMesh = sourceMesh;

        MeshRenderer meshRenderer = highlightObject.AddComponent<MeshRenderer>();
        meshRenderer.shadowCastingMode = ShadowCastingMode.Off;
        meshRenderer.receiveShadows = false;

        Material material = GetHighlightMaterial();
        if (material != null)
            meshRenderer.sharedMaterial = material;

        CreateBoundaryOverlay(highlightRoot.transform, startAngle, angle, yBottom, yTop);

        highlightRoot.SetActive(false);
        return highlightRoot;
    }

    void CreateBoundaryOverlay(Transform parent, float startAngle, float angle, float yBottom, float yTop)
    {
        float topY = yTop + highlightEdgeOffset;
        float bottomY = yBottom - highlightEdgeOffset;

        Vector3 topCenter = new Vector3(0f, topY, 0f);
        Vector3 bottomCenter = new Vector3(0f, bottomY, 0f);
        Vector3 startTop = GetRimPoint(startAngle, 0f, 1, 0, topY, radius + highlightEdgeOffset);
        Vector3 endTop = GetRimPoint(startAngle + angle, 0f, 1, 0, topY, radius + highlightEdgeOffset);
        Vector3 startBottom = GetRimPoint(startAngle, 0f, 1, 0, bottomY, radius + highlightEdgeOffset);
        Vector3 endBottom = GetRimPoint(startAngle + angle, 0f, 1, 0, bottomY, radius + highlightEdgeOffset);

        CreateOverlayLine(parent, "StartTopBoundary", new[] { topCenter, startTop });
        CreateOverlayLine(parent, "EndTopBoundary", new[] { topCenter, endTop });
        CreateOverlayLine(parent, "StartOuterVerticalBoundary", new[] { startBottom, startTop });
        CreateOverlayLine(parent, "EndOuterVerticalBoundary", new[] { endBottom, endTop });
        CreateOverlayLine(parent, "CenterVerticalBoundary", new[] { bottomCenter, topCenter });
    }

    void CreateOverlayLine(Transform parent, string name, Vector3[] positions)
    {
        GameObject lineObject = new GameObject(name);
        lineObject.transform.SetParent(parent, false);

        LineRenderer lineRenderer = lineObject.AddComponent<LineRenderer>();
        lineRenderer.useWorldSpace = false;
        lineRenderer.positionCount = positions.Length;
        lineRenderer.widthMultiplier = highlightEdgeWidth;
        lineRenderer.numCornerVertices = 3;
        lineRenderer.numCapVertices = 3;
        lineRenderer.startColor = highlightColor;
        lineRenderer.endColor = highlightColor;
        lineRenderer.shadowCastingMode = ShadowCastingMode.Off;
        lineRenderer.receiveShadows = false;
        Material material = GetOverlayLineMaterial();
        if (material != null)
            lineRenderer.sharedMaterial = material;

        lineRenderer.SetPositions(positions);
    }

    Material GetHighlightMaterial()
    {
        if (runtimeHighlightMaterial == null)
        {
            Shader shader = Shader.Find("Universal Render Pipeline/Unlit");

            if (shader == null)
                shader = Shader.Find("Sprites/Default");

            if (shader == null)
                shader = Shader.Find("Universal Render Pipeline/Lit");

            if (shader != null)
            {
                runtimeHighlightMaterial = new Material(shader);

                if (runtimeHighlightMaterial.HasProperty(BaseColorId))
                    runtimeHighlightMaterial.SetColor(BaseColorId, highlightColor);

                if (runtimeHighlightMaterial.HasProperty(ColorId))
                    runtimeHighlightMaterial.SetColor(ColorId, highlightColor);

                if (runtimeHighlightMaterial.HasProperty("_EmissionColor"))
                    runtimeHighlightMaterial.SetColor("_EmissionColor", highlightColor);

                if (runtimeHighlightMaterial.HasProperty("_SpecularHighlights"))
                    runtimeHighlightMaterial.SetFloat("_SpecularHighlights", 0f);

                if (runtimeHighlightMaterial.HasProperty("_EnvironmentReflections"))
                    runtimeHighlightMaterial.SetFloat("_EnvironmentReflections", 0f);

                if (runtimeHighlightMaterial.HasProperty("_Cull"))
                    runtimeHighlightMaterial.SetFloat("_Cull", (float)CullMode.Front);

                if (runtimeHighlightMaterial.HasProperty("_ZWrite"))
                    runtimeHighlightMaterial.SetFloat("_ZWrite", 0f);

                runtimeHighlightMaterial.renderQueue = 3000;
            }
        }

        return runtimeHighlightMaterial;
    }

    Material GetOverlayLineMaterial()
    {
        if (runtimeOverlayLineMaterial == null)
        {
            Shader shader = Shader.Find("Hidden/Internal-Colored");

            if (shader == null)
                shader = Shader.Find("Universal Render Pipeline/Unlit");

            if (shader == null)
                shader = Shader.Find("Sprites/Default");

            if (shader != null)
            {
                runtimeOverlayLineMaterial = new Material(shader);

                if (runtimeOverlayLineMaterial.HasProperty(BaseColorId))
                    runtimeOverlayLineMaterial.SetColor(BaseColorId, highlightColor);

                if (runtimeOverlayLineMaterial.HasProperty(ColorId))
                    runtimeOverlayLineMaterial.SetColor(ColorId, highlightColor);

                if (runtimeOverlayLineMaterial.HasProperty("_Cull"))
                    runtimeOverlayLineMaterial.SetFloat("_Cull", (float)CullMode.Off);

                if (runtimeOverlayLineMaterial.HasProperty("_ZWrite"))
                    runtimeOverlayLineMaterial.SetFloat("_ZWrite", 0f);

                if (runtimeOverlayLineMaterial.HasProperty("_ZTest"))
                    runtimeOverlayLineMaterial.SetFloat("_ZTest", (float)CompareFunction.Always);

                runtimeOverlayLineMaterial.renderQueue = 5000;
            }
        }

        return runtimeOverlayLineMaterial;
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
        return GetRimPoint(startAngle, angle, steps, index, y, radius);
    }

    Vector3 GetRimPoint(float startAngle, float angle, int steps, int index, float y, float targetRadius)
    {
        float a = Mathf.Deg2Rad * (startAngle + angle / steps * index);
        return new Vector3(Mathf.Cos(a) * targetRadius, y, Mathf.Sin(a) * targetRadius);
    }

    Color GetColor(float returnRate)
    {
        Color color = GetReturnRateColor(returnRate);
        return renderingSampleData
            ? Color.Lerp(color, sampleDataTint, Mathf.Clamp01(sampleDataDesaturation))
            : color;
    }

    Color GetReturnRateColor(float returnRate)
    {
        if (returnRate > 2f)
            return Color.Lerp(new Color(1f, 0.6f, 0.6f), new Color(1f, 0.1f, 0.1f), Mathf.Clamp01(returnRate / 20f));
        else if (returnRate < -2f)
            return Color.Lerp(new Color(0.6f, 0.6f, 1f), new Color(0.1f, 0.1f, 1f), Mathf.Clamp01(-returnRate / 20f));
        else
            return new Color(0.75f, 0.75f, 0.75f);
    }
}
