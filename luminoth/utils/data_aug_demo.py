import os
import tempfile

from easydict import EasyDict
import click
import cv2
import numpy as np
import pandas as pd
import tensorflow as tf

from luminoth.utils.mosaic import assemble_mosaic, _set_fill_value
from luminoth.utils.overlay_bbs import overlay_bb_labels
from luminoth.utils.split_train_val import LUMI_CSV_COLUMNS
from luminoth.utils.image import (
    flip_image,
    random_patch,
    random_resize,
    random_distortion,
    expand,
    rot90,
    random_patch_gaussian,
)
from .vis import FONT_COLOR, FONT_SCALE, BB_COLOR, BB_LINE_WIDTH

# Constant for tile_size of an image in the mosaic
TILE_SIZE = [256, 256]
# Constants for bounding box and label overlays
FONT = cv2.FONT_HERSHEY_SIMPLEX
AUG_FONT_SCALE = 1
AUG_FONT_COLOR = (255, 0, 255)
LINE_TYPE = 2

# TODO PV Move below config to a config file that can be given as input for cli
RANDOM_DISTORT_CONFIG = EasyDict(
    {
        "brightness": {
            "max_delta": 0.3,
        },
        "contrast": {
            "lower": 0.4,
            "upper": 0.8,
        },
        "hue": {
            "max_delta": 0.2,
        },
        "saturation": {
            "lower": 0.5,
            "upper": 1.5,
        },
    }
)
FLIP_CONFIG = EasyDict({"left_right": True, "up_down": False})
RANDOM_PATCH_CONFIG = EasyDict({"min_height": 600, "min_width": 600})
RANDOM_RESIZE_CONFIG = EasyDict({"min_size": 400, "max_size": 980})
EXPAND_CONFIG = EasyDict({"fill": 0, "min_ratio": 1, "max_ratio": 4})
ROTATE90_CONFIG = EasyDict({})
GAUSSIAN_CONFIG = EasyDict({})
DATA_AUGMENTATION_CONFIGS = {
    flip_image: FLIP_CONFIG,
    random_patch: RANDOM_PATCH_CONFIG,
    random_resize: RANDOM_RESIZE_CONFIG,
    random_distortion: RANDOM_DISTORT_CONFIG,
    expand: EXPAND_CONFIG,
    rot90: ROTATE90_CONFIG,
    random_patch_gaussian: GAUSSIAN_CONFIG,
}


def update_augmentation(
    augmented_dict, labels, location, augmentation, augmented_images
):
    """
    Updates list augmented_images with the path to the image
    containing overlaid image with bboxes in augmented_dict
    bboxes is of shape (N, 5) containing n rows of [xmin,ymin,xmax,ymax,label]

    Args:
        augmented_dict: dict 2 keys bboxes, image containing numpy arrays of
            images and bounding boxes with the label in the image as values
        labels: list List of sorted unique labels for the
            bounding box objects in  the image
        location: str directory to save the augmented image in
        augmentation: str augmentation technique text
            to write on the augmented bounding box overlaid image
        augmented_images: list list of full path to augmented images

    Returns:
       Update list of full path to augmented image overlaid with bounding box
    """
    # Write augmented image to a path to input to overlay_bb_labels
    image = augmented_dict["image"]
    base_path = "input_{}_image.png".format(augmentation)
    base_path_wo_format = "input_{}_image".format(augmentation)
    im_filename = os.path.join(location, base_path)
    cv2.imwrite(im_filename, image)

    # Form dataframe with bounding boxes, labels to input to overlay_bb_labels
    df = pd.DataFrame(columns=LUMI_CSV_COLUMNS + ["base_path"])
    for bboxes in augmented_dict["bboxes"]:
        label = labels[bboxes[4]]
        if type(label) is int or type(label) is float:
            label = str(label)
        df = df.append(
            {
                "xmin": bboxes[0],
                "ymin": bboxes[1],
                "xmax": bboxes[2],
                "ymax": bboxes[3],
                "label": label,
            },
            ignore_index=True,
        )

    df.base_path = base_path_wo_format
    df.image_path = im_filename

    # overlay bounding box labels on the augmented image
    overlaid_augmented_image = overlay_bb_labels(
        im_filename, ".png", df, BB_COLOR, BB_LINE_WIDTH, FONT_COLOR, FONT_SCALE
    )

    # write augmentation technique string on the image
    cv2.putText(
        overlaid_augmented_image,
        augmentation,
        (100, 100),
        FONT,
        AUG_FONT_SCALE,
        AUG_FONT_COLOR,
        LINE_TYPE,
    )

    # Write the augmented bounding box overlaid png image to disk
    im_filename = os.path.join(location, augmentation + "bb_labels.png")
    cv2.imwrite(im_filename, overlaid_augmented_image)

    # Update augmented_images list
    augmented_images.append(im_filename)


def get_data_aug_images(image_array, bboxes_array, labels):
    """
    Get list of all augmented images

    Args:
        image_array: np.array Image of shape [h, w, 3]
        bboxes_array: np.array bboxes of shape [N, 5]
            containing n rows of [xmin,ymin,xmax,ymax,label]
        labels: list List of sorted unique labels for the
            bounding box objects in  the image

    Returns:
        augmented_images: list
    """

    # Convert image, bboxes to tensorflow
    assert len(image_array.shape) == 3, "shape array length must be 3"
    assert image_array.shape[2] == 3, "must be a 3 channeled image"
    image = tf.placeholder(tf.float32, image_array.shape)
    feed_dict = {
        image: image_array,
    }
    if bboxes_array is not None:
        bboxes = tf.placeholder(tf.int32, bboxes_array.shape)
        feed_dict[bboxes] = bboxes_array
    else:
        bboxes = None

    location = tempfile.mkdtemp()
    augmented_images = []
    with tf.Session() as sess:
        # run all the data augmentation
        for aug_fn, config in DATA_AUGMENTATION_CONFIGS.items():
            augmented = aug_fn(image, bboxes=bboxes, **config)
            augment_dict = sess.run(augmented, feed_dict=feed_dict)
            # write all the augmented overlaid images and set their paths
            # in augmented_images list
            update_augmentation(
                augment_dict, labels, location, aug_fn.__name__, augmented_images
            )

    return augmented_images


def mosaic_data_aug(input_image, input_image_format, csv_path, fill_value, output_png):
    """
    Mosaic data augmented images after resizing them to tile_size.
    all the pixels that don't fit in the stitched image shape
    by the resized tiles are filled with fill_value

    Args:
        input_image: str Input png to augment
        input_image_format: str Format of the input images
        csv_path: str csv containing image_id,xmin,xmax,ymin,ymax,label
            for the input_png. Bounding boxes in the csv are augmented
        fill_value: str fill the tiles that couldn't be filled with the images
        output_png: str write the stitched mosaic image to

    Returns:
        Write stitched data augmentation mosaic image of shape
        tile_size[0] * sqrt(len(DATA_AUGMENTATION_STRATEGIES)),
        tile_size[1] * sqrt(len(DATA_AUGMENTATION_STRATEGIES)).
    """
    image = cv2.imread(input_image, cv2.IMREAD_COLOR)

    # Filter out rows containing bounding boxes and annotations
    # for the input_image path above
    df = pd.read_csv(csv_path)
    basename = os.path.basename(input_image).replace(input_image_format, "")
    tmp_df = df[
        [
            True
            if os.path.basename(row["image_id"])
            .replace(input_image_format, "")
            .endswith(basename)
            else False
            for index, row in df.iterrows()
        ]
    ]

    # Get the list of bboxes for one input_image
    bboxes = []
    labels = sorted(df["label"].unique().tolist())
    for index, row in tmp_df.iterrows():
        bboxes.append(
            [
                np.int32(row["xmin"]),
                np.int32(row["ymin"]),
                np.int32(row["xmax"]),
                np.int32(row["ymax"]),
                np.int32(labels.index(row["label"])),
            ]
        )
    bboxes = np.array(bboxes, dtype=np.int32)

    augmented_images = get_data_aug_images(image, bboxes, labels)

    # Save the mosaiced image
    mosaiced_image = assemble_mosaic(
        augmented_images, TILE_SIZE, _set_fill_value(image, fill_value)
    )
    shape = mosaiced_image.shape
    cv2.imwrite(output_png, mosaiced_image)

    print("Mosaiced image is at: {} of shape {}".format(output_png, shape))


@click.command(
    help="Save one assembled mosaic filled with data augmented images for given input image"
)  # noqa
@click.option(
    "--input_image", help="Input data to augment", required=True, type=str
)  # noqa
@click.option(
    "--input_image_format", help="Input image format", required=True, type=str
)  # noqa
@click.option(
    "--csv_path",
    help="Csv containing image_id,xmin,xmax,ymin,ymax,label.Bounding boxes in the input png to augment",
    required=True,
    type=str,
)  # noqa
@click.option(
    "--fill_value",
    help="fill the image with zeros or the first intensity at [0,0] in the image",
    required=False,
    type=str,
)  # noqa
@click.option(
    "--output_png",
    help="Absolute path to folder name to save the data aug mosaiced images to",
    required=True,
    type=str,
)  # noqa
def data_aug_demo(input_image, input_image_format, csv_path, fill_value, output_png):
    mosaic_data_aug(input_image, input_image_format, csv_path, fill_value, output_png)


if __name__ == "__main__":
    data_aug_demo()
