from typing import List, Type, Tuple
import logging
from typing import Final
import traceback
import time

from devicebay import Device
from agentdesk.device import Desktop
from rich.console import Console
from pydantic import BaseModel
from surfkit.agent import TaskAgent
from taskara import Task
from mllm import Router
from skillpacks.server.models import V1ActionSelection
from threadmem import RoleThread, RoleMessage
from tenacity import (
    retry,
    stop_after_attempt,
    before_sleep_log,
)
from rich.json import JSON

logger: Final = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

console = Console(force_terminal=True)

router = Router.from_env()


class SurfPizzaConfig(BaseModel):
    pass


class SurfPizza(TaskAgent):
    """A desktop agent that uses GPT-4V augmented with OCR and Grounding Dino to solve tasks"""

    def solve_task(
        self,
        task: Task,
        device: Device,
        max_steps: int = 30,
    ) -> Task:
        """Solve a task

        Args:
            task (Task): Task to solve.
            device (Device): Device to perform the task on.
            max_steps (int, optional): Max steps to try and solve. Defaults to 30.

        Returns:
            Task: The task
        """

        # Post a message to the default thread to let the user know the task is in progress
        task.post_message("assistant", f"Starting task '{task.description}'")

        # Create threads in the task to update the user
        console.print("creating threads...")
        task.ensure_thread("debug")
        task.post_message("assistant", f"I'll post debug messages here", thread="debug")

        # Check that the device we received is one we support
        if not isinstance(device, Desktop):
            raise ValueError("Only desktop devices supported")

        # Open a site if that is in the parameters
        site = task._parameters.get("site") if task._parameters else None
        if site:
            console.print(f"▶️ opening site url: {site}", style="blue")
            task.post_message("assistant", f"opening site url {site}...")
            device.open_url(site)
            console.print("waiting for browser to open...", style="blue")
            time.sleep(5)

        # Get the json schema for the tools
        tools = device.json_schema()
        console.print("tools: ", style="purple")
        console.print(JSON.from_data(tools))

        # Get info about the desktop
        info = device.info()
        screen_size = info["screen_size"]

        # Create our thread and start with a system prompt
        thread = RoleThread()
        thread.post(
            role="user",
            msg=(
                "You are an AI assistant which uses a devices to accomplish tasks. "
                f"Your current task is {task.description}, and your available tools are {device.json_schema()} "
                f"Please return the result chosen action as raw JSON adhearing to the schema {V1ActionSelection.model_json_schema()} "
            ),
        )
        response = router.chat(thread, namespace="system")
        console.print(f"\nsystem prompt response: {response}", style="blue")
        thread.add_msg(response.msg)

        # Loop to run actions
        for i in range(max_steps):
            console.print(f"\n\n-------\n\nstep {i + 1}\n", style="green")

            try:
                thread, done = self.take_action(device, task, thread, screen_size)
            except Exception as e:
                console.print(f"Error: {e}", style="red")
                task.status = "failed"
                task.error = str(e)
                task.save()
                task.post_message("assistant", f"❗ Error taking action: {e}")
                return task

            if done:
                console.print("task is done", style="green")
                # TODO: remove
                time.sleep(10)
                return task

            time.sleep(2)

        task.status = "failed"
        task.save()
        task.post_message("assistant", "❗ Max steps reached without solving task")
        console.print("Reached max steps without solving task", style="red")

        return task

    @retry(
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.INFO),
    )
    def take_action(
        self,
        desktop: Desktop,
        task: Task,
        thread: RoleThread,
        screen_size: dict,
    ) -> Tuple[RoleThread, bool]:
        """Take an action

        Args:
            desktop (SemanticDesktop): Desktop to use
            task (str): Task to accomplish
            thread (RoleThread): Role thread for the task
            screen_size (dict): Size of the screen

        Returns:
            bool: Whether the task is complete
        """
        try:
            # Check to see if the task has been cancelled
            if task.remote:
                task.refresh()
            if task.status == "cancelling" or task.status == "cancelled":
                console.print(f"task is {task.status}", style="red")
                if task.status == "cancelling":
                    task.status = "cancelled"
                    task.save()
                return thread, True

            console.print("taking action...", style="white")

            # Create a copy of the thread, and remove old images
            _thread = thread.copy()
            _thread.remove_images()

            # Take a screenshot of the desktop and post a message with it
            screenshot_b64 = desktop.take_screenshot()
            task.post_message(
                "assistant",
                "current image",
                images=[f"data:image/jpeg;base64,{screenshot_b64}"],
                thread="debug",
            )

            # Get the current mouse coordinates
            x, y = desktop.mouse_coordinates()
            console.print(f"mouse coordinates: ({x}, {y})", style="white")

            msg = RoleMessage(
                role="user",
                text=(
                    f"Here is a screenshot of the current desktop with the mouse coordinates ({x}, {y}). "
                    "Please select an action from the provided schema."
                ),
                images=[screenshot_b64],
            )
            _thread.add_msg(msg)

            # Make the action selection
            response = router.chat(
                _thread, namespace="action", expect=V1ActionSelection
            )

            try:
                # Post to the user letting them know what the modle selected
                selection = response.parsed
                if not selection:
                    raise ValueError("No action selection parsed")

                task.post_message("assistant", f"👁️ {selection.observation}")
                task.post_message("assistant", f"💡 {selection.reason}")
                console.print(f"action selection: ", style="white")
                console.print(JSON.from_data(selection.model_dump()))

                task.post_message(
                    "assistant",
                    f"▶️ Taking action '{selection.action.name}' with parameters: {selection.action.parameters}",
                )

            except Exception as e:
                console.print(f"Response failed to parse: {e}", style="red")
                raise

            # The agent will return 'result' if it believes it's finished
            if selection.action.name == "result":
                console.print("final result: ", style="green")
                console.print(JSON.from_data(selection.action.parameters))
                task.post_message(
                    "assistant",
                    f"✅ I think the task is done, please review the result: {selection.action.parameters['value']}",
                )
                task.status = "review"
                task.save()
                return _thread, True

            # Find the selected action in the tool
            action = desktop.find_action(selection.action.name)
            console.print(f"found action: {action}", style="blue")
            if not action:
                console.print(f"action returned not found: {selection.action.name}")
                raise SystemError("action not found")

            # Take the selected action
            try:
                action_response = desktop.use(action, **selection.action.parameters)
            except Exception as e:
                raise ValueError(f"Trouble using action: {e}")

            console.print(f"action output: {action_response}", style="blue")
            if action_response:
                task.post_message(
                    "assistant", f"👁️ Result from taking action: {action_response}"
                )

            # Record the action for feedback and tuning
            task.record_action(
                prompt=response.prompt_id,
                action=selection.action,
                tool=desktop.ref(),
                result=action_response,
                agent_id=self.name(),
                model="TODO",
            )

            _thread.add_msg(response.msg)
            return _thread, False

        except Exception as e:
            print("Exception taking action: ", e)
            traceback.print_exc()
            task.post_message("assistant", f"⚠️ Error taking action: {e} -- retrying...")
            raise e

    @classmethod
    def supported_devices(cls) -> List[Type[Device]]:
        """Devices this agent supports

        Returns:
            List[Type[Device]]: A list of supported devices
        """
        return [Desktop]

    @classmethod
    def config_type(cls) -> Type[SurfPizzaConfig]:
        """Type of config

        Returns:
            Type[DinoConfig]: Config type
        """
        return SurfPizzaConfig

    @classmethod
    def from_config(cls, config: SurfPizzaConfig) -> "SurfPizza":
        """Create an agent from a config

        Args:
            config (DinoConfig): Agent config

        Returns:
            SurfPizza: The agent
        """
        return SurfPizza()

    @classmethod
    def default(cls) -> "SurfPizza":
        """Create a default agent

        Returns:
            SurfPizza: The agent
        """
        return SurfPizza()

    @classmethod
    def init(cls) -> None:
        """Initialize the agent class"""
        # <INITIALIZE AGENT HERE>
        return


Agent = SurfPizza
