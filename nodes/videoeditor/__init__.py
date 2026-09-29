from .planner import VideoEditorPlannerNode
from .prepare import VideoEditorPrepareNode
from .clip import VideoEditorClipNode
from .assemble import VideoEditorAssembleNode
from .subtitle import SubtitleNode
from .finalize import VideoEditorFinalizeNode

from pocketflow import AsyncFlow


def build_videoeditor_flow() -> AsyncFlow:
    planner = VideoEditorPlannerNode()
    prepare = VideoEditorPrepareNode(output_root="output")
    clip = VideoEditorClipNode()
    assemble = VideoEditorAssembleNode()
    subtitle = SubtitleNode()
    finalize = VideoEditorFinalizeNode()

    planner >> prepare >> clip >> assemble >> subtitle >> finalize

    return AsyncFlow(start=planner)
