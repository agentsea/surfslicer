import argparse
from PIL import Image

def superimpose_images(image1_path, image2_path, opacity):
    # Open the images
    image1 = Image.open(image1_path)
    image2 = Image.open(image2_path)

    # Ensure both images have the same size
    if image1.size != image2.size:
        raise ValueError("Images must have the same dimensions.")

    # Convert the images to RGBA mode if they are not already
    image1 = image1.convert("RGBA")
    image2 = image2.convert("RGBA")

    # Create a new image with the same size as the input images
    merged_image = Image.new("RGBA", image1.size)

    # Convert image1 to grayscale
    image1 = image1.convert("L")

    # Paste image1 onto the merged image
    merged_image.paste(image1, (0, 0))

    # Create a new image for image2 with adjusted opacity
    image2_with_opacity = Image.blend(Image.new("RGBA", image2.size, (0, 0, 0, 0)), image2, opacity)

    # Paste image2 with opacity onto the merged image
    merged_image = Image.alpha_composite(merged_image, image2_with_opacity)

    return merged_image

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image1", required=True, help="Path to the first image file")
    parser.add_argument("--image2", required=True, help="Path to the second image file")
    parser.add_argument("--opacity", type=float, default=0.5, help="Opacity value for image2 (default: 0.5)")
    args = parser.parse_args()

    merged_image = superimpose_images(args.image1, args.image2, args.opacity)
    merged_image_path = "merged_image.png"
    merged_image.save(merged_image_path)