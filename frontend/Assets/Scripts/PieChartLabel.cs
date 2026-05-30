using System.Collections;
using UnityEngine;

public class PieChartLabel : MonoBehaviour
{
    private const int FontSize = 64;
    private static Sprite backgroundSprite;

    [SerializeField] private Vector2 padding = new Vector2(0.05f, 0f);
    [SerializeField] private Color textColor = Color.black;
    [SerializeField] private Color backgroundColor = new Color(0.55f, 0.55f, 0.55f, 0.55f);

    private Camera targetCamera;
    private TextMesh textMesh;
    private SpriteRenderer backgroundRenderer;
    private float characterSize;

    public void Initialize(string text, Font font, Camera cameraToFace, float labelCharacterSize)
    {
        targetCamera = cameraToFace;
        characterSize = labelCharacterSize;

        EnsureBackgroundSprite();
        CreateBackground();
        CreateText(text, font);
        ResizeBackground();
        StartCoroutine(ResizeBackgroundAfterTextLayout());
    }

    void LateUpdate()
    {
        if (targetCamera == null)
            targetCamera = Camera.main;

        if (targetCamera == null)
            return;

        transform.rotation = targetCamera.transform.rotation;
    }

    void CreateText(string text, Font font)
    {
        GameObject textObject = new GameObject("Text");
        textObject.transform.SetParent(transform, false);
        textObject.transform.localPosition = Vector3.zero;

        textMesh = textObject.AddComponent<TextMesh>();
        textMesh.text = text;
        textMesh.font = font;
        textMesh.fontSize = FontSize;
        textMesh.characterSize = characterSize;
        textMesh.anchor = TextAnchor.MiddleCenter;
        textMesh.alignment = TextAlignment.Center;
        textMesh.color = textColor;

        MeshRenderer textRenderer = textObject.GetComponent<MeshRenderer>();
        if (font != null)
            textRenderer.sharedMaterial = font.material;
        textRenderer.sortingOrder = 1;
    }

    void CreateBackground()
    {
        GameObject backgroundObject = new GameObject("Background");
        backgroundObject.transform.SetParent(transform, false);
        backgroundObject.transform.localPosition = Vector3.zero;

        backgroundRenderer = backgroundObject.AddComponent<SpriteRenderer>();
        backgroundRenderer.sprite = backgroundSprite;
        backgroundRenderer.color = backgroundColor;
        backgroundRenderer.sortingOrder = 0;
    }

    IEnumerator ResizeBackgroundAfterTextLayout()
    {
        yield return null;
        ResizeBackground();
    }

    void ResizeBackground()
    {
        MeshRenderer textRenderer = textMesh.GetComponent<MeshRenderer>();
        Bounds textBounds = textRenderer.localBounds;
        Vector3 size = textBounds.size;

        if (size.x <= 0.001f || size.y <= 0.001f)
            size = EstimateTextSize();

        float width = size.x + padding.x * 2f;
        float height = size.y + padding.y * 2f;
        backgroundRenderer.transform.localPosition = new Vector3(textBounds.center.x, textBounds.center.y, 0f);
        backgroundRenderer.transform.localScale = new Vector3(width, height, 1f);
    }

    Vector3 EstimateTextSize()
    {
        int visibleCharacterCount = string.IsNullOrEmpty(textMesh.text) ? 1 : textMesh.text.Length;
        float width = Mathf.Max(characterSize, visibleCharacterCount * characterSize * 0.62f);
        float height = characterSize * 1.65f;
        return new Vector3(width, height, 0f);
    }

    static void EnsureBackgroundSprite()
    {
        if (backgroundSprite != null)
            return;

        backgroundSprite = Sprite.Create(
            Texture2D.whiteTexture,
            new Rect(0, 0, 1, 1),
            new Vector2(0.5f, 0.5f),
            1f
        );
    }
}
