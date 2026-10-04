"""Validation offline du workflow Klein avec images de référence.

Aucune génération, aucun GPU : on vérifie que le graphe API est cohérent, que
tous les PARAM_* sont résolus, et que le graphe COLLE à la topologie officielle
`Image Edit (Flux.2 Klein 4B Distilled)`.

Régression majeure verrouillée ici : FLUX.2 exige `EmptyFlux2LatentImage`
(128 canaux, /16). Un `EmptyLatentImage` (4 canaux, /8) produit un latent de
forme incompatible et biaise l'échantillonnage -- c'est exactement le bug qui
avait fait perdre l'identité du personnage.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from helpers.comfyui_api import COMFYUI_INPUT, REF_IMAGE_SLOTS, _fill_params, _load_workflow

COMFYUI_MODELS = Path("/media/marcs/Linux_Apps/Projets_AI/ComfyUI/models")

WORKFLOW_NAME = "generate_image_klein_ref"

# Valeurs du modèle DISTILLED : 4 steps, cfg 1.0.
PARAMS = {
    "PARAM_PROMPT": "portrait of Aiko, brown hair, sitting on a bench, golden hour",
    "PARAM_INT_SEED": 42,
    "PARAM_INT_STEPS": 4,
    "PARAM_FLOAT_CFG": 1.0,
    "PARAM_STR_SAMPLER_NAME": "euler",
    "PARAM_INT_WIDTH": 320,
    "PARAM_INT_HEIGHT": 576,
    "PARAM_MODEL": "flux-2-klein-4b-Q8_0.gguf",
    "PARAM_REF_IMAGE_1": "ref_1.png",
    "PARAM_REF_IMAGE_2": "ref_2.png",
}

# Nœuds obligatoires de la pile d'échantillonnage FLUX.2.
OFFICIAL_SAMPLER_STACK = {
    "Flux2Scheduler",
    "RandomNoise",
    "KSamplerSelect",
    "CFGGuider",
    "SamplerCustomAdvanced",
}


def _wf():
    return _load_workflow(WORKFLOW_NAME)


def _links(node):
    return {k: v for k, v in node.get("inputs", {}).items() if isinstance(v, list) and len(v) == 2}


def _of_class(wf, class_type):
    return [k for k, v in wf.items() if v["class_type"] == class_type]


def test_all_links_resolve():
    wf = _wf()
    for node_id, node in wf.items():
        for key, target in _links(node).items():
            assert target[0] in wf, f"noeud {node_id}.{key} pointe vers {target[0]} inexistant"
    print("test_all_links_resolve PASSED")


def test_all_params_resolved():
    filled = _fill_params(_wf(), PARAMS)
    left = {
        k for node in filled.values()
        for k, v in node.get("inputs", {}).items()
        if isinstance(v, str) and v.startswith("PARAM_")
    }
    assert not left, f"PARAM_ non résolus: {sorted(left)}"
    print("test_all_params_resolved PASSED")


def test_flux2_latent_not_empty_latent():
    """RÉGRESSION : FLUX.2 veut 128 canaux /16, pas 4 canaux /8."""
    wf = _wf()
    assert not _of_class(wf, "EmptyLatentImage"), "EmptyLatentImage interdit (4ch/8) sur FLUX.2"
    ids = _of_class(wf, "EmptyFlux2LatentImage")
    assert len(ids) == 1, f"attendu 1 EmptyFlux2LatentImage, trouvé {len(ids)}"
    inputs = wf[ids[0]]["inputs"]
    assert inputs["width"] == "PARAM_INT_WIDTH"
    assert inputs["height"] == "PARAM_INT_HEIGHT"
    print("test_flux2_latent_not_empty_latent PASSED")


def test_canvas_vide():
    """Le latent envoyé au sampler doit être le latent vide, jamais le
    VAEEncode d'une référence : c'est ce qui sépare 'génération guidée par
    référence' du mode 'édition' (qui conserverait la composition Danbooru)."""
    wf = _wf()
    sampler_id, = _of_class(wf, "SamplerCustomAdvanced")
    latent_src = wf[sampler_id]["inputs"]["latent_image"][0]
    assert wf[latent_src]["class_type"] == "EmptyFlux2LatentImage", wf[latent_src]["class_type"]
    print("test_canvas_vide PASSED")


def test_official_sampler_stack():
    """La pile officielle est obligatoire, et le KSampler générique est interdit
    (il n'expose pas le scheduler flux2)."""
    wf = _wf()
    present = {v["class_type"] for v in wf.values()}
    missing = OFFICIAL_SAMPLER_STACK - present
    assert not missing, f"nœuds officiels manquants: {sorted(missing)}"
    assert not _of_class(wf, "KSampler"), "KSampler générique interdit (pas de scheduler flux2)"
    print("test_official_sampler_stack PASSED")


def test_scheduler_uses_target_resolution():
    """Flux2Scheduler reçoit width/height de la cible i2v."""
    wf = _wf()
    sched_id, = _of_class(wf, "Flux2Scheduler")
    inputs = wf[sched_id]["inputs"]
    assert inputs["steps"] == "PARAM_INT_STEPS"
    assert inputs["width"] == "PARAM_INT_WIDTH"
    assert inputs["height"] == "PARAM_INT_HEIGHT"
    print("test_scheduler_uses_target_resolution PASSED")


def test_negative_from_zeroed_positive():
    """Le blueprint distilled fait le négatif via ConditioningZeroOut, pas via un
    second encodage de texte. Il ne doit donc exister qu'UN seul CLIPTextEncode."""
    wf = _wf()
    assert len(_of_class(wf, "CLIPTextEncode")) == 1, "un seul CLIPTextEncode attendu"
    guider_id, = _of_class(wf, "CFGGuider")
    node_id = wf[guider_id]["inputs"]["negative"][0]
    while wf[node_id]["class_type"] == "ReferenceLatent":
        node_id = wf[node_id]["inputs"]["conditioning"][0]
    assert wf[node_id]["class_type"] == "ConditioningZeroOut", wf[node_id]["class_type"]
    print("test_negative_from_zeroed_positive PASSED")


def test_both_conditionings_carry_references():
    """Le blueprint officiel attache les reference latents AU POSITIF ET AU
    NÉGATIF : les tokens de référence doivent faire partie des deux séquences,
    sinon le CFG casse."""
    wf = _wf()
    guider_id, = _of_class(wf, "CFGGuider")
    tails = {"positive": "CLIPTextEncode", "negative": "ConditioningZeroOut"}
    for branch, tail in tails.items():
        chain, node_id = [], wf[guider_id]["inputs"][branch][0]
        while True:
            node = wf[node_id]
            chain.append(node["class_type"])
            if node["class_type"] != "ReferenceLatent":
                break
            node_id = node["inputs"]["conditioning"][0]
        assert chain == ["ReferenceLatent", "ReferenceLatent", tail], (branch, chain)
    print("test_both_conditionings_carry_references PASSED")


def test_reference_slots_cabled():
    wf = _wf()
    assert len(_of_class(wf, "LoadImage")) == REF_IMAGE_SLOTS
    assert len(_of_class(wf, "ImageScaleToTotalPixels")) == REF_IMAGE_SLOTS
    assert len(_of_class(wf, "VAEEncode")) == REF_IMAGE_SLOTS
    assert len(_of_class(wf, "ReferenceLatent")) == 2 * REF_IMAGE_SLOTS
    print("test_reference_slots_cabled PASSED")


def test_references_scaled_official():
    """1 MP en nearest-exact : réglage officiel. Le scaler conserve les lignes
    fines du trait anime, alors qu'un lanczos 0.75 MP les adoucit et favorise
    un rendu gras."""
    wf = _wf()
    for node_id in _of_class(wf, "ImageScaleToTotalPixels"):
        inputs = wf[node_id]["inputs"]
        assert inputs["megapixels"] == 1, inputs
        assert inputs["upscale_method"] == "nearest-exact", inputs
    print("test_references_scaled_official PASSED")


def test_models_installed():
    """Les 3 poids référencés doivent exister : pas de téléchargement."""
    wf = _wf()
    clip_id, = _of_class(wf, "CLIPLoaderGGUF")
    vae_id, = _of_class(wf, "VAELoader")
    unet = Path(COMFYUI_MODELS / "unet" / PARAMS["PARAM_MODEL"])
    clip = Path(COMFYUI_MODELS / "text_encoders" / wf[clip_id]["inputs"]["clip_name"])
    vae = Path(COMFYUI_MODELS / "vae" / wf[vae_id]["inputs"]["vae_name"])
    for m in (unet, clip, vae):
        assert m.is_file(), f"modèle absent: {m}"
    print("test_models_installed PASSED")


def test_target_resolution_patch_aligned():
    for key in ("PARAM_INT_WIDTH", "PARAM_INT_HEIGHT"):
        assert PARAMS[key] % 16 == 0, f"{key}={PARAMS[key]} pas aligné 16"
    print("test_target_resolution_patch_aligned PASSED")


if __name__ == "__main__":
    test_all_links_resolve()
    test_all_params_resolved()
    test_flux2_latent_not_empty_latent()
    test_canvas_vide()
    test_official_sampler_stack()
    test_scheduler_uses_target_resolution()
    test_negative_from_zeroed_positive()
    test_both_conditionings_carry_references()
    test_reference_slots_cabled()
    test_references_scaled_official()
    test_models_installed()
    test_target_resolution_patch_aligned()
    print("\nTous les tests du workflow Klein ref sont passés.")