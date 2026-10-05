"""Source de vérité unique pour le découpage I2V / T2V d'un script.

Deux appelants ont besoin de savoir EXACTEMENT quelles lignes `Video:` sont des
I2V (le personnage filmé) et lesquelles sont des T2V (b-roll) :

  - `AssetPlannerAltNode._decorate_blueprint`, qui marque `mode: i2v` sur les
    slots du blueprint ;
  - `PydanticScriptValidation ALT`, qui applique la règle b-roll (interdit le
    personnage, exige l'idée de la VO) UNIQUEMENT aux T2V.

Deux implémentations indépendantes divergeraient : le validateur bloquerait un
plan I2V, ou laisserait passer un plan T2V. D'où cette fonction unique, appelée
par les deux côtés avec les indices de plan dans l'ordre du script.

RÈGLE : l'I2V est la 1re vidéo de chacun des `I2V_SLOT_COUNT` premiers plans
qui ont au moins une vidéo. Jamais deux I2V dans le même plan — donc la 2e vidéo
d'un plan (VO longue au hook, max 2 vidéos/plan) reste un T2V, même placée ENTRE
les deux I2V.
"""

I2V_SLOT_COUNT = 2


def i2v_slot_positions(plan_indices, count: int = I2V_SLOT_COUNT) -> set:
    """Positions (0-based) des lignes `Video:` qui deviennent des I2V.

    `plan_indices` : une entrée par ligne `Video:`, dans l'ordre du script,
    valant l'indice de plan du `Plan N (` correspondant. Un `None` (plan
    illisible) est traité comme un plan à part entière, pour ne pas perdre d'I2V.

    Exemple — plan 1 avec 2 vidéos, plan 2 avec 1, plan 3 avec 1 :
        [1, 1, 2, 3] -> {0, 2}
         ^  1re vidéo du plan 1 = I2V
             ^ 2e vidéo du plan 1 = T2V (entre les deux I2V)
                ^ 1re vidéo du plan 2 = I2V
    """
    positions: set = set()
    if count <= 0:
        return positions
    seen_plans = set()
    for pos, plan_index in enumerate(plan_indices):
        if plan_index is not None and plan_index in seen_plans:
            continue
        if plan_index is not None:
            seen_plans.add(plan_index)
        positions.add(pos)
        if len(positions) >= count:
            break
    return positions
