"""Sérialise le graphe PocketFlow en JSON pour la vue Svelte.

Parcourt le flow réel (start_node + successors) et renvoie nodes + edges avec
les stepIds utilisés dans shared["steps"]. Permet à la vue Svelte de refléter
automatiquement les changements de wiring du pipeline.

Conventions (alignées sur la vue) :
- Un SubFlowNode est un nœud "conteneur" (id = son step_name). On ajoute un
  edge conteneur → premier nœud interne, puis on explore les internes.
- Les ids sont les stepIds réels (ex: "viralfinder", "tikhub_search").
"""
import re

from pocketflow import Flow

NODE_TYPES = {
    "reset":                    {"icon": "⏰", "label": "Reset",                "model": ""},
    "router":                   {"icon": "🔀", "label": "PathFinder",           "model": ""},
    "route_choice":             {"icon": "🔀", "label": "PathFinder",           "model": ""},
    "vf":                       {"icon": "🔍", "label": "ViralFinder",          "model": ""},
    "viralfinder":              {"icon": "🔍", "label": "ViralFinder",          "model": ""},
    "actufinder":               {"icon": "📰", "label": "ActuFinder",           "model": ""},
    "actufinder_fetch_all":     {"icon": "📡", "label": "Fetch All Feeds",      "model": ""},
    "actufinder_decider_score": {"icon": "🧮", "label": "Decider Score",        "model": "decider-4b"},
    "actufinder_filter":        {"icon": "🔎", "label": "Filter",               "model": ""},
    "actufinder_llm_select":    {"icon": "🧠", "label": "LLM Select",           "model": "qwen-opus"},
    "actufinder_fetch_content": {"icon": "📄", "label": "Fetch Article",       "model": ""},
    "actufinder_fetch_browser": {"icon": "🌐", "label": "Fetch Browser",       "model": ""},
    "actufinder_synthesize":    {"icon": "🧬", "label": "Synthesize Article",  "model": "qwen-opus"},
    "actufinder_title_gate":    {"icon": "🎯", "label": "Gate Personnage",     "model": "qwen-opus"},
    
    "pydantic_af_validation":   {"icon": "✅", "label": "Pydantic AF",          "model": ""},
    "video_analysis":           {"icon": "🎥", "label": "VideoAnalysis",       "model": ""},
    "scriptwriter":             {"icon": "✍️", "label": "ScriptWriter",         "model": ""},
    "scriptwriter_alt":         {"icon": "📝", "label": "ScriptWriterInfoMissed", "model": ""},
    "assetfinder":              {"icon": "🎯", "label": "AssetFinder",          "model": ""},
    "assetfinder_alt":          {"icon": "🎯", "label": "AssetFinder (Imgs)",   "model": ""},
    "alt_retry_gate":           {"icon": "🔁", "label": "Alt Retry Gate",       "model": ""},
    "videoeditor":              {"icon": "🎬", "label": "VideoEditor",          "model": ""},
    # ViralFinder subflow
    "tikhub_search":            {"icon": "🔍", "label": "TikHub",               "model": "qwen-opus"},
    "tiktok_analyst":           {"icon": "📊", "label": "Analyst",              "model": "qwen-opus"},
    "pydantic_validation":      {"icon": "✅", "label": "Pydantic",             "model": ""},
    "validate_vf":              {"icon": "👤", "label": "Validation VF",        "model": "Telegram"},
    # VideoAnalysis subflow
    "video_download":           {"icon": "⬇️", "label": "VideoDownload",       "model": ""},
    "video_analysis_llm":       {"icon": "🧠", "label": "AnalysisLLM",         "model": "gemma4-12b"},
    "video_cleanup":            {"icon": "🧹", "label": "Cleanup",              "model": ""},
    # ScriptWriter subflow
    "script_generator":         {"icon": "📝", "label": "ScriptGen",           "model": "qwen3.6q2"},
    "thinking_agent":           {"icon": "🧠", "label": "Thinking Agent",      "model": "qwen3.8-27b"},
    "brainstorm_thinking":      {"icon": "💬", "label": "Brainstorm TG",       "model": "qwen3.8-27b"},
    "alt_script_generator":     {"icon": "📝", "label": "SW InfoMissed Gen",   "model": "qwen3.8-27b"},
    "vo_coherence_review":      {"icon": "🛡️", "label": "VO Coherence Review",  "model": "qwen3.8-27b"},
    "vo_shortener":             {"icon": "✂️", "label": "VO Shortener",        "model": "qwen3.8-27b"},
    "video_asset_fixer":        {"icon": "🎬", "label": "Video Fix",            "model": "qwen3.8-27b"},
    "script_fixer":             {"icon": "🔧", "label": "ScriptFixer",          "model": "qwen3.8-27b"},
    "pydantic_script_validation": {"icon": "✅", "label": "Pydantic SW",       "model": ""},
    "pydantic_script_validation_alt": {"icon": "✅", "label": "Pydantic SW (alt)", "model": ""},
    "validate_sw":              {"icon": "👤", "label": "Validation SW",        "model": "Telegram"},
    "validate_sw_alt":          {"icon": "👤", "label": "Validation SW (alt)",  "model": "Telegram"},
    # AssetFinder subflow
    "asset_planner":            {"icon": "📋", "label": "AssetPlanner",         "model": "qwen-opus"},
    "asset_planner_alt":        {"icon": "📋", "label": "Planner Alt (2x I2V)", "model": "qwen-opus"},
    "real_character_image":     {"icon": "🖼️", "label": "Real Character Img",   "model": "icrawler"},
    "validate_character_refs":  {"icon": "👤", "label": "Validation Perso",     "model": "Telegram"},
    "rewrite_i2v_prompt":       {"icon": "✏️", "label": "Rewrite I2V Prompt",   "model": "qwen3.6q2"},
    "validate_i2v":             {"icon": "👤", "label": "Validation I2V",       "model": "Telegram"},
    "alt_i2_v":                 {"icon": "🎥", "label": "Alt I2V",              "model": "LTX-Video 2.3"},
    "cleanup_llama_proxy":      {"icon": "🧹", "label": "CleanupLlama",         "model": ""},
    "cleanup_llama_jev":        {"icon": "🧹", "label": "Cleanup Llama+Jev",    "model": ""},
    "retry_no_image":           {"icon": "🔁", "label": "Retry No Image",       "model": ""},
    "combined_cleanup":         {"icon": "🧹", "label": "Cleanup All",          "model": ""},
    "i2v_cleanup":             {"icon": "🧹", "label": "I2V Cleanup",          "model": ""},
    "i2v_video_cleanup":       {"icon": "🧹", "label": "I2V Vid Cleanup",      "model": ""},
    "cleanup_upscale":         {"icon": "🧹", "label": "Cleanup All",          "model": ""},
    "comfyui_start_upscale":   {"icon": "🚀", "label": "ComfyUI Start",        "model": ""},
    "sdcpp_i2v_gen":           {"icon": "🎥", "label": "SDCpp I2V",            "model": "LTX-Video 2.3"},
    "comfyui_image_gen":       {"icon": "🖼️", "label": "Gen Image",            "model": "Klein 4B"},
    "comfyui_klein_ref_gen":   {"icon": "🖼️", "label": "Gen Image (Réf.)",    "model": "Klein 4B"},
    "init_cleanup":             {"icon": "🧹", "label": "Init Cleanup",          "model": ""},
    "init_cleanup_alt":         {"icon": "🧹", "label": "Init Cleanup (alt)",    "model": ""},
    "sdcpp_video_gen":          {"icon": "🎥", "label": "SDCpp Video",          "model": "LTX-Video"},
    "comfyui_free_ltx":         {"icon": "🧹", "label": "ComfyUI Free",        "model": ""},
    "comfyui_upscale":          {"icon": "✨", "label": "Upscale RIFE",         "model": ""},
    "comfyui_free_audio":       {"icon": "🧹", "label": "Free Audio",          "model": ""},
    "ffmpeg_upscaler":          {"icon": "📐", "label": "Upscale FFmpeg",       "model": "lanczos"},
    "music_generator":          {"icon": "🎶", "label": "Music (ACE-Step)",     "model": "ACE-Step 1.5"},
    "voice_generator":          {"icon": "🗣️", "label": "Voice (FishSpeech)",  "model": "Fish Speech S2"},
    "montage_planner":          {"icon": "📋", "label": "MontagePlanner",       "model": "qwen-opus"},
    "montage_critic":           {"icon": "🔍", "label": "MontageCritic",        "model": "qwen-opus"},
    "validate_af":              {"icon": "👤", "label": "Validation AF",        "model": "Telegram"},
    "validate_af_news":         {"icon": "👤", "label": "Validation News AF",    "model": "Telegram"},
    "comfyui_free_svg":         {"icon": "🧹", "label": "Free SVG",             "model": ""},
    "comfyui_start_after_svg":  {"icon": "🚀", "label": "ComfyUI Start",        "model": ""},
    # VideoEditor subflow
    "videoeditor_planner":      {"icon": "📋", "label": "VE Planner",          "model": "qwen-opus"},
    "videoeditor_prepare":      {"icon": "🔧", "label": "VE Prepare",          "model": ""},
    "videoeditor_clip":         {"icon": "✂️", "label": "VE Clip",             "model": ""},
    "videoeditor_assemble":     {"icon": "🎞️", "label": "VE Assemble",        "model": ""},
    "videoeditor_subtitle":     {"icon": "💬", "label": "VE Subtitle",         "model": "whisper"},
    "videoeditor_finalize":     {"icon": "🏁", "label": "VE Finalize",         "model": ""},
}


def _class_to_stepid(node) -> str:
    """Dérive un stepId depuis le nom de la classe (snake_case)."""
    name = type(node).__name__
    name = re.sub(r"Node$", "", name)
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _node_stepid(node) -> str:
    """Résout le stepId d'un nœud selon sa nature."""
    if getattr(node, "step_name", None):
        return node.step_name
    if getattr(node, "step", None):
        return node.step
    overrides = {
        "SDCppVideoGenerator": "sdcpp_video_gen",
        "VideoControlNetNode": "video_controlnet",
        "SDCppI2VNode": "sdcpp_i2v_gen",
        "ComfyUIImageGenerator": "comfyui_image_gen",
        "ComfyUIKleinRefImageGenerator": "comfyui_klein_ref_gen",
        "AssetPlannerNode": "asset_planner",
        "MontagePlannerNode": "montage_planner",
        "MontageCriticNode": "montage_critic",
        "MusicGenerator": "music_generator",
        "VoiceGenerator": "voice_generator",
        "CleanupLlamaProxy": "cleanup_llama_proxy",
        "CleanupLlamaJev": "cleanup_llama_jev",
        "CombinedCleanup": "combined_cleanup",
        "ComfyUIUpscaler": "comfyui_upscale",
        "TikHubSearchNode": "tikhub_search",
        "TikTokAnalystNode": "tiktok_analyst",
        "PydanticValidationNode": "pydantic_validation",
        "VideoDownloadNode": "video_download",
        "VideoAnalysisLLMNode": "video_analysis_llm",
        "VideoCleanupNode": "video_cleanup",
        "ScriptGeneratorNode": "script_generator",
        "AltScriptGeneratorNode": "alt_script_generator",
        "ThinkingAgentNode": "thinking_agent",
        "BrainstormValidationNode": "brainstorm_thinking",
        "VoCoherenceReviewNode": "vo_coherence_review",
        "PydanticScriptValidationNode": "pydantic_script_validation",
        "AltPydanticScriptValidationNode": "pydantic_script_validation_alt",
        "VideoAssetFixerNode": "video_asset_fixer",
        "VideoEditorPlannerNode": "videoeditor_planner",
        "VideoEditorPrepareNode": "videoeditor_prepare",
        "VideoEditorClipNode": "videoeditor_clip",
        "VideoEditorAssembleNode": "videoeditor_assemble",
        "SubtitleNode": "videoeditor_subtitle",
        "VideoEditorFinalizeNode": "videoeditor_finalize",
        "ResetNode": "reset",
        "RouteChoiceNode": "route_choice",
        "ActuFinderNode": "actufinder",
        "FetchAllFeedsNode": "actufinder_fetch_all",
        "DeciderScoreNode": "actufinder_decider_score",
        "FilterArticlesNode": "actufinder_filter",
        "LLMSelectNode": "actufinder_llm_select",
        "PydanticAFValidationNode": "pydantic_af_validation",
        "FetchArticleContentNode": "actufinder_fetch_content",
        "BrowserFetchArticleNode": "actufinder_fetch_browser",
        "SynthesizeArticleNode": "actufinder_synthesize",
        "TitleCharacterGateNode": "actufinder_title_gate",
    }
    return overrides.get(type(node).__name__, _class_to_stepid(node))


def _is_container(node) -> bool:
    """True si le nœud est un sous-flow (SubFlowNode/ValidationSubFlowNode)."""
    return callable(getattr(node, "flow_builder", None))


class _Graph:
    def __init__(self):
        self.seen: set = set()
        self.nodes: list = []
        self.edges: list = []

    def _add_edge(self, source: str, target: str, action: str) -> None:
        label = None if action in ("default", "approve", "regen_done") else action
        for e in self.edges:
            if e["source"] == source and e["target"] == target and e["label"] == label:
                return
        self.edges.append({"id": f"e-{source}-{target}-{action}", "source": source, "target": target, "label": label})

    def _ensure_node(self, sid: str) -> None:
        if sid not in self.seen:
            self.seen.add(sid)
            self.nodes.append({"id": sid, "type": "step", "stepId": sid})

    def walk(self, node, parent_id: str | None = None, on_path: set | None = None) -> None:
        """Parcourt un nœud et ses successors (DFS), en dépliant les subflows."""
        if on_path is None:
            on_path = set()

        sid = _node_stepid(node)
        if not sid:
            return

        # Cycle detection : si le nœud est déjà dans le chemin DFS courant, stop.
        if sid in on_path:
            return

        self._ensure_node(sid)
        if parent_id and parent_id != sid:
            self._add_edge(parent_id, sid, "default")

        on_path = on_path | {sid}

        # SubFlow : explorer les nœuds internes (edge conteneur → start interne).
        if _is_container(node):
            sub = node.flow_builder()
            self.walk(sub.start_node, parent_id=sid, on_path=on_path)

        # Nœud feuille ou conteneur : successors du flow courant.
        for action, nxt in node.successors.items():
            nxt_sid = _node_stepid(nxt)
            if not nxt_sid:
                continue
            self._ensure_node(nxt_sid)
            self._add_edge(sid, nxt_sid, action)
            self.walk(nxt, on_path=on_path)


def serialize_flow(flow: Flow) -> dict:
    g = _Graph()
    g.walk(flow.start_node)
    return {"nodes": g.nodes, "edges": g.edges, "nodeTypes": NODE_TYPES}
