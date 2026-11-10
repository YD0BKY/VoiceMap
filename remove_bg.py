from PIL import Image, ImageDraw
import os
import sys

img_path = "D:\\VoiceMap\\sdr_monitoring_logo.png"

if not os.path.exists(img_path):
    print(f"Error: {img_path} not found.")
    sys.exit(1)

# Open image in RGB mode
img = Image.open(img_path)
img = img.convert("RGB")
width, height = img.size

# Target fill color: The exact background color of the web header (#060812)
WEB_BG_COLOR = (6, 8, 18)

corners = [
    (0, 0),
    (width - 1, 0),
    (0, height - 1),
    (width - 1, height - 1)
]

# Run flood fill from the corners with a high threshold (140)
# to eat through the light checkered pattern completely
for corner in corners:
    ImageDraw.floodfill(img, corner, WEB_BG_COLOR, thresh=140)

# Save the final image
img.save(img_path, "PNG")
print("[+] Corner fill with web background color completed!")
