from typing import Annotated, Literal
import json

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


Actuator = Literal["led", "buzzer", "relay1", "relay2"]
MAX_FRAME = 8192
SAMPLE_INTERVAL = 2.0
FRESH_SECONDS = 8.0
COMMAND_TIMEOUT = 5.0
DRAFT_SECONDS = 90


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Hello(Frame):
    type: Literal["hello"]
    device_id: str = Field(min_length=1, max_length=48)
    token: str = Field(min_length=1, max_length=256)
    outputs: list[Actuator] = Field(max_length=4)


class Telemetry(Frame):
    type: Literal["telemetry"]
    seq: Annotated[StrictInt, Field(ge=0, le=2_147_483_647)]
    temperature: Annotated[float, Field(ge=-40, le=125, strict=True)] | None
    humidity: Annotated[float, Field(ge=0, le=100, strict=True)] | None
    light: Annotated[StrictInt, Field(ge=0, le=4095)] | None
    presence: StrictBool | None
    obstruction: StrictBool | None
    outputs: dict[Actuator, StrictInt]


class Receipt(Frame):
    type: Literal["command_ack"]
    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    ok: StrictBool
    outputs: dict[Actuator, StrictInt]
    detail: str = Field(default="", max_length=200)


def allowed_value(actuator: str, value: int) -> bool:
    return 0 <= value <= (100 if actuator == "led" else 1)


def valid_output_states(outputs: dict, expected: set[str]) -> bool:
    return outputs.keys() == expected and all(allowed_value(key, value) for key, value in outputs.items())


def encode(frame: dict) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
