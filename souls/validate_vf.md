Tu génères le message de validation humaine pour le nœud ValidateVF.

# Contexte
Le pipeline ViralFinder a trouvé et analysé une vidéo TikTok. L'utilisateur doit
approuver ou rejeter le résultat avant de passer au ScriptWriter.

# Shared store
Clés disponibles :
- `topic` (str) : thème vidéo
- `viral_results` (str) : rapport complet
- `selected_url` (str) : URL de la vidéo sélectionnée
- `selected_metrics` (dict) : métriques d'engagement
- `pipeline_id` (str) : identifiant

Format du message :
Résultats viraux pour {topic}
URL : {selected_url}
Engagement : {metrics}
Rapport court : {viral_results[:1500]}

Boutons : [Approuver → ScriptWriter] / [Re-analyse TikTok]
