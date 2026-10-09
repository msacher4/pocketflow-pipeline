from .asset_planner import AssetPlannerNode, AssetPlannerAltNode
from .cleanup_llama_proxy import CleanupLlamaProxy
from .cleanup_llama_jev import CleanupLlamaJev
from .combined_cleanup import CombinedCleanup
from .init_cleanup import InitCleanup
from .sdcpp_video_generator import SDCppVideoGenerator
from .comfyui_free_memory import ComfyUIFreeMemory
from .comfyui_start import ComfyUIStart
from .comfyui_upscaler import ComfyUIUpscaler
from .ffmpeg_upscaler import FfmpegUpscaler
from .music_generator import MusicGenerator
from .voice_generator import VoiceGenerator
from .montage_planner import MontagePlannerNode
from .sdcpp_i2v_generator import SDCppI2VNode
from .comfyui_image_generator import ComfyUIImageGenerator
from .comfyui_klein_ref_generator import ComfyUIKleinRefImageGenerator
from .real_character_image import RealCharacterImageNode
from .retry_no_image import RetryNoImage
from .alt_i2v import AltI2VNode
from .rewrite_i2v_prompt import RewriteI2VPromptNode
from .validate_i2v import ValidateI2V
from .validate_character_refs import ValidateCharacterRefs

from pocketflow import AsyncFlow
from nodes.base import ValidationSubFlowNode
from nodes.validation import build_af_validation_flow


def build_assetfinder_flow() -> AsyncFlow:
    init = InitCleanup()
    ap = AssetPlannerNode()
    cleanup = CleanupLlamaProxy(step="assetfinder_cleanup_llama")
    mp = MontagePlannerNode()

    svg = SDCppVideoGenerator()
    free_svg = ComfyUIFreeMemory(step="comfyui_free_svg", stop_service=True)
    start_comfyui = ComfyUIStart(step="comfyui_start_after_svg")
    free2 = ComfyUIFreeMemory(step="comfyui_free_ltx")
    upscale = ComfyUIUpscaler()
    free_audio = ComfyUIFreeMemory(step="comfyui_free_audio")
    upscale_lanczos = FfmpegUpscaler()
    music = MusicGenerator()
    voice = VoiceGenerator()

    regen_cleanup = CombinedCleanup()
    i2v_cleanup = CombinedCleanup(step="i2v_cleanup")
    i2v_video_cleanup = CombinedCleanup(step="i2v_video_cleanup")
    cleanup_upscale = CombinedCleanup(step="cleanup_upscale", route_to_generator=False)
    start_upscale_comfyui = ComfyUIStart(step="comfyui_start_upscale")
    image_gen = ComfyUIImageGenerator()
    sdcpp_i2v = SDCppI2VNode()

    validate = ValidationSubFlowNode(
        "validate_af", build_af_validation_flow,
        inputs=["topic", "script", "assets", "_feedback_attempts", "generated_videos", "downloaded_audio",
                "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
        outputs=["assets", "_feedback_attempts", "af_feedback", "user_feedback", "generated_videos", "downloaded_audio",
                 "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                 "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
    )

    init >> ap >> cleanup >> free_svg >> svg >> start_comfyui >> free2 >> free_audio >> music >> voice >> mp >> validate
    validate - "approve" >> cleanup_upscale
    cleanup_upscale - "default" >> start_upscale_comfyui
    start_upscale_comfyui - "default" >> upscale >> upscale_lanczos
    validate - "sdcpp_video_gen" >> regen_cleanup
    validate - "music_generator" >> regen_cleanup
    validate - "voice_generator" >> regen_cleanup
    validate - "i2v" >> i2v_cleanup
    validate - "i2v_video" >> i2v_video_cleanup
    regen_cleanup - "sdcpp_video_gen" >> svg
    regen_cleanup - "music_generator" >> music
    regen_cleanup - "voice_generator" >> voice
    i2v_cleanup - "i2v" >> image_gen
    i2v_video_cleanup - "i2v_video" >> sdcpp_i2v
    image_gen - "default" >> validate
    svg - "regen_done" >> start_comfyui
    sdcpp_i2v - "regen_done" >> validate
    start_comfyui - "regen_done" >> validate
    start_comfyui - "default" >> free2
    music - "regen_done" >> validate
    voice - "regen_done" >> validate

    return AsyncFlow(start=init)


def build_assetfinder_alt_flow() -> AsyncFlow:
    """Boucle AssetFinder pour le chemin alt (ActuFinder → ScriptWriterInfoMissed).

    Comme build_assetfinder_flow, MAIS :
    - AssetPlannerAltNode marque les 2 premiers slots visuels en I2V ;
    - RealCharacterImageNode récupère les images RÉELLES du personnage (2 par
      slot, utilisées comme références d'identité, pas comme frames) ;
    - ComfyUIKleinRefImageGenerator produit l'image I2V à partir de ces
      références : le prompt du slot décrit la scène, Klein dessine un visuel
      neuf qui ressemble au personnage ;
    - RewriteI2VPromptNode réécrit les prompts I2V pour qu'ils correspondent aux
      images réellement sélectionnées (sinon le modèle vidéo déforme la frame 0) ;
    - ValidateI2V envoie chaque image + prompt réécrit sur Telegram (boutons OK/KO)
      et reboucle sur les seuls slots rejetés (regen_image → real, regen_prompt → rewrite) ;
    - SDCppVideoGenerator (T2V) ignore les slots i2v, qui sont générés par
      AltI2VNode (image Klein → vidéo, automatique) ;
    - les slots i2v en échec retombent en T2V via un second passage svg.
    La regen I2V Telegram (validate -> image_gen -> sdcpp_i2v) est conservée :
    le bouton de regen du montage repasse par Klein SANS référence (txt2img),
    donc volontairement sans continuité d'identité avec les photos de référence.
    """
    init = InitCleanup(step="init_cleanup_alt")
    ap = AssetPlannerAltNode()
    real = RealCharacterImageNode()
    validate_refs = ValidateCharacterRefs()
    retry_no_image = RetryNoImage()
    klein_ref = ComfyUIKleinRefImageGenerator()
    rewrite_i2v_prompt = RewriteI2VPromptNode()
    validate_i2v = ValidateI2V()
    cleanup_jev = CleanupLlamaJev()
    mp = MontagePlannerNode()

    svg = SDCppVideoGenerator()
    alt_i2v = AltI2VNode()
    free_svg = ComfyUIFreeMemory(step="comfyui_free_svg", stop_service=True)
    start_comfyui = ComfyUIStart(step="comfyui_start_after_svg")
    free2 = ComfyUIFreeMemory(step="comfyui_free_ltx")
    upscale = ComfyUIUpscaler()
    free_audio = ComfyUIFreeMemory(step="comfyui_free_audio")
    upscale_lanczos = FfmpegUpscaler()
    music = MusicGenerator()
    voice = VoiceGenerator()

    regen_cleanup = CombinedCleanup()
    i2v_cleanup = CombinedCleanup(step="i2v_cleanup")
    i2v_video_cleanup = CombinedCleanup(step="i2v_video_cleanup")
    cleanup_upscale = CombinedCleanup(step="cleanup_upscale", route_to_generator=False)
    start_upscale_comfyui = ComfyUIStart(step="comfyui_start_upscale")
    image_gen = ComfyUIImageGenerator()
    sdcpp_i2v = SDCppI2VNode()

    validate = ValidationSubFlowNode(
        "validate_af", build_af_validation_flow,
        inputs=["topic", "script", "assets", "_feedback_attempts", "generated_videos", "downloaded_audio",
                "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
        outputs=["assets", "_feedback_attempts", "af_feedback", "user_feedback", "generated_videos", "downloaded_audio",
                 "generated_images", "_pending_reject_queue", "_rejected_slot", "_rejected_slot_label", "_rejected_slot_type",
                 "_slots_to_regenerate", "_video_action", "_i2v_phase", "_i2v_image_prompt"],
    )

    # cleanup_jev (llama-proxy 8080 + Jev-Omni 8977) passe AVANT klein_ref :
    # ComfyUI monte a ~15.9 Go sur 16 Go, il faut la VRAM des deux serveurs.
    # CleanupLlamaProxy ne cleans que le proxy — d'ou un noeud dedie, pour que
    # le graphe montre que Jev tombe aussi.
    # `real` s'execute avant (il reveille Jev-Omni sur son instance dediee 8977)
    # et rewrite_i2v_prompt apres (il reveille le modele vision a la demande,
    # llama-server le charge tout seul).
    #
    # validate_refs est entre real et klein_ref : c'est le dernier filet avant
    # generation. Jev-Omni ne sait dire que "un seul personnage ou pas" — dire si
    # c'est LE bon personnage reste un jugement humain, donc on ne lance pas Klein
    # sans ton accord Telegram. Un rejet reboucle vers `real`, qui blacklist l'URL
    # et pioche le candidat suivant.
    init >> ap >> real >> validate_refs >> cleanup_jev >> klein_ref >> rewrite_i2v_prompt >> validate_i2v >> free_svg >> svg
    # Slot I2V sans image reelle -> on re-selectionne un autre article
    # (AltRetryGate -> actufinder, borne a ALT_RETRY_MAX). Sans ce edge le
    # sous-flux s'arretait sur "Flow ends: 'retry_no_image' not found".
    real - "retry_no_image" >> retry_no_image
    validate_refs - "reject_refs" >> real
    validate_i2v - "regen_image" >> real
    validate_i2v - "regen_prompt" >> rewrite_i2v_prompt
    svg - "default" >> alt_i2v
    alt_i2v - "default" >> start_comfyui
    alt_i2v - "regen" >> svg
    start_comfyui >> free2 >> free_audio >> music >> voice >> mp >> validate
    validate - "approve" >> cleanup_upscale
    cleanup_upscale - "default" >> start_upscale_comfyui
    start_upscale_comfyui - "default" >> upscale >> upscale_lanczos
    validate - "sdcpp_video_gen" >> regen_cleanup
    validate - "music_generator" >> regen_cleanup
    validate - "voice_generator" >> regen_cleanup
    validate - "i2v" >> i2v_cleanup
    validate - "i2v_video" >> i2v_video_cleanup
    regen_cleanup - "sdcpp_video_gen" >> svg
    regen_cleanup - "music_generator" >> music
    regen_cleanup - "voice_generator" >> voice
    i2v_cleanup - "i2v" >> image_gen
    i2v_video_cleanup - "i2v_video" >> sdcpp_i2v
    image_gen - "default" >> validate
    sdcpp_i2v - "regen_done" >> validate
    svg - "regen_done" >> start_comfyui
    start_comfyui - "regen_done" >> validate
    music - "regen_done" >> validate
    voice - "regen_done" >> validate

    return AsyncFlow(start=init)
