from .video_download import VideoDownloadNode
from .video_analysis_llm import VideoAnalysisLLMNode
from .video_cleanup import VideoCleanupNode

from pocketflow import AsyncFlow


def build_video_analysis_flow() -> AsyncFlow:
    dl = VideoDownloadNode()
    llm = VideoAnalysisLLMNode()
    cleanup = VideoCleanupNode()
    dl >> llm >> cleanup
    return AsyncFlow(start=dl)
