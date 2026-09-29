import logging
import os

log = logging.getLogger("pocketflow-pipeline")


def _remap_montage_paths(shared: dict, upscaled_videos: list[dict]) -> None:
    """Après upscale, les clips sont renommés (clip_N_raw.webm -> clip_N.webm).

    Le montage_structure produit par MontagePlanner référence les chemins
    PRE-upscale : on ré-aligne chaque segment sur la nouvelle video_path."""

    structure = shared.get("montage_structure")
    if not isinstance(structure, dict) or isinstance(structure.get("segments"), list) is False:
        return

    new_paths = {}
    for v in upscaled_videos:
        sid = v.get("slot_id")
        p = v.get("video_path", "")
        if p:
            new_paths[sid] = os.path.abspath(p)

    for seg in structure.get("segments", []):
        idx = seg.get("index")
        new = new_paths.get(idx)
        if new and new != os.path.abspath(seg.get("file", "")):
            log.info(f"Montage remap: segment #{idx} -> {os.path.basename(new)}")
            seg["file"] = new