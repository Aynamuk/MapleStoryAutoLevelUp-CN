import cv2
import argparse
import os

# local import
from src.utils.common import to_standard_hsv, imread_unicode

def main():
    parser = argparse.ArgumentParser(description="OpenCV Pixel Inspector")
    parser.add_argument("image_path", help="Path to image file")
    args = parser.parse_args()

    if not os.path.exists(args.image_path):
        print(f"[Error] File not found: {args.image_path}")
        return

    # Load image in BGR（中文路径安全版）
    img = imread_unicode(args.image_path)
    if img is None:
        print("[Error] Failed to load image.")
        return
    img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    window_name = "Pixel Inspector"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    img_display = img.copy()
    info = {"x": 0, "y": 0, "r": 0, "g": 0, "b": 0, "h": 0, "s": 0, "v": 0}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_MOUSEMOVE:
            b, g, r = img[y, x]
            h_cv2, s_cv2, v_cv2 = img_hsv[y, x]
            h_std, s_std, v_std = to_standard_hsv((h_cv2, s_cv2, v_cv2))
            info["x"], info["y"] = x, y
            info["r"], info["g"], info["b"] = r, g, b
            info["h"], info["s"], info["v"] = round(h_std), round(s_std), round(v_std)

    cv2.setMouseCallback(window_name, on_mouse)

    while True:
        img_display = img.copy()
        text = f"Pixel Coordinate({info['x']}, {info['y']})"\
               f"RGB: ({info['r']}, {info['g']}, {info['b']})"\
               f"HSV: ({info['h']}, {info['s']}, {info['v']})"
        text_size, _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)

        # Position of text
        x_text = img.shape[1] - text_size[0] - 30
        y_text = 30

        # Draw the color dot (circle)
        cv2.circle(img_display, (x_text - 10, y_text - 5), 6,
                (int(info['b']), int(info['g']), int(info['r'])), -1)

        # Draw the text
        cv2.putText(img_display, text,
                    (x_text, y_text),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 1, cv2.LINE_AA)

        cv2.imshow(window_name, img_display)
        key = cv2.waitKey(10)
        if key == 27:  # ESC to quit
            break

    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
