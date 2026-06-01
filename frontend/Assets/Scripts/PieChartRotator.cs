using UnityEngine;

#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

public class PieChartRotator : MonoBehaviour
{
    [SerializeField] private float yawDegreesPerPixel = 0.25f;

    private bool isDragging;
    private Vector2 previousPointerPosition;
    private float yaw;

    void Awake()
    {
        Vector3 euler = transform.localEulerAngles;
        yaw = NormalizeAngle(euler.y);
    }

    void Update()
    {
        if (TryGetPointerDown(out Vector2 downPosition))
        {
            isDragging = true;
            previousPointerPosition = downPosition;
            return;
        }

        if (!isDragging)
            return;

        if (TryGetPointerUp())
        {
            isDragging = false;
            return;
        }

        if (!TryGetPointerPosition(out Vector2 currentPosition))
            return;

        Vector2 delta = currentPosition - previousPointerPosition;
        previousPointerPosition = currentPosition;

        yaw -= delta.x * yawDegreesPerPixel;
        transform.localRotation = Quaternion.Euler(0f, yaw, 0f);
    }

    static float NormalizeAngle(float angle)
    {
        angle %= 360f;
        return angle > 180f ? angle - 360f : angle;
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

    bool TryGetPointerUp()
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null && Mouse.current.leftButton.wasReleasedThisFrame)
            return true;

        if (Touchscreen.current != null && Touchscreen.current.primaryTouch.press.wasReleasedThisFrame)
            return true;
#endif

#if ENABLE_LEGACY_INPUT_MANAGER
        if (Input.GetMouseButtonUp(0))
            return true;
#endif

        return false;
    }

    bool TryGetPointerPosition(out Vector2 position)
    {
#if ENABLE_INPUT_SYSTEM
        if (Mouse.current != null && Mouse.current.leftButton.isPressed)
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
        if (Input.GetMouseButton(0))
        {
            position = Input.mousePosition;
            return true;
        }
#endif

        position = Vector2.zero;
        return false;
    }
}
