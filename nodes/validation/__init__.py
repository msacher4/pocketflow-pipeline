from .send_node import TGSendValidationNode
from .feedback_node import FeedbackInterpreterNode, ScriptEditFeedbackNode

from pocketflow import AsyncFlow


def _fmt_vf(shared):
    sv = shared.get("selected_video", {})
    vid = f"{shared.get('pipeline_id', '?')}_pf"
    text = (
        f"Vidéo sélectionnée à valider\n\n"
        f"Topic: {shared.get('topic', '?')}\n"
        f"URL: {sv.get('url', '?')}\n"
        f"Description: {sv.get('description', '?')}"
    )
    return text, vid


def _fmt_vf_proposal(shared):
    sv = shared.get("selected_video", {})
    return (
        f"Topic: {shared.get('topic', '?')}\n"
        f"URL: {sv.get('url', '?')}\n"
        f"Description: {sv.get('description', '?')}"
    )


def build_vf_validation_flow() -> AsyncFlow:
    send = TGSendValidationNode(
        step_name="validate_vf",
        format_msg=_fmt_vf,
        buttons_config=[
            [("Approve -> ScriptWriter", "approve")],
            [("Rejeter avec feedback", "reject")],
        ],
    )
    feedback = FeedbackInterpreterNode(
        step_name="validate_vf",
        format_proposal=_fmt_vf_proposal,
        default_target="search",
        allowed_targets=["search", "analyst"],
        feedback_key="video_feedback",
    )
    send - "reject" >> feedback
    return AsyncFlow(start=send)


def _fmt_sw(shared):
    sv = shared.get("selected_video", {})
    vid = f"{shared.get('pipeline_id', '?')}_pf_sw"
    text = (
        f"Script généré à valider\n\n"
        f"Topic: {shared.get('topic', '?')}\n"
        f"URL source: {sv.get('url', '?')}\n"
        f"Script:\n{shared.get('script', '')}"
    )
    return text, vid


def _fmt_sw_proposal(shared):
    sv = shared.get("selected_video", {})
    return (
        f"Topic: {shared.get('topic', '?')}\n"
        f"URL source: {sv.get('url', '?')}\n"
        f"Script:\n{shared.get('script', '')}"
    )


def build_sw_validation_flow() -> AsyncFlow:
    send = TGSendValidationNode(
        step_name="validate_sw",
        format_msg=_fmt_sw,
        buttons_config=[
            [("Approve -> AssetFinder", "approve")],
            [("Rejeter avec feedback", "reject")],
        ],
    )
    feedback = FeedbackInterpreterNode(
        step_name="validate_sw",
        format_proposal=_fmt_sw_proposal,
        default_target="scriptwriter",
        allowed_targets=["scriptwriter"],
    )
    send - "reject" >> feedback
    return AsyncFlow(start=send)


def _fmt_sw_alt(shared):
    article = shared.get("selected_article", {})
    vid = f"{shared.get('pipeline_id', '?')}_pf_sw_alt"
    text = (
        f"Script (mode actu) à valider\n\n"
        f"Topic: {shared.get('topic', '?')}\n"
        f"Article: {article.get('title', '?')}\n"
        f"Script:\n{shared.get('script', '')}"
    )
    return text, vid


def _fmt_sw_alt_proposal(shared):
    article = shared.get("selected_article", {})
    return (
        f"Topic: {shared.get('topic', '?')}\n"
        f"Article: {article.get('title', '?')}\n"
        f"Script:\n{shared.get('script', '')}"
    )


def build_sw_alt_validation_flow() -> AsyncFlow:
    send = TGSendValidationNode(
        step_name="validate_sw_alt",
        format_msg=_fmt_sw_alt,
        buttons_config=[
            [("Approve -> AssetFinder", "approve")],
            [("✏️ Éditer le script", "edit")],
            [("Rejeter avec feedback", "reject")],
        ],
    )
    feedback = FeedbackInterpreterNode(
        step_name="validate_sw_alt",
        format_proposal=_fmt_sw_alt_proposal,
        default_target="scriptwriter_alt",
        allowed_targets=["scriptwriter_alt"],
    )
    edit = ScriptEditFeedbackNode(step_name="validate_sw_alt")
    send - "reject" >> feedback
    send - "edit" >> edit
    return AsyncFlow(start=send)


def _fmt_af_proposal(shared):
    return (
        f"Topic: {shared.get('topic', '?')}\n"
        f"Script:\n{shared.get('script', '')}\n\n"
        f"Plan:\n{shared.get('assets', '')}"
    )


def _fmt_af_news(shared):
    art = shared.get("selected_article", {})
    vid = f"{shared.get('pipeline_id', '?')}_pf_af_news"
    perso = art.get("character_name", "")
    franchise = art.get("franchise", "")
    perso_line = f"Personnage: {perso}" + (f" ({franchise})" if franchise else "") if perso else ""
    text = (
        f"📰 News à valider\n\n"
        f"Topic: {shared.get('topic', '?')}\n"
        f"Titre: {art.get('title', '?')}\n"
        f"Source: {art.get('source', '?')}\n"
        f"Score: {art.get('score', '?')}\n"
        + (f"{perso_line}\n" if perso_line else "")
        + f"Angle: {art.get('hook_angle', '?')}"
    )
    return text, vid


def build_af_news_validation_flow() -> AsyncFlow:
    send = TGSendValidationNode(
        step_name="validate_af_news",
        format_msg=_fmt_af_news,
        buttons_config=[
            [("Approve -> ScriptWriterInfoMissed", "approve")],
            [("Rejeter", "reject")],
        ],
    )
    feedback = FeedbackInterpreterNode(
        step_name="validate_af_news",
        format_proposal=_fmt_af_news,
        default_target="actufinder_llm_select",
        allowed_targets=["actufinder_llm_select"],
        feedback_key="news_feedback",
    )
    send - "reject" >> feedback
    return AsyncFlow(start=send)


def build_af_validation_flow() -> AsyncFlow:
    from nodes.assetfinder.validate_images import ValidateImages

    send = ValidateImages()
    feedback = FeedbackInterpreterNode(
        step_name="validate_af",
        format_proposal=_fmt_af_proposal,
        default_target="sdcpp_video_gen",
        allowed_targets=["sdcpp_video_gen", "i2v", "music_generator", "voice_generator"],
        feedback_key="af_feedback",
    )
    send - "reject" >> feedback
    return AsyncFlow(start=send)
