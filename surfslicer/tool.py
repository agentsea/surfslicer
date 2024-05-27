import hashlib
import logging
import os
import time
from typing import List, Optional, Tuple

import requests
from agentdesk.device import Desktop
from mllm import RoleMessage, RoleThread, Router
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field
from rich.console import Console
from rich.json import JSON
from taskara import Task
from toolfuse import Tool, action

from .grid import create_grid_image, zoom_in
from .img import Box, b64_to_image, image_to_b64
from .merge_image import superimpose_images

router = Router.from_env()
console = Console()

logger = logging.getLogger(__name__)
logger.setLevel(int(os.getenv("LOG_LEVEL", logging.DEBUG)))


class SemanticDesktop(Tool):
    """A semantic desktop replaces click actions with semantic description rather than coordinates"""

    def __init__(
        self, task: Task, desktop: Desktop, data_path: str = "./.data"
    ) -> None:
        """
        Initialize and open a URL in the application.

        Args:
            task: Agent task. Defaults to None.
            desktop: Desktop instance to wrap.
            data_path (str, optional): Path to data. Defaults to "./.data".
        """
        super().__init__(wraps=desktop)
        self.desktop = desktop

        self.data_path = data_path
        self.img_path = os.path.join(self.data_path, "images", task.id)
        os.makedirs(self.img_path, exist_ok=True)

        self.task = task

    @action
    def click_object(self, description: str, type: str) -> None:
        """Click on an object on the screen

        Args:
            description (str): The description of the object including its general location, for example
                "a round dark blue icon with the text 'Home' in the top-right of the image", please be a generic as possible
            type (str): Type of click, can be 'single' for a single click or
                'double' for a double click. If you need to launch an application from the desktop choose 'double'
        """
        if type != "single" and type != "double":
            raise ValueError("type must be'single' or 'double'")

        logging.debug("clicking icon with description ", description)

        max_depth = int(os.getenv("MAX_DEPTH", 3))
        color_text = os.getenv("COLOR_TEXT", "yellow")
        color_circle = os.getenv("COLOR_CIRCLE", "red")
        num_cells = int(os.getenv("NUM_CELLS", 3))

        click_hash = hashlib.md5(description.encode()).hexdigest()[:5]

        class ZoomSelection(BaseModel):
            """Zoom selection model"""

            number: int = Field(
                ...,
                description=f"Number of the dot closest to the place we want to click.",
            )

        current_img_b64 = self.desktop.take_screenshot()
        current_img = b64_to_image(current_img_b64)
        original_img = current_img.copy()
        img_width, img_height = current_img.size

        initial_box = Box(0, 0, img_width, img_height)
        bounding_boxes = [initial_box]
        total_upscale = 1

        thread = RoleThread()

        self.task.post_message(
            role="assistant",
            msg=f"Clicking '{type}' on object '{description}'",
            thread="debug",
            images=[image_to_b64(current_img)],
        )

        for i in range(max_depth):
            logger.info(f"zoom depth {i}")
            image_path = os.path.join(self.img_path, f"{click_hash}_current_{i}.png")
            current_img.save(image_path)
            img_width, img_height = current_img.size

            screenshot_b64 = image_to_b64(current_img)
            self.task.post_message(
                role="assistant",
                msg=f"Zooming into image with depth {i}",
                thread="debug",
                images=[screenshot_b64],
            )

            color_circle = "red"
            color_number = "yellow"
            n = 8
            upscale = 3

            grid_path = os.path.join(self.img_path, f"{click_hash}_grid_{i}.png")
            create_grid_image(
                img_width, img_height, color_circle, color_number, n, grid_path
            )

            merged_image_path = os.path.join(
                self.img_path, f"{click_hash}_merge_{i}.png"
            )
            merged_image = superimpose_images(image_path, grid_path, 1)
            merged_image.save(merged_image_path)

            merged_image_b64 = image_to_b64(merged_image)
            self.task.post_message(
                role="assistant",
                msg=f"Merge image for depth {i}",
                thread="debug",
                images=[merged_image_b64],
            )

            prompt = f"""
            You are an experienced AI trained to find the elements on the screen.
            You see a screenshot of the web application. 
            I have drawn some big {color_number} numbers on {color_circle} circles on this image 
            to help you to find required elements. 
            Please tell me the closest big {color_number} number to {description}.
            Please return you response as raw JSON following the schema {ZoomSelection.model_json_schema()}
            Be concise and only return the raw json, for example if the circle you wanted to select had a number 3 next to it
            you would return {{"number": 3}}
            """

            msg = RoleMessage(
                role="user",
                text=prompt,
                images=[merged_image_b64],
            )
            thread.add_msg(msg)

            response = router.chat(
                thread, namespace="zoom", expect=ZoomSelection, agent_id="SurfSlicer"
            )
            if not response.parsed:
                raise SystemError("No response parsed from zoom")

            logger.info(f"zoom response {response}")

            self.task.add_prompt(response.prompt)

            zoom_resp = response.parsed
            self.task.post_message(
                role="assistant",
                msg=f"Selection {zoom_resp.model_dump_json()}",
                thread="debug",
            )
            console.print(JSON(zoom_resp.model_dump_json()))

            zoomed_img, top_left, bottom_right = zoom_in(
                image_path, n, zoom_resp.number, upscale
            )
            current_img = zoomed_img.copy()
            bounding_box = Box(
                top_left[0], top_left[1], bottom_right[0], bottom_right[1]
            )
            absolute_box = bounding_box.to_absolute_with_upscale(
                bounding_boxes[-1], total_upscale
            )
            total_upscale *= upscale
            bounding_boxes.append(absolute_box)

        click_x, click_y = bounding_boxes[-1].center()
        logger.info(f"clicking exact coords {click_x}, {click_y}")
        self.task.post_message(
            role="assistant",
            msg=f"Clicking coordinates {click_x}, {click_y}",
            thread="debug",
        )

        debug_img = self._debug_image(
            original_img.copy(), bounding_boxes, (click_x, click_y)
        )
        self.task.post_message(
            role="assistant",
            msg=f"Final debug img",
            thread="debug",
            images=[image_to_b64(debug_img)],
        )
        self._click_coords(x=click_x, y=click_y, type=type)
        return

    def _click_coords(self, x: int, y: int, type: str = "single") -> None:
        """Click mouse button

        Args:
            x (Optional[int], optional): X coordinate to move to, if not provided
                it will click on current location. Defaults to None.
            y (Optional[int], optional): Y coordinate to move to, if not provided
                it will click on current location. Defaults to None.
            button (str, optional): Button to click. Defaults to "left".
        """
        # TODO: fix click cords in agentd
        logging.debug("moving mouse")
        body = {"x": int(x), "y": int(y)}
        resp = requests.post(f"{self.desktop.base_url}/move_mouse", json=body)
        resp.raise_for_status()
        time.sleep(2)

        if type == "single":
            logging.debug("clicking")
            resp = requests.post(f"{self.desktop.base_url}/click", json={})
            resp.raise_for_status()
            time.sleep(2)
        elif type == "double":
            logging.debug("double clicking")
            resp = requests.post(f"{self.desktop.base_url}/double_click", json={})
            resp.raise_for_status()
            time.sleep(2)
        else:
            raise ValueError(f"unkown click type {type}")
        return

    def _debug_image(
        self,
        img: Image.Image,
        boxes: List[Box],
        final_click: Optional[Tuple[int, int]] = None,
    ) -> Image.Image:
        draw = ImageDraw.Draw(img)
        for box in boxes:
            box.draw(draw)

        if final_click:
            draw.ellipse(
                [
                    final_click[0] - 5,
                    final_click[1] - 5,
                    final_click[0] + 5,
                    final_click[1] + 5,
                ],
                fill="red",
                outline="red",
            )
        return img
