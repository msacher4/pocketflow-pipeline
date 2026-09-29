"""Test de la détection "garbage" du contenu d'article (ActuFinder).

La refonte des marqueurs vise un cas réel :
- L'article Crunchyroll (Star Detective Precure) était parfaitement récupéré
  par r.jina.ai (17k chars, article complet), mais _looks_like_garbage le
  rejetait parce que le header/footer de Crunchyroll contient "create account",
  "join for free", "we use cookies", "privacy policy".
- Résultat : article jeté -> fallback navigateur -> bannière cookies ->
  échec -> run terminé sans news.

Règles appliquées :
- Un contenu riche (>=60 mots de prose) n'est PAS garbage à cause de la nav
  du site (login/premium/cookies) : seuls les vrais bloqueurs (403/404/
  Cloudflare/security check) et les bannières consent EN TÊTE de page
  disqualifient.
- Le check s'effectue sur le corps COMPLET avant troncature (la tête de page
  est souvent de la nav markdown).
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
sys.path.insert(0, str(PIPELINE_ROOT / "nodes"))

from nodes.actufinder.actufinder_node import FetchArticleContentNode as F

# Article réel : nav du site (create account / join free / log in) + footer
# cookies, mais >= 60 mots de prose = contenu article riche.
_ARTICLE_RICHE = """
[Back to Crunchyroll Home](linkwww.crunchyroll.com/)
*   [Create Account Join for free or go Premium.](linksso.crunchyroll.com/authorize)
*   [Log in already joined crunchyroll? welcome back.](linksso.crunchyroll.com/authorize)
[Try Premium Free](linkwww.crunchyroll.com/premium)

Star Detective Precure! Anime Welcomes the Whole Team in New Opening Video
## New episodes of the magical girl anime stream every week on Crunchyroll

The CureTTO Detective Agency is together at last! Now that both Cure Eclair and
Cure Arcana are on the team, it is time for a new opening video. This season
brings new transformations and a bigger cast of characters than ever before,
with each Precure getting her own solo transformation scene and a group
finisher. Fans of the franchise have been eagerly awaiting these new designs
since the first announcement of the project. The producer confirmed the opening
was animated by the same studio that handled the previous season, and promised
that the final battle will be the most ambitious one yet.

[Privacy Policy](linkwww.crunchyroll.com/privacy) [Terms of Use](linktos)
We use cookies to improve your experience. Create Account Privacy Policy
"""

# Bannière de cookies pure (courte, en tête) -> garbage
_BANNIERE = (
    "Notre site utilise des cookies et d'autres technologies pour améliorer son "
    "fonctionnement et vous proposer de la publicité. Pour en savoir plus, "
    "veuillez lire notre Politique relative à la confidentialité et aux cookies. "
    "Vous pouvez gérer vos préférences à tout moment."
)

# Page 404 / bloquée
_NOTFOUND = (
    "<html><body><h1>404 - Page Not Found</h1>"
    "<p>The page you were looking for doesn't exist. Go back to the home page. "
    "Yuzu says there's nothing to see here! Please check the URL and try again.</p>"
    "</body></html>"
)

# Cloudflare / security check
_CLOUDFLARE = (
    "Verification de securite en cours. Ce site utilise un service de securite "
    "pour se proteger contre les bots malveillants. Just a moment while we check "
    "your browser. If you are a robot, you have been blocked. Attention required."
)

# Encart sponsorisé servi par r.jina.ai au lieu de l'article (cas réel
# Siliconera/ZenMarket : la synthèse trouvait has_character=False sur la pub).
_SPONSOR = """
Honkai: Star Rail's Robin Launches a New Song with Hatsune Miku
This article is written in partnership with ZenMarket. ZenMarket is a reliable
shipping proxy service that allows you to buy directly from Japanese stores
such as Mercari, Yahoo! Auctions Japan, Surugaya, and much more. Sign up and
gain ZenPoints to enjoy a wider range of services, up to 20% off shipping,
discounts on packaging fees, and more. In addition, there are plenty of
opportunities to win 20,000 Yen worth of ZenPoints, so don't miss out on these
fascinating benefits, which will definitely make your shopping experience
smoother. As for Robin's single, we do not have any further details on its
release date yet, but we will keep you informed as soon as we have more
information.
"""

# Page de navigation pure servie par r.jina.ai (cas réel GameSpace.com : menus
# catégories + plateformes + liens vers d'autres articles, 43 liens/4000 chars,
# ~34 mots de prose hors liens = contenu non-actionnable).
_NAV_SHELL = """
[![Image: GameSpace.com](linkgamespace.com/wp-content/uploads/2017/02/GF-Logo-black.png)](linkgamespace.com/)

*   [Games](linkgamespace.com/category/all-games/)
        *   [All](linkgamespace.com/category/all-games/)
        *   [3DS](linkgamespace.com/category/all-games/3ds/)
        *   [Android](linkgamespace.com/category/all-games/android/)
        *   [iOS](linkgamespace.com/category/all-games/ios/)
        *   [PC](linkgamespace.com/category/all-games/pc/)
        *   [PS Vita](linkgamespace.com/category/all-games/ps-vita/)
        *   [PS4](linkgamespace.com/category/all-games/ps4/)
        *   [Switch](linkgamespace.com/category/all-games/switch/)
        *   [Wii U](linkgamespace.com/category/all-games/wii-u/)
        *   [Xbox One](linkgamespace.com/category/all-games/xbox-one/)

[](linkgamespace.com/all-articles/news/2xko-check-out-lux-gameplay-reveal-trailer/#)

[![Image 4: Maestro Receives Physical Edition for Nintendo Switch](linkgamespace.com/wp-content/uploads/2026/07/Maestro-206x127.jpg)](linkgamespace.com/all-games/maestro-receives-physical-edition-for-nintendo-switch/ "Maestro Receives Physical Edition for Nintendo Switch")

[Maestro Receives Physical Edition for Nintendo Switch](linkgamespace.com/all-games/maestro-receives-physical-edition-for-nintendo-switch/)

[![Image 5: Octopath Traveler II for Nintendo Switch Adds Teaser Trailer](linkgamespace.com/wp-content/uploads/2026/07/Octopath-206x127.jpg)](linkgamespace.com/all-games/octopath-traveler-ii-for-nintendo-switch-adds-teaser-trailer/ "Octopath Traveler II for Nintendo Switch Adds Teaser Trailer")

[Octopath Traveler II for Nintendo Switch Adds Teaser Trailer](linkgamespace.com/all-games/octopath-traveler-ii-for-nintendo-switch-adds-teaser-trailer/)

[![Image 6: Clockfall: A Tale Towards Infinity](linkgamespace.com/wp-content/uploads/2026/07/Clockfall-206x127.jpg)](linkgamespace.com/all-games/clockfall-a-tale-towards-infinity/ "Clockfall: A Tale Towards Infinity")

[Clockfall: A Tale Towards Infinity](linkgamespace.com/all-games/clockfall-a-tale-towards-infinity/)
"""


def test_article_riche_passe():
    assert F._looks_like_garbage(_ARTICLE_RICHE) is False, "article réel rejeté"
    # Version tronquée à 4000 (nav en tête) SANS le corps complet : rejetée ->
    # c'est le rôle de content_full de décider sur le corps complet.
    assert F._looks_like_garbage(_ARTICLE_RICHE[:300]) is True


def test_banniere_cookies_rejetee():
    assert F._looks_like_garbage(_BANNIERE) is True


def test_pages_erreur_rejetees():
    assert F._looks_like_garbage(_NOTFOUND) is True
    assert F._looks_like_garbage(_CLOUDFLARE) is True


def test_footer_cookies_seul_ne_rejette_pas():
    """Footer cookies (en fin de contenu riche) ne doit pas tuer l'article."""
    article_fin = _ARTICLE_RICHE + ("We use cookies Privacy Policy " * 5)
    assert F._looks_like_garbage(article_fin) is False


def test_sponsor_rejete_meme_riche():
    """Pub sponsorisée (ZenMarket / partnership) jamais actionnable."""
    assert F._looks_like_garbage(_SPONSOR) is True


def test_nav_shell_rejetee():
    """Page de navigation pure (menus/liens, peu de prose) = garbage."""
    assert F._looks_like_garbage(_NAV_SHELL) is True


def test_find_article_start_ignore_nav():
    """_find_article_start doit sauter la nav GameSpace pour retrouver l'article."""
    full = _NAV_SHELL + """

2XKO – Check Out Lux Gameplay Reveal Trailer

Riot Games shared a brand new gameplay trailer for Lux in the upcoming tag-team
fighter 2XKO. The trailer highlights her abilities, her projectiles, her light
shield mechanic and her ultimate Light Binding. Fans of the champion will be
happy to see her iconic moves translated into a fighting game context, and the
new trailer gives a good sense of the pacing and the flashy combo potential the
game aims to deliver at launch. 2XKO is scheduled to launch on PlayStation 5,
Xbox Series X, and PC. The developer also confirmed that the duelist mode has
received several balance changes based on the feedback gathered during the
previous playtests, and that the new training mode includes a detailed frame
data guide for every character on the roster. The community is already
reasoning about the best combos, and the competitive scene is expected to pick
up the game quickly once the servers go live across all regions.
"""
    start = F._find_article_start(full)
    assert start > 0, "nav non ignorée"
    assert "Lux Gameplay Reveal" in full[start:start + 300]


if __name__ == "__main__":
    test_article_riche_passe()
    test_banniere_cookies_rejetee()
    test_pages_erreur_rejetees()
    test_footer_cookies_seul_ne_rejette_pas()
    test_sponsor_rejete_meme_riche()
    test_nav_shell_rejetee()
    test_find_article_start_ignore_nav()
    print("test_actufinder_garbage PASSED")