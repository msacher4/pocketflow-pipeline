from .tikhub_search import TikHubSearchNode
from .tiktok_analyst import TikTokAnalystNode
from .pydantic_validation import PydanticValidationNode

from pocketflow import AsyncFlow

from nodes.base import ValidationSubFlowNode
from nodes.validation import build_vf_validation_flow


def build_viralfinder_flow() -> AsyncFlow:
    ts = TikHubSearchNode()
    ta = TikTokAnalystNode()
    pv = PydanticValidationNode()
    validate = ValidationSubFlowNode(
        "validate_vf", build_vf_validation_flow,
        inputs=["topic", "selected_video", "_feedback_attempts"],
        outputs=["selected_video", "_feedback_attempts", "video_feedback", "user_feedback"],
    )
    ts >> ta >> pv >> validate
    pv - "reformat" >> ta
    validate - "search" >> ts
    validate - "analyst" >> ta
    ta - "search" >> ts
    return AsyncFlow(start=ts)
