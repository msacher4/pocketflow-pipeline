from .script_generator import ScriptGeneratorNode
from .alt_script_generator import AltScriptGeneratorNode
from .vo_coherence_review import VoCoherenceReviewNode
from .thinking_agent import ThinkingAgentNode
from .pydantic_validation import PydanticScriptValidationNode, AltPydanticScriptValidationNode
from .script_fixer import ScriptFixerNode
from nodes.validation.brainstorm_node import BrainstormValidationNode

from pocketflow import AsyncFlow
from nodes.base import ValidationSubFlowNode
from nodes.validation import build_sw_validation_flow, build_sw_alt_validation_flow


def build_scriptwriter_flow() -> AsyncFlow:
    sg = ScriptGeneratorNode()
    pv = PydanticScriptValidationNode()
    validate = ValidationSubFlowNode(
        "validate_sw", build_sw_validation_flow,
        inputs=["topic", "selected_video", "video_analysis", "script", "_feedback_attempts"],
        outputs=["script", "_feedback_attempts", "script_feedback", "user_feedback"],
    )
    sg >> pv >> validate
    pv - "reformat" >> sg
    validate - "scriptwriter" >> sg
    return AsyncFlow(start=sg)


def build_alt_scriptwriter_flow() -> AsyncFlow:
    think = ThinkingAgentNode()
    brainstorm = BrainstormValidationNode()
    sg = AltScriptGeneratorNode()
    vo_review = VoCoherenceReviewNode()
    pv = AltPydanticScriptValidationNode()
    fixer = ScriptFixerNode()
    validate = ValidationSubFlowNode(
        "validate_sw_alt", build_sw_alt_validation_flow,
        inputs=["topic", "selected_article", "script", "_feedback_attempts"],
        outputs=["script", "_feedback_attempts", "script_feedback", "user_feedback"],
    )
    think >> brainstorm >> sg >> vo_review >> pv >> validate
    # Routage vers le NODE FUSIONNÉ UNIQUE : toutes les erreurs réparables
    # (VO trop longue ET Video: fautive bracket/statique/headcount JEV) →
    # ScriptFixer (réécriture ciblée + AUTO-validation des corrections).
    # En cas de NON-convergence après N tentatives, le fixer ESCALADE vers
    # AltSG (régénération complète) au lieu de reboucler → boucle cassée.
    pv - "reformat_vo" >> fixer
    pv - "reformat_video" >> fixer
    pv - "reformat_timing" >> fixer
    pv - "reformat_sg" >> sg
    fixer >> pv
    fixer - "reformat_sg" >> sg
    # Retry réel du fixer : si l'auto-validation échoue (1ʳᵉ passe), on
    # reboucle sur le node avec l'erreur exacte au lieu d'accepter à tort.
    fixer - "refix" >> fixer
    validate - "scriptwriter_alt" >> sg
    # Édition directe (✏️) : le script édité repasse par la gate pydantic, puis
    # re-validation Telegram (approve) avant de repartir vers AssetFinder.
    validate - "edit" >> pv
    return AsyncFlow(start=think)
