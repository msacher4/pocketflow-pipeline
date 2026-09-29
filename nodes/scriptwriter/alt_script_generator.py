import json
import logging
from datetime import datetime, timezone

from pocketflow import AsyncNode

from config import LLM_SCRIPTWRITER_ALT_REMOTE_MODEL, OPENCODE_LLM_URL, OPENCODE_API_KEY, OPENCODE_SESSION
from helpers.state import _set_state, _shared_snapshot, _set_traces
from helpers.call_llm import call_llm, load_soul, load_knowledge, extract_knowledge_section, _extract_json, _trace_llm

log = logging.getLogger("pocketflow-pipeline")


class AltScriptGeneratorNode(AsyncNode):
    def __init__(self):
        super().__init__(max_retries=2, wait=30)

    async def prep_async(self, shared):
        shared["_current_step"] = "alt_script_generator"
        await _set_state(**_shared_snapshot(shared))
        return shared

    async def exec_async(self, shared):
        soul = load_soul("script_generator_alt")
        article = shared.get("selected_article", {})
        reformat_error = shared.get("_reformat_error", "")
        article_content = article.get("synthesis") or article.get("description") or article.get("summary") or ""
        character = article.get("character") or {}
        thinking = shared.get("thinking_agent", {})
        structures = load_knowledge("waifu_structures")
        ctx = (
            f"Phase: EXEC — transforme cette news en vidéo TikTok virale pour "
            f"WaifuDrama (anime/gaming/personnages féminins). Base-toi sur les "
            f"FAITS de l'article (n'invente aucun fait), MAIS construis une vidéo "
            f"émotionnelle et percutante selon la structure narrative choisie par le "
            f"Thinking Agent.\n"
            f"Thème: {shared.get('topic', '')}\n"
            f"Article source: {article.get('title', '')}\n"
            f"Source: {article.get('source', '')}\n"
            f"URL: {article.get('url', '')}\n"
        )
        if structures:
            chosen = thinking.get("chosen_structure", "").strip()
            section = extract_knowledge_section(structures, chosen)
            if chosen and not section:
                log.warning(f"AltScriptGenerator: structure {chosen!r} not found, using full knowledge file")
            injected = section or structures
            ctx += (
                f"\n===== STRUCTURE NARRATIVE CHOISIE — LE TABLEAU CI-DESSOUS EST TA "
                f"NARRATION, PLAN PAR PLAN =====\n"
                f"La structure choisie par le Thinking Agent est '{chosen}'.\n"
                f"{injected}\n"
            )
            ctx += (
                f"\nRÈGLES D'EXÉCUTION DE LA STRUCTURE :\n"
                f"- Suis l'ORDRE des beats du tableau ligne par ligne (HOOK → CTA) : tu ne changes pas l'ordre des beats, tu ne les sautes pas.\n"
                f"- UN beat = UN plan. VO courte : ≈ 2 mots/sec. **MAX 2 vidéos par plan** (I2V≈3s + T2V≈4s = 7s ≈ 18 mots, 2 T2V = 8s ≈ 20 mots). Si la VO dépasse → RACCOURCIS la phrase (jamais de split, jamais plus de 2 lignes Video:).\n"
                f"- Durée d'un plan = SOMME des durées de SES assets : I2V≈3s, T2V≈4s. Il n'y a que 2 I2V par script : la 1re Video: du Plan 1 et la 1re Video: du Plan 2. Tout le reste est T2V. Réécris le titre du plan (ex: I2V+T2V = 'Plan 1 (0-7s)') et décale les horaires suivants.\n"
                f"- Avant chaque VO, calcule : ≈ 2 mots/sec. Une VO trop longue se CONDENSE, elle n'ajoute jamais de 3e vidéo.\n"
                f"- Chaque plan se remplit avec un fait réel de l'article ET le bon perso (nom + jeu/franchise).\n"
                f"- Les 'Sample VO' existent UNIQUEMENT pour te faire comprendre l'intention émotionnelle : NE LES RECOPIE JAMAIS.\n"
                f"- En cas de conflit entre ce tableau et les autres règles : LE TABLEAU GAGNE sur les beats ; la durée/le nombre d'assets (règle 8 du soul) s'adapte au tableau.\n"
                f"- Au moins 7 plans au total, sections HOOK/BODY/CTA obligatoires, aucun produit/app/lien.\n"
                f"===== FIN STRUCTURE =====\n"
            )
        if thinking:
            ctx += (
                f"\n===== THINKING AGENT (réflexion en amont) =====\n"
                f"Idée de vidéo: {thinking.get('video_idea', '')}\n"
                f"Pourquoi ça marche: {thinking.get('why_it_works', '')}\n"
                f"Public cible: {thinking.get('target_audience', '')}\n"
                f"Angle affiliation: {thinking.get('affiliate_angle', '')}\n"
                f"Insights recherches: {thinking.get('search_insights', '')}\n"
                f"Concept visuel: {thinking.get('visual_concept', '')}\n"
                f"Mécanisme viral: {thinking.get('viral_mechanism', '')}\n"
                f"Enjeu spectateur: {thinking.get('spectator_stake', '')}\n"
                f"STRUCTURE CHOISIE: {thinking.get('chosen_structure', '')}\n"
                f"Pourquoi cette structure: {thinking.get('structure_why', '')}\n"
                f"===== FIN THINKING AGENT =====\n"
            )
        if character.get("name"):
            ctx += (
                f"\n===== CHARACTER (personnage central) =====\n"
                f"name: {character.get('name')}\n"
                f"franchise: {character.get('franchise', '')}\n"
                f"Les 2 assets I2V (la 1re Video: du Plan 1 et la 1re Video: du Plan 2, quels que soient les beats qu'ils portent) doivent être un portrait "
                f"fidèle et reconnaissable de CE personnage (tenue, attributs, posture, "
                f"style visuel), décrit 100 % visuellement, sans aucun texte à l'écran.\n"
                f"===== FIN CHARACTER =====\n"
            )
        ctx += (
            f"\n--- CONTENU COMPLET DE L'ARTICLE ---\n"
            f"{article_content[:5000]}\n"
            f"--- FIN DU CONTENU ---\n"
        )
        ctx += (
            f"\nPipeline ID: {shared.get('pipeline_id', 'unknown')}\n\n"
            f"Retourne UNIQUEMENT un JSON valide, sans texte avant ni après.\n"
            f"Format : {{\"script\": \"script vidéo complet...\"}}\n"
            f"Langues : VO et Video descriptions en anglais."
        )
        if reformat_error:
            ctx += (
                f"\n\n--- REFORMAT REQUIRED ---\n"
                f"Rejeté par validation: {reformat_error}\n"
                f"Corrige le format et retourne EXACTEMENT {{\"script\": \"...\"}}"
            )
            shared["_reformat_error"] = ""
        script_feedback = shared.get("script_feedback", "")
        if script_feedback:
            ctx += (
                f"\n\n--- USER FEEDBACK (à intégrer) ---\n"
                f"{script_feedback}\n"
                f"Intègre ce feedback dans le script réécrit."
            )
            shared["script_feedback"] = ""
        llm_resp = await call_llm(LLM_SCRIPTWRITER_ALT_REMOTE_MODEL, soul, ctx, max_tokens=8192, timeout=600, url=OPENCODE_LLM_URL, api_key=OPENCODE_API_KEY, session=OPENCODE_SESSION)
        _trace_llm(shared, "alt_script_generator", "exec", LLM_SCRIPTWRITER_ALT_REMOTE_MODEL, soul, ctx, llm_resp)
        decision = _extract_json(llm_resp)
        log.info(f"AltSG EXEC -> script length={len(decision.get('script', ''))}")
        return json.dumps(decision, ensure_ascii=False)

    async def post_async(self, shared, prep, exec):
        try:
            decision = json.loads(exec)
        except json.JSONDecodeError:
            log.error("AltSG POST -> exec is not valid JSON")
            shared["_current_step"] = "alt_script_generator_error"
            shared["_error"] = "AltScriptGenerator: exec is not valid JSON"
            shared["steps"].append({
                "step": "alt_script_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AltScriptGenerator aborted: exec is not valid JSON")

        raw_script = decision.get("script", "")
        if not raw_script.strip():
            log.warning("AltSG POST -> script is empty")
            shared["_current_step"] = "alt_script_generator_error"
            shared["_error"] = "AltScriptGenerator: empty script"
            shared["steps"].append({
                "step": "alt_script_generator", "status": "error",
                "ts": datetime.now(timezone.utc).isoformat(),
                "input": shared.get("topic", ""),
                "output": str(exec)[:10000],
            })
            await _set_state(**_shared_snapshot(shared, running=False))
            await _set_traces(shared.get("_traces", {}))
            raise RuntimeError("AltScriptGenerator aborted: empty script")

        shared["script"] = raw_script
        # Script neuf = budget de réparations neuf : le compteur pydantic compte
        # les défauts du script COURANT, pas du run entier (sinon 4 défauts
        # différents — même réparés — tuent le run). Garde-fou global anti-boucle
        # ci-dessous : nombre de régénérations AltSG.
        shared["_reformat_attempts"] = 0
        shared["_sg_regens"] = shared.get("_sg_regens", 0) + 1
        shared["_current_step"] = "alt_script_generator_done"
        shared["steps"].append({
            "step": "alt_script_generator", "status": "ok",
            "ts": datetime.now(timezone.utc).isoformat(),
            "input": shared.get("topic", ""),
            "output": str(exec)[:10000],
        })
        await _set_state(**_shared_snapshot(shared))
        await _set_traces(shared.get("_traces", {}))
        return "default"
